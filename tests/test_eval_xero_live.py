"""Xero 程序级真实大模型评测. 本文件先写现有程序部分 (需求 R250; 设计稿 v2.1 第 5.1, 6.1, 6.2 节).

走法: FV-51 测试台 (tests/xero_harness) 连库载入 T0 副本 (db_deps: 演示数据导入两阶段, 合同在上传时由 DOC-04 抽取),
再按依赖分层跑 REV-02 与 CSH-02 的闭包 (run_programs: 先 DOC-04, PLN-07, PLN-01 定重要性, 再 CSH-04, REV-03 等; 每层
先自动确认上游判断, 所以 REV-03 的判断确认后才跑 REV-02); 某程序出了 failed 任务即照 rerun 决定重跑一次. 只有预期
文件里的五个程序 (DOC-04, DOC-06, REV-03, REV-02, CSH-02) 交真实模型 (RoutedLLMClient 按程序分流, 经预算), 其余调用
(上传分类, A7 复核) 读副本的运行副本.

预期在 tests/fixtures/xero/expected_existing.json: hard 不符即失败, soft 只告警; 对象为两段之并 (三份合同, 两户回函,
三份合同的收入评价, 六张发票, BA-091 的函证判断, 共 15 个), 按 (程序, 对象) 取观察值 (observe) 逐字段比 (not_cited
列不得引用的来源). 硬性另有: 每个对象都有通过四级校验的结果 (failed 后重跑一次仍没有, 或带规则例外的对象失败转为
unattributed 任务, 都算不过); 每个对象都真的交给了模型; 没有失败的执行 (Agent 抛出的异常由节点转成失败的执行); 全部
证据指针经真实存储解析通过. 汇总写 var/eval/xero-existing-<UTC>.json.

配置经 tests.external.live_llm_settings 取, 没配时跳过. 不联网的自测把 "真实模型" 换成运行副本的固定输出: 连库一遍
(同真实模型的路径), 离线一遍 (一个对象先失败再重跑, 回函抽错时硬性检查报出).
"""

import json
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from backend.agents.ingest import extracted_from
from backend.contracts.models import (
    Confirmation,
    Judgment,
    Procedure,
    ProcedureRun,
    ProposedAction,
    ReviewTask,
    RunStatus,
    TaskKind,
    TaskStatus,
    TrailKind,
)
from backend.contracts.ports import LLMClient
from tests.live_walk import latest_model_judgments, unresolved_pointers
from tests.test_xero_injections import Build, offline_build
from tests.test_xero_injections_db import db_build, live_client
from tests.tools.live_budget import EVAL_DIR, Budget, summary, write_summary
from tests.xero_fakes import XERO_RULES
from tests.xero_flow import offline_runner
from tests.xero_harness import FlakyLLM, LayeredMockLLMClient, RoutedLLMClient, XeroHarness, run_programs
from tests.xero_injection import XeroCase

pytestmark = pytest.mark.external  # 都读 Xero 副本

Fields = dict[str, dict[str, dict[str, Any]]]  # 程序 → 对象 → 字段 → 值
EXPECTED: dict[str, Fields] = json.loads(
    (Path(__file__).parent / "fixtures" / "xero" / "expected_existing.json").read_text(encoding="utf-8")
)
SUBJECTS = sorted({(pid, s) for part in EXPECTED.values() for pid, objects in part.items() for s in objects})
LIVE_PROGRAMS = sorted({pid for pid, _ in SUBJECTS})  # 交真实模型的五个程序
PROGRAMS = ["REV-02", "CSH-02"]  # 测试台要跑的程序; 依赖闭包含 DOC-04 (先跑), DOC-06, REV-03
MAX_CALLS = 45  # 15 个对象各三次尝试


async def observe(h: XeroHarness) -> Fields:
    """存储中各对象通过校验的结果, 取预期比对的字段: DOC-04 为合同 (条款类型排序), DOC-06 为当前函证中由回函抽取的
    (证据指向回函, 同 ingest.extracted_from), 判断程序为模型自己给出的最近一次判断 (latest_model_judgments, 重跑过的
    取重跑的), cited 为证据与推理链引用的来源. 没有结果的对象不在其中."""
    eid, store = h.engagement.id, h.deps.output_store
    found: Fields = {"DOC-04": {}, "DOC-06": {}}
    for c in await h.deps.financial_store.contracts(eid):
        found["DOC-04"][c.id] = c.model_dump(mode="json", include={"counterparty_abn", "amount_cents", "sign_date"})
        found["DOC-04"][c.id]["term_types"] = sorted(t.term_type.value for t in c.terms)
    for c in await store.latest(eid, Confirmation):
        if extracted_from(c, c.reply_document_id):
            fields = {"confirmed_balance_cents", "balance_date", "accrued_interest_cents"}
            found["DOC-06"][c.bank_account_id] = c.model_dump(mode="json", include=fields)
    for (pid, subject), j in latest_model_judgments(await store.latest(eid, Judgment)).items():
        draft = j.misstatement_draft
        found.setdefault(pid, {})[subject] = {
            "judgment_id": j.id,
            "proposed_action": j.proposed_action.value,
            "misstatement": j.proposed_action is ProposedAction.misstatement,
            "amount_cents": draft.amount_cents if draft else None,
            "direction": draft.direction.value if draft else None,
            "cited": sorted({p.source_id for p in [*j.evidence, *(p for s in j.reasoning_chain for p in s.evidence)]}),
        }
    return found


