"""需求 R115: 真实大模型调用的计数与预算上限 (tests/tools/live_budget.py). 全部离线: 内部客户端用 MockLLMClient
(读 tests/fixtures/llm), 不联网."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient, SwitchableLLMClient
from backend.contracts.llm_outputs import ConfirmationExtraction
from backend.contracts.models import Decision, ReviewTask, TaskKind, TaskStatus
from backend.contracts.ports import LLMMessage, LLMRequest, LLMResponse, T_out
from tests import test_e2e_live as live
from tests.test_e2e_live_checks import offline
from tests.test_ingest_doc06 import setup
from tests.tools.live_budget import (
    Budget,
    BudgetedLLMClient,
    BudgetExceeded,
    declared_limits,
    install_budget,
    session_limits,
    summary,
    write_summary,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"
FAKE_KEY = "sk-test-0123456789"  # 假密钥: 断言它不出现在汇总里


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("LIVE_MAX_CALLS", "LIVE_MAX_INPUT_TOKENS", "LIVE_MAX_OUTPUT_TOKENS", "LIVE_SESSION_MAX_CALLS"):
        monkeypatch.delenv(key, raising=False)
    for key in ("LIVE_SESSION_MAX_INPUT_TOKENS", "LIVE_SESSION_MAX_OUTPUT_TOKENS", "LLM_PRICE_INPUT_PER_MTOK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)


class Counting(MockLLMClient):
    """MockLLMClient 读固定输出, 另记被调了几次, 并按给定的 token 数回报用量."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(FIXTURES)
        self.calls = 0
        self.tokens = (input_tokens, output_tokens)

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        self.calls += 1
        response = await super().complete(req, output_model)
        return response.model_copy(update={"input_tokens": self.tokens[0], "output_tokens": self.tokens[1]})


def request(procedure_id: str = "DOC-06", key: str = "BA-01") -> LLMRequest:
    return LLMRequest(
        procedure_id=procedure_id,
        run_id=f"RUN-{procedure_id}-1",
        system="s",
        messages=[LLMMessage(role="user", content="u")],
        max_tokens=100,
        fixture_key=key,
    )


async def call(client: Any, procedure_id: str = "DOC-06") -> None:
    await client.complete(request(procedure_id), ConfirmationExtraction)


def failed(call_check: Any) -> str:
    with pytest.raises(pytest.fail.Exception) as e:
        call_check()
    return str(e.value)


# ── 验收 1, 6: 超次数, 超 token, 正常累计 ──


async def test_calls_over_the_limit_are_not_sent() -> None:
    """预算 3 次: 第 4 次调用不调内部客户端, 抛 BudgetExceeded 并置 exceeded, 此后每次都直接拒绝; check 以
    "超出预算: 已用 n 次调用, 输入 x, 输出 y token" 失败."""
    inner = Counting()
    budget = Budget(max_calls=3)
    client = BudgetedLLMClient(inner, budget)
    for _ in range(3):
        await call(client)
    budget.check()  # 用满不算超限
    with pytest.raises(BudgetExceeded, match="超出预算: 已用 3 次调用, 输入 0, 输出 0 token"):
        await call(client, "DOC-04")
    with pytest.raises(BudgetExceeded):
        await call(client)
    assert (inner.calls, budget.used.calls, budget.exceeded) == (3, 3, True)
    message = failed(budget.check)
    assert message.startswith("超出预算: 已用 3 次调用, 输入 0, 输出 0 token") and "DOC-04" in message


async def test_tokens_over_the_limit_stop_further_calls() -> None:
    """累计输入 token 超过上限的那次调用已经发出 (之后才知道用量), 记账后即超限: check 失败, 下一次调用被拒."""
    inner = Counting(input_tokens=100, output_tokens=10)
    budget = Budget(max_calls=10, max_input_tokens=150)
    client = BudgetedLLMClient(inner, budget)
    await call(client)
    budget.check()
    await call(client)
    assert budget.exceeded and "输入 200, 输出 20 token" in failed(budget.check)
    with pytest.raises(BudgetExceeded):
        await call(client)
    assert inner.calls == 2
    out = Budget(max_calls=10, max_output_tokens=15)
    await call(BudgetedLLMClient(Counting(output_tokens=10), out))
    await call(BudgetedLLMClient(Counting(output_tokens=10), out))
    assert out.exceeded


