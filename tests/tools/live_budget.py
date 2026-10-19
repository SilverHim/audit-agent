"""真实大模型测试的用量预算 (需求 R115): 按程序计调用次数与 token, 超过上限即停发, 并让测试失败 (不进入 rerun);
结束时写用量汇总.

- "一次调用" 指 BudgetedLLMClient 调一次内部客户端的 complete: 抛错的 (超时, 传输层重试耗尽, 连接失败) 也算;
  内部客户端自己的重发 (传输层重试 adapters/llm.MAX_RETRIES, 参数不受支持时调整后的重发) 不另计; 校验不过后
  判断引擎的重新尝试是另一次 complete, 另计.
- Budget: 一次测试 (或整个会话) 的上限与已用量. 第 max_calls+1 次调用, 或累计输入, 输出 token 已超上限后的调用都
  不再发出, 抛 BudgetExceeded 并置 exceeded; 此后每次调用都直接拒绝. 调用次数在发出前占位 (admit), 并发的调用
  (gather_all, P2 并行) 也只放行 max_calls 次. 测试的预算挂在会话预算下 (parent), 记账一并计入, 任一超限即拒绝.
- BudgetedLLMClient: 包一层 LLMClient, 调用前查预算并占位, 返回后记 token. install_budget 用
  SwitchableLLMClient.reload 把项目依赖里的主模型, 判断引擎与复核模型 (A7) 的内部客户端都换成包装, 共用一个预算.
- 超预算的调用在 Agent 里抛出, 由 run_agent 转成 failed 任务, 驱动测试的循环若照常提交 rerun 会反复超限; 所以各
  live 测试在每次提交决定前调 budget.check(), 已超限即 pytest.fail.
- summary: 调用次数与 token 由包装计, 尝试次数分布与校验失败级别取该项目 llm_call 留痕; 只有设了
  LLM_PRICE_INPUT_PER_MTOK 与 LLM_PRICE_OUTPUT_PER_MTOK 时才折算金额. write_summary 写 var/eval/<名称>-<UTC 时间>.json
  并打印. 汇总只有计数, 不含请求, 密钥与请求头.
- 上限: 各测试在 @pytest.mark.live(max_calls=..., max_input_tokens=..., max_output_tokens=...) 里写默认值,
  LIVE_MAX_CALLS, LIVE_MAX_INPUT_TOKENS, LIVE_MAX_OUTPUT_TOKENS 覆盖; 会话预算缺省为本次选中的各 live 测试上限之和,
  LIVE_SESSION_MAX_CALLS 等可调低. 上限按调用次数与 token 定, 不写单价; 不保证覆盖最坏情况 (一次调用的输出 token
  只在调用之后才知道, 超 token 上限的那一次已经发出; 并发时同时在途的几次也都已发出). 标了 live 却没写 max_calls
  或没取 live_budget 夹具的测试在收集阶段即报错 (live_without_limits, conftest).
- conftest 的两个夹具只调 session_budget, budget_for 与 finish.
"""

import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import SwitchableLLMClient
from backend.agents.base import AgentDeps
from backend.contracts.models import TrailEntry
from backend.contracts.ports import LLMClient, LLMRequest, LLMResponse, T_out

REPO = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO / "var" / "eval"  # 用量汇总的目录 (var/ 不入库)
LIMITS = ("max_calls", "max_input_tokens", "max_output_tokens")
TEST_ENV = {
    "max_calls": "LIVE_MAX_CALLS",
    "max_input_tokens": "LIVE_MAX_INPUT_TOKENS",
    "max_output_tokens": "LIVE_MAX_OUTPUT_TOKENS",
}
SESSION_ENV = {
    "max_calls": "LIVE_SESSION_MAX_CALLS",
    "max_input_tokens": "LIVE_SESSION_MAX_INPUT_TOKENS",
    "max_output_tokens": "LIVE_SESSION_MAX_OUTPUT_TOKENS",
}
PRICE_ENV = ("LLM_PRICE_INPUT_PER_MTOK", "LLM_PRICE_OUTPUT_PER_MTOK")  # 每百万 token 的单价, 两项都设才折算


class BudgetExceeded(Exception):
    """超出预算: 这次调用没有发出."""