def differences(observed: Fields, expected: Fields) -> list[str]:
    """observed 与一段预期 (hard 或 soft) 不符之处, 每处一句; not_cited 为不得引用的来源. 没有结果的对象不在这里报
    (见 evaluate 的 missing)."""
    found = []
    for pid, objects in sorted(expected.items()):
        for subject, fields in sorted(objects.items()):
            got = observed.get(pid, {}).get(subject)
            if got is None:
                continue
            for name, want in fields.items():
                if name == "not_cited":
                    cited = sorted(set(want) & set(got["cited"]))
                    found += [f"{pid} {subject} 引用了上下文外的 {', '.join(cited)}"] if cited else []
                elif got[name] != want:
                    found.append(f"{pid} {subject} {name}: {got[name]!r}, 预期 {want!r}")
    return found


async def evaluate(h: XeroHarness, routed: RoutedLLMClient, budget: Budget) -> dict[str, Any]:
    """汇总 (验收 4) 与两级检查的材料, 都取存储 (中途出错时也能汇总已写入的): 用量 (调用与 token 取预算, 尝试与失败
    级别取五个程序的 llm_call 留痕), 各对象交真实模型的次数与观察值, 重跑的程序与对象, 失败的执行, 待决的非 normal
    任务, 缺结果与没交给模型的对象, hard 与 soft 的不符之处, 证据指针数与解析不了的."""
    eid, store = h.engagement.id, h.deps.output_store
    observed = await observe(h)
    live = Counter((r.procedure_id, r.fixture_key) for route, r in routed.requests if route == "live")
    entries = await h.deps.trail.list(eid, TrailKind.llm_call, limit=10_000)
    runs = await store.latest(eid, ProcedureRun)
    times = Counter(r.procedure_id for r in runs)
    pack = {p.id: p for p in await store.latest(eid, Procedure)}
    data: dict[str, Any] = {
        "engagement_id": eid,
        "usage": summary([e for e in entries if e.procedure_id in LIVE_PROGRAMS], budget),
        "live_calls": {f"{pid} {s}": live[pid, s] for pid, s in SUBJECTS},
        "observed": {f"{pid} {s}": observed[pid][s] for pid, s in SUBJECTS if s in observed.get(pid, {})},
        "reruns": {pid: pack[pid].rerun_subject_ids for pid, n in sorted(times.items()) if n > 1},  # 空列表为整条重跑
        "failed_runs": sorted(r.id for r in runs if r.status is RunStatus.failed),
        "open_tasks": sorted(
            f"{t.id} {t.kind.value}"
            for t in await store.latest(eid, ReviewTask)
            if t.status is TaskStatus.pending and t.kind is not TaskKind.normal
        ),
        "missing": [f"{pid} {s}" for pid, s in SUBJECTS if s not in observed.get(pid, {})],
        "unrouted": [f"{pid} {s}" for pid, s in SUBJECTS if not live[pid, s]],
        "hard": differences(observed, EXPECTED["hard"]),
        "soft": differences(observed, EXPECTED["soft"]),
    }
    data["pointers"], data["unresolved_pointers"] = await unresolved_pointers(offline_runner(h.deps), eid)
    return data


def assert_hard(data: dict[str, Any]) -> None:
    """硬性检查 (验收 2, 4): 每个对象都有结果且都交给了模型, 没有失败的执行, hard 段全部相符, 证据指针全部解析通过."""
    problems = {key: data[key] for key in ("missing", "unrouted", "failed_runs", "hard", "unresolved_pointers")}
    assert not any(problems.values()), problems
    assert data["pointers"] > 30


def warn_soft(data: dict[str, Any]) -> None:
    """软性检查 (验收 3): soft 段的不符之处逐条告警."""
    for line in data["soft"]:
        warnings.warn(line, stacklevel=1)