async def test_usage_accumulates_by_procedure_and_into_the_session() -> None:
    """正常累计: 按程序记调用与 token, 测试的预算挂在会话预算下, 记账一并计入; 会话超限时测试的调用也被拒."""
    session = Budget(max_calls=4)
    first, second = Budget(max_calls=3, parent=session), Budget(max_calls=3, parent=session)
    a, b = BudgetedLLMClient(Counting(5, 2), first), BudgetedLLMClient(Counting(7, 3), second)
    await call(a, "DOC-06")
    await call(a, "DOC-04")
    await call(b, "DOC-06")
    assert (first.used.calls, first.used.input_tokens, first.used.output_tokens) == (2, 10, 4)
    assert {k: (v.calls, v.input_tokens) for k, v in session.by_procedure.items()} == {
        "DOC-06": (2, 12),
        "DOC-04": (1, 5),
    }
    await call(b, "DOC-04")
    with pytest.raises(BudgetExceeded):  # 会话的 4 次已用满
        await call(a, "DOC-06")
    assert session.exceeded and not second.exceeded
    assert failed(second.check).startswith("超出预算: 已用 4 次调用")  # 会话超限, 各测试的 check 都失败


# ── 验收 1, 6: 主模型与复核模型共用一个预算 ──


async def test_install_budget_wraps_main_and_review_models_once() -> None:
    """install_budget 把主模型 (判断引擎用的同一个) 与复核模型的内部客户端换成同一预算的包装, 模型名不变; 再装
    一次不重复包; 不是 SwitchableLLMClient 时报错."""
    main, review = SwitchableLLMClient(Counting(), "main-model"), SwitchableLLMClient(Counting(), "review-model")
    deps: Any = SimpleNamespace(llm=main, judgments=SimpleNamespace(llm=main), review_llm=review)
    budget = Budget(max_calls=3)
    install_budget(deps, budget)
    install_budget(deps, budget)
    assert isinstance(main.inner, BudgetedLLMClient) and not isinstance(main.inner.inner, BudgetedLLMClient)
    assert (main.model_name, review.model_name) == ("main-model", "review-model")
    await call(main, "CSH-02")
    await call(review, "GEN-09")
    await call(deps.judgments.llm, "DOC-06")
    with pytest.raises(BudgetExceeded):
        await call(review, "GEN-09")
    assert {k: v.calls for k, v in budget.by_procedure.items()} == {"CSH-02": 1, "GEN-09": 1, "DOC-06": 1}
    plain: Any = SimpleNamespace(llm=Counting(), judgments=SimpleNamespace(llm=main), review_llm=None)
    with pytest.raises(TypeError, match="SwitchableLLMClient"):
        install_budget(plain, budget)


# ── 验收 3: 上限 ──


def item(**kwargs: Any) -> Any:
    marker = SimpleNamespace(kwargs=kwargs)
    return SimpleNamespace(get_closest_marker=lambda name: marker if name == "live" else None)


def test_limits_from_marker_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """各测试的默认上限写在标记里, LIVE_MAX_* 覆盖; 没写 max_calls 报错. 会话上限缺省为各测试之和 (token 有一个没写
    就不限), LIVE_SESSION_MAX_* 只能调低."""
    assert declared_limits({"max_calls": 60}) == {"max_calls": 60, "max_input_tokens": None, "max_output_tokens": None}
    with pytest.raises(pytest.UsageError, match="max_calls"):
        declared_limits({})
    monkeypatch.setenv("LIVE_MAX_CALLS", "5")
    monkeypatch.setenv("LIVE_MAX_OUTPUT_TOKENS", "900")
    assert declared_limits({"max_calls": 60}) == {"max_calls": 5, "max_input_tokens": None, "max_output_tokens": 900}
    monkeypatch.delenv("LIVE_MAX_CALLS")
    monkeypatch.delenv("LIVE_MAX_OUTPUT_TOKENS")

    items = [
        item(max_calls=60, max_input_tokens=1000),
        item(max_calls=6),
        SimpleNamespace(get_closest_marker=lambda n: None),
    ]
    assert session_limits(items) == {"max_calls": 66, "max_input_tokens": None, "max_output_tokens": None}
    both = [item(max_calls=2, max_input_tokens=10), item(max_calls=3, max_input_tokens=20)]
    assert session_limits(both)["max_input_tokens"] == 30
    monkeypatch.setenv("LIVE_SESSION_MAX_CALLS", "40")
    assert session_limits(items)["max_calls"] == 40
    monkeypatch.setenv("LIVE_SESSION_MAX_CALLS", "100")  # 调高不生效
    assert session_limits(items)["max_calls"] == 66