@dataclass
class Usage:
    """一个程序 (或合计) 的用量."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Budget:
    """大模型用量的上限与已用量 (一次测试或整个会话). token 上限为 None 时不限."""

    max_calls: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    parent: "Budget | None" = None  # 会话预算: 记账一并计入, 它超限也拒绝
    used: Usage = field(default_factory=Usage)
    by_procedure: dict[str, Usage] = field(default_factory=dict)
    exceeded: bool = False
    rejected_procedure: str | None = None  # 第一次被拒的调用来自的程序
    reported: bool = False  # 测试自己写过汇总 (夹具收尾时不再写)

    def over(self) -> bool:
        """调用次数已用满 (再调一次就超出). token 超限在记账时即置 exceeded (record)."""
        return self.used.calls >= self.max_calls

    def message(self) -> str:
        u = self.used
        where = f"; 被拒的调用来自 {self.rejected_procedure}" if self.rejected_procedure else ""
        return f"超出预算: 已用 {u.calls} 次调用, 输入 {u.input_tokens}, 输出 {u.output_tokens} token{where}"

    def admit(self, procedure_id: str) -> None:
        """发出一次调用之前查预算并占位: 本预算或会话预算已超限 (exceeded), 或调用次数已用满时, 置它 exceeded 并抛
        BudgetExceeded, 哪个预算都不计; 否则当场给链上每个预算的调用次数 (合计与该程序) 加一. 查与占之间没有
        await, 并发的调用不会一起通过; 之后内部客户端抛错, 这次也已计入."""
        chain = self.chain()
        for budget in chain:
            if budget.exceeded or budget.over():
                budget.exceeded = True
                budget.rejected_procedure = budget.rejected_procedure or procedure_id
                raise BudgetExceeded(budget.message())
        for budget in chain:
            for usage in budget.usages(procedure_id):
                usage.calls += 1

    def record(self, procedure_id: str, input_tokens: int, output_tokens: int) -> None:
        """一次调用返回之后记 token (本预算与会话预算; 调用次数已在 admit 计入); 累计 token 超过上限时即置
        exceeded (调用次数用满不算超限, 下一次调用才被拒)."""
        for budget in self.chain():
            for usage in budget.usages(procedure_id):
                usage.input_tokens += input_tokens
                usage.output_tokens += output_tokens
            if budget.tokens_over():
                budget.exceeded = True

    def usages(self, procedure_id: str) -> tuple[Usage, Usage]:
        """合计与该程序的用量."""
        return self.used, self.by_procedure.setdefault(procedure_id, Usage())

    def tokens_over(self) -> bool:
        u = self.used
        return (self.max_input_tokens is not None and u.input_tokens > self.max_input_tokens) or (
            self.max_output_tokens is not None and u.output_tokens > self.max_output_tokens
        )

    def chain(self) -> list["Budget"]:
        return [self] if self.parent is None else [self, *self.parent.chain()]

    def check(self) -> None:
        """驱动测试的循环在每次提交决定前调: 本预算或会话预算已超限即 pytest.fail (不再提交 rerun 等决定)."""
        for budget in self.chain():
            if budget.exceeded:
                pytest.fail(budget.message())


class BudgetedLLMClient:
    """LLMClient 的包装: 调用前查预算并占位 (超限抛 BudgetExceeded, 不调 inner), 返回后按请求的程序记 token."""

    def __init__(self, inner: LLMClient, budget: Budget) -> None:
        self.inner = inner
        self.budget = budget

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        self.budget.admit(req.procedure_id)
        response = await self.inner.complete(req, output_model)
        self.budget.record(req.procedure_id, response.input_tokens or 0, response.output_tokens or 0)
        return response


def install_budget(deps: AgentDeps, budget: Budget) -> None:
    """把项目依赖里的主模型 (deps.llm), 判断引擎的客户端 (deps.judgments.llm, 组装根与 deps.llm 是同一个) 与复核模型
    (deps.review_llm, A7) 的内部客户端换成同一预算的包装 (SwitchableLLMClient.reload, 模型名不变). 已经包过的不再包.
    客户端不是 SwitchableLLMClient 时抛 TypeError (组装根总是建它; 测试里的假客户端须先包一层)."""
    clients = {id(c): c for c in (deps.llm, deps.judgments.llm, deps.review_llm) if c is not None}
    for client in clients.values():
        if not isinstance(client, SwitchableLLMClient):
            raise TypeError(f"install_budget 需要 SwitchableLLMClient, 得到 {type(client).__name__}")
        if not isinstance(client.inner, BudgetedLLMClient):
            client.reload(BudgetedLLMClient(client.inner, budget), client.model_name)


def env_limit(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None


def declared_limits(declared: Mapping[str, Any]) -> dict[str, int | None]:
    """一个 live 测试的上限: 标记里写的默认值, LIVE_MAX_* 设了时以它为准. 没写 max_calls 时抛 pytest.UsageError."""
    if not declared.get("max_calls"):
        raise pytest.UsageError("live 测试须在标记里写上限: @pytest.mark.live(max_calls=n)")
    limits: dict[str, int | None] = {key: declared.get(key) for key in LIMITS}
    for key, env in TEST_ENV.items():
        override = env_limit(env)
        if override is not None:
            limits[key] = override
    return limits


def live_without_limits(items: Iterable[Any]) -> list[str]:
    """标了 live 却没在标记里写 max_calls, 或没取 live_budget 夹具的测试 (nodeid); conftest 在收集阶段据此报错, 免得
    联网测试没有上限 (没取夹具就没包上预算, 写了上限也不起作用)."""
    return [
        i.nodeid
        for i in items
        if (m := i.get_closest_marker("live")) is not None
        and (not m.kwargs.get("max_calls") or "live_budget" not in i.fixturenames)
    ]


def session_limits(items: Iterable[Any]) -> dict[str, int | None]:
    """会话预算的上限: 本次选中的各 live 测试上限 (declared_limits) 之和; token 上限有一个测试没写就不限.
    LIVE_SESSION_MAX_* 设了且更小时取它 (只能调低)."""
    each = [declared_limits(m.kwargs) for i in items if (m := i.get_closest_marker("live")) is not None]
    limits: dict[str, int | None] = {"max_calls": sum(int(e["max_calls"] or 0) for e in each)}
    for key in ("max_input_tokens", "max_output_tokens"):
        values = [e[key] for e in each]
        limits[key] = sum(v for v in values if v is not None) if values and None not in values else None
    for key, env in SESSION_ENV.items():
        lower = env_limit(env)
        if lower is not None and (limits[key] is None or lower < int(limits[key] or 0)):
            limits[key] = lower
    return limits


def from_limits(limits: Mapping[str, int | None], parent: Budget | None = None) -> Budget:
    return Budget(
        max_calls=int(limits["max_calls"] or 0),
        max_input_tokens=limits["max_input_tokens"],
        max_output_tokens=limits["max_output_tokens"],
        parent=parent,
    )


def session_budget(items: Iterable[Any]) -> Budget:
    """会话夹具 live_session_budget 的预算: 上限取 session_limits (本次选中的各 live 测试之和)."""
    return from_limits(session_limits(items))


def budget_for(node: Any, session: Budget) -> Budget:
    """测试夹具 live_budget 的预算: 上限取该测试 live 标记 (declared_limits, 没有标记即报错), 挂在会话预算下."""
    marker = node.get_closest_marker("live")
    return from_limits(declared_limits(marker.kwargs if marker is not None else {}), parent=session)


def finish(name: str, budget: Budget, root: Path = EVAL_DIR) -> Path | None:
    """夹具收尾: 有调用而测试自己没写过汇总 (reported) 时, 按包装记的账写一份 (没有留痕的尝试与失败级别)."""
    if not budget.used.calls or budget.reported:
        return None
    return write_summary(name, summary([], budget), root)


def summary(entries: Sequence[TrailEntry], budget: Budget) -> dict[str, Any]:
    """用量汇总: 调用次数与 token 取 budget (包装记的账, 含被拒前的全部调用); 尝试次数分布与校验失败级别 (syntax,
    structure, reference, semantic, 通过记 passed) 取 entries (该项目的 llm_call 留痕, payload.attempt 与
    level_failed). 设了 LLM_PRICE_INPUT_PER_MTOK 与 LLM_PRICE_OUTPUT_PER_MTOK 时另出 cost (按每百万 token 折算)."""
    calls = [e for e in entries if e.payload.get("attempt") is not None]
    by_procedure: dict[str, Any] = {}
    for pid in sorted(set(budget.by_procedure) | {e.procedure_id or "" for e in calls}):
        usage = budget.by_procedure.get(pid, Usage())
        rows = [e for e in calls if (e.procedure_id or "") == pid]
        by_procedure[pid] = {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "attempts": {str(k): v for k, v in sorted(Counter(e.payload["attempt"] for e in rows).items())},
            "level_failed": dict(sorted(Counter(e.payload.get("level_failed") or "passed" for e in rows).items())),
        }
    out: dict[str, Any] = {
        "calls": budget.used.calls,
        "input_tokens": budget.used.input_tokens,
        "output_tokens": budget.used.output_tokens,
        "limits": {key: getattr(budget, key) for key in LIMITS},
        "exceeded": budget.exceeded,
        "attempts": {str(k): v for k, v in sorted(Counter(e.payload["attempt"] for e in calls).items())},
        "level_failed": dict(sorted(Counter(e.payload.get("level_failed") or "passed" for e in calls).items())),
        "by_procedure": by_procedure,
    }
    prices = [os.getenv(name) for name in PRICE_ENV]
    if all(prices):
        price_in, price_out = (float(p or 0) for p in prices)
        out["cost"] = round(budget.used.input_tokens / 1e6 * price_in + budget.used.output_tokens / 1e6 * price_out, 6)
    return out


def write_summary(name: str, data: Mapping[str, Any], root: Path = EVAL_DIR, now: datetime | None = None) -> Path:
    """汇总写 root/<名称>-<UTC 时间>.json 并打印; 名称里的路径分隔符与参数化的方括号换成 "_"."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe}-{stamp}.json"
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    print(f"\n大模型用量 ({path.name}):\n{text}")
    return path


def report(name: str, entries: Sequence[TrailEntry], budget: Budget) -> dict[str, Any]:
    """测试结束时写本次的用量汇总 (summary + write_summary), 记 reported (夹具收尾时不再写); 返回汇总."""
    data = summary(entries, budget)
    budget.reported = True
    write_summary(name, data)
    return data