async def evaluated(
    build: Build, case: XeroCase, live: LLMClient, budget: Budget, root: Path = EVAL_DIR
) -> dict[str, Any]:
    """在 case 上装配测试台 (build), 五个程序的调用交 live (经 budget), 其余读副本的运行副本; 跑 PROGRAMS 的闭包 (出了
    failed 任务的程序重跑一次), 预算已超限即失败. 汇总写 root (缺省 var/eval) 下的 xero-existing-<UTC>.json, 中途出错
    也写出已走到的部分 (装配就出错时只有 live_budget 夹具收尾写的用量)."""
    routed = RoutedLLMClient(LayeredMockLLMClient([case.supplements / "llm"]), live, (), budget, programs=LIVE_PROGRAMS)
    h = await build(case, routed)
    try:
        await run_programs(h, PROGRAMS, rerun=True)
        budget.check()
    finally:
        data = await evaluate(h, routed, budget)
        budget.reported = True
        write_summary("xero-existing", data, root=root)
    return data


@pytest.mark.slow
@pytest.mark.live(max_calls=MAX_CALLS)  # 用量上限 (需求 R115), LIVE_MAX_CALLS 等可覆盖
async def test_xero_existing_programs_live(xero_case: Any, live_budget: Budget) -> None:
    """验收 1-4: 连库测试台在 T0 副本上, DOC-04, DOC-06, REV-03, REV-02, CSH-02 交真实模型 (预算 45 次); 硬性与软性
    检查见 assert_hard, warn_soft; 汇总写 var/eval/xero-existing-<UTC>.json."""
    live = live_client()  # 先核对数据与测试库, 没配模型时跳过
    data = await evaluated(db_build, xero_case("T0"), live, live_budget)
    warn_soft(data)
    assert_hard(data)


@pytest.mark.slow
async def test_live_path_on_postgres_with_mock_llm(xero_case: Any, tmp_path: Path) -> None:
    """连库自测: 真实模型用例的同一路径, "真实模型" 换成运行副本的固定输出. 硬性检查都过, soft 没有不符, 没有重跑与
    非 normal 任务; 15 个对象各交一次, 预算计 15 次; 汇总写在临时目录."""
    case = xero_case("T0")
    budget = Budget(max_calls=MAX_CALLS)
    data = await evaluated(db_build, case, LayeredMockLLMClient([case.supplements / "llm"]), budget, tmp_path)
    assert_hard(data)
    assert (data["soft"], data["reruns"], data["open_tasks"]) == ([], {}, [])
    assert set(data["live_calls"].values()) == {1} and data["usage"]["calls"] == 15
    assert len(list(tmp_path.glob("xero-existing-*.json"))) == 1


async def test_eval_reruns_once_and_catches_a_wrong_extraction(xero_case: Any, tmp_path: Path) -> None:
    """离线自测 (离线测试台, 规则预置 XERO_RULES): REV-03 对 CON-X02 的前 3 次调用输出不是 JSON, 成为 failed 任务, 重跑
    一次后得出 accept (第 2 次执行的判断), REV-02 照常判断; DOC-06 把 BA-090 的余额日抽成回函的落款日. 硬性检查恰报出
    这一处, 外加三份合同没交给模型 (离线测试台的合同在载入时按固定输出抽取, DOC-04 不调模型); soft 没有不符."""
    case = xero_case("T0")
    runtime, wrong = case.supplements / "llm", tmp_path / "wrong"
    reply = json.loads((runtime / "DOC-06" / "BA-090.json").read_text(encoding="utf-8"))
    (wrong / "DOC-06").mkdir(parents=True)
    (wrong / "DOC-06" / "BA-090.json").write_text(json.dumps(reply | {"balance_date": "2026-10-08"}))
    live = FlakyLLM(LayeredMockLLMClient([wrong, runtime]), {("REV-03", "CON-X02"): 3})
    data = await evaluated(offline_build(XERO_RULES), case, live, Budget(max_calls=MAX_CALLS), tmp_path)
    assert data["hard"] == ["DOC-06 BA-090 balance_date: '2026-10-08', 预期 '2026-09-30'"]
    assert data["unrouted"] == ["DOC-04 CON-X01", "DOC-04 CON-X02", "DOC-04 CON-X03"]
    assert (data["missing"], data["failed_runs"], data["soft"]) == ([], [], [])
    assert data["reruns"] == {"REV-03": ["CON-X02"]} and data["live_calls"]["REV-03 CON-X02"] == 4
    assert data["observed"]["REV-03 CON-X02"]["judgment_id"] == "J-REV-03-2-CON-X02"
    with pytest.raises(AssertionError, match="BA-090"):
        assert_hard(data)