def test_live_tests_declare_their_limits() -> None:
    """R59 的端到端验收默认 60 次 (验收 7); 其余联网用例也写了上限."""
    from tests import test_llm_live, test_sheet_intake

    def limit(func: Any) -> int:
        [mark] = [m for m in func.pytestmark if m.name == "live"]
        return int(mark.kwargs["max_calls"])

    assert limit(live.test_live_llm_full_flow) == 60
    assert limit(test_llm_live.test_live_doc06_extracts_ba01_reply) == 6
    assert limit(test_sheet_intake.test_live_recognizes_samples) == 3


# ── 验收 4, 5: 汇总 ──


async def test_summary_counts_attempts_and_prices_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用与 token 取包装的账, 尝试次数与失败级别取 llm_call 留痕 (DOC-06 两份回函, 各一次通过); 没设单价不出金额,
    两个单价都设了才按每百万 token 折算."""
    budget = Budget(max_calls=5)
    agent, ctx, trail = setup(llm=BudgetedLLMClient(Counting(input_tokens=1000, output_tokens=200), budget))
    await agent.run(ctx)
    data = summary(trail.entries, budget)
    assert (data["calls"], data["input_tokens"], data["output_tokens"], data["exceeded"]) == (2, 2000, 400, False)
    assert data["attempts"] == {"1": 2} and data["level_failed"] == {"passed": 2}
    assert data["by_procedure"]["DOC-06"]["calls"] == 2 and "cost" not in data
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "0.5")
    assert "cost" not in summary(trail.entries, budget)  # 只设一个不折算
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "2")
    assert summary(trail.entries, budget)["cost"] == round(2000 / 1e6 * 0.5 + 400 / 1e6 * 2, 6)


def test_summary_file_and_print_carry_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """汇总写 <名称>-<UTC 时间>.json 并打印; 文件与打印都只有计数, 不含密钥与 Authorization 头 (用假密钥全文检索)."""
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    budget = Budget(max_calls=2)
    budget.record("DOC-06", 10, 5)
    path = write_summary("test_x[case/1]", summary([], budget), tmp_path, datetime(2026, 10, 19, 1, 2, 3, tzinfo=UTC))
    assert path == tmp_path / "test_x_case_1_-20261019T010203Z.json"
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["calls"] == 1
    printed = capsys.readouterr().out
    for content in (text, printed):
        assert FAKE_KEY not in content and "authorization" not in content.casefold()


# ── 验收 2: 超限必失败, 不进入 rerun ──


async def test_e2e_driver_fails_on_budget_without_submitting_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R59 的驱动循环 (离线, 同 test_e2e_live_checks): 预算 3 次, 第 4 次调用落在一条程序上被拒, 该程序转成 failed
    任务; 驱动在提交决定前查预算, 以 "超出预算" 失败, 没有提交 rerun; 用量汇总照样写了 (exceeded 为真)."""
    observed = offline(monkeypatch, "fixtures")
    decisions: list[Any] = []
    original = live.review_decision

    async def spy(*args: Any) -> Any:
        decision = await original(*args)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(live, "review_decision", spy)
    with pytest.raises(pytest.fail.Exception, match="超出预算: 已用 3 次调用"):
        await live.test_live_llm_full_flow(tmp_path, Budget(max_calls=3))
    assert all(d.decision is not Decision.rerun for d in decisions)
    run = observed["run"]
    tasks = await run.deps.output_store.latest(run.eid, ReviewTask)
    assert any(t.kind is TaskKind.failed and t.status is TaskStatus.pending for t in tasks)
    assert observed["usage"]["exceeded"] is True and observed["usage"]["calls"] == 3
