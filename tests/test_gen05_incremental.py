"""需求 R194: GEN-05 的增量实例 (verify_correction 节点).

Xero 演示上沿用错报闭环的装配 (misstatement_fakes.fin09_deps: 规则引擎预置 FIN-09 的 V01 行, 财务存储另记
更正分录), 功能另开 journals: 程序包有 P2 已完成的 GEN-05 (第 1 次执行) 与 FIN-09. 规则引擎对 GEN-05 不出例外, 只记
调用 (程序编号, 执行, 参数). 节点层的用例直接调 GraphNodes; 主图的用例检查点以 fan_in 身份写入, 分诊与沟通空执行,
停在 await_correction 等更正通知, 提交后 verify_correction 先做增量扫描再执行 FIN-09 (FIN-09 等复核).
"""

from typing import Any

import pytest

from backend.contracts.errors import RuleError
from backend.contracts.models import (
    AuditException,
    CorrectionNotice,
    ExceptionDisposition,
    Judgment,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    RunPopulation,
    RunStatus,
    RunTrigger,
)
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from backend.orchestration.merge import MergeRejected
from backend.orchestration.nodes import GraphNodes, increment_entry_nos
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.features import BASELINE, MISSTATEMENT
from tests.fakes import (
    ENGAGEMENT,
    FakeTrail,
    fin08_registered,
    finished,
    gen05_exception,
    latest,
    make_ctx,
    make_run,
    start_at_fan_in,
    submit_and_wait,
)
from tests.misstatement_fakes import (
    FIN09,
    FIRST,
    SECOND,
    fin03_state,
    fin09_deps,
    notice,
    round1_communicated,
    round1_misstatements,
    round2_communicated,
    round2_sources,
)
from tests.test_decisions import Started
from tests.xero_fakes import registration, xero_engagement

pytestmark = pytest.mark.external  # Xero 快照, 补造与 FIN-09 的 Xero fixture

GEN05 = "GEN-05"
FEATURES = BASELINE | {MISSTATEMENT, "journals"}
ALL = sorted(FIRST + SECOND)
CONFIG = GraphRunner.config(ENGAGEMENT.id)  # 同 xero_engagement().id; 收集阶段不读快照


def incremental(run_no: int, entry_nos: list[str]) -> ProcedureRun:
    """GEN-05 第 run_no 次执行: 扫过 entry_nos 的增量实例 (已结束)."""
    run = make_run(GEN05, run_no, params={"entry_nos": entry_nos})
    return finished(run.model_copy(update={"trigger": RunTrigger.incremental}))


def posted(oid: str, entry_nos: list[str]) -> CorrectionNotice:
    return notice(oid, adjustment_entry_nos=entry_nos)


@pytest.mark.parametrize(
    ("notices", "runs", "expected"),
    [
        ([posted("D-CN1", FIRST)], [], FIRST),
        ([posted("D-CN1", FIRST), posted("D-CN2", SECOND)], [incremental(2, FIRST)], SECOND),
        ([posted("D-CN1", FIRST), posted("D-CN2", [])], [incremental(2, FIRST)], SECOND),  # 空即载入全部未载入的
        ([posted("D-CN1", ["AJE-X05", "AJE-X01"])], [finished(make_run(GEN05, 2, {"entry_nos": FIRST}))], FIRST),
        ([notice("D-STOP", kind="stop_waiting", adjustment_entry_nos=[], reason="r")], [], []),
    ],
    ids=["first", "second", "empty_notice", "rerun", "stop_waiting"],
)
def test_increment_entry_nos(notices: list[CorrectionNotice], runs: list[ProcedureRun], expected: list[str]) -> None:
    """要扫的凭证: 各份 adjustments_posted 通知载入的 (为空取数据集可载入的全部), 去掉增量执行扫过的, 排序. 改参数的
    重跑 (trigger rerun) 带 entry_nos 也不算扫过."""
    state = fin03_state(correction_notices=notices, procedure_runs=runs)
    assert increment_entry_nos(state, GEN05, ALL) == expected


def gen05_pack(deps: Any) -> list[Procedure]:
    """程序包: P2 已完成的 GEN-05, 加错报闭环的 FIN-03, FIN-08, FIN-09 (第 1 版)."""
    first = [make_ctx(deps, pid, engagement=xero_engagement()).procedure for pid in (GEN05, "FIN-03", "FIN-08", FIN09)]
    return [first[0].model_copy(update={"status": ProcedureStatus.completed}), *first[1:]]


def scanning_deps(exceptions: Any = None, entries: list[str] = FIRST) -> Any:
    """fin09_deps 加 GEN-05 的规则结果 (缺省不出例外; exceptions 为按执行生成例外的函数), 功能另开 journals."""
    gen05 = {GEN05: lambda run: RuleResult(exceptions=exceptions(run) if exceptions else [], rows={})}
    return fin09_deps(FakeTrail(), entries=entries, results=gen05, features=FEATURES)


def node_state(deps: Any, **changes: Any) -> EngagementState:
    """verify_correction 之前的状态: GEN-05 第 1 次执行已结束; FIN-08 第 1 轮已登记 (首次沟通时间)."""
    first = fin08_registered(1, registration(1, round1_misstatements()))
    base = fin03_state(procedure_runs=[finished(make_run(GEN05))], procedure_pack=gen05_pack(deps), **first)
    return base | changes  # type: ignore[return-value]


def rule_calls(deps: Any, procedure_id: str = GEN05) -> list[tuple[str, list[str] | None]]:
    """规则引擎对该程序的调用: (执行, 参数 entry_nos)."""
    return [(run_id, params.get("entry_nos")) for pid, run_id, params in deps.rules.calls if pid == procedure_id]


def x01_hit(run: ProcedureRun) -> list[AuditException]:
    """R02 命中 AJE-X01 的一条例外."""
    return [gen05_exception(run, "R02", "medium", "AJE-X01-2", 75000, {"entry_no": "AJE-X01"})]


async def test_scan_increment_writes_run_and_exceptions_only() -> None:
    """验收 1: 建 RUN-GEN-05-2 (trigger incremental, 参数 entry_nos 排序后的凭证号, audit_adjustment_start 为首次沟通
    时间), 调 A6, 执行记录, 例外与被测总体 (POP-RUN-GEN-05-2, 需求 R324) 在一个事务内写入; GEN-05 不出新版本; 返回
    片段即写入的. 再执行一次 (重放) 不调规则引擎, 取回同一片段."""
    deps = scanning_deps(x01_hit)
    nodes = GraphNodes(deps, 2)
    state = node_state(deps)
    engagement = xero_engagement()

    patch = await nodes.scan_increment(state, engagement, GEN05, ["AJE-X05", "AJE-X01"])

    [run] = patch["procedure_runs"]
    assert (run.id, run.trigger, run.status, run.params_snapshot["entry_nos"]) == (
        "RUN-GEN-05-2",
        RunTrigger.incremental,
        RunStatus.succeeded,
        FIRST,
    )
    first_communicated = registration(1, round1_misstatements())[0]["communicated_at"]
    assert run.params_snapshot["audit_adjustment_start"] == first_communicated
    assert [x.id for x in patch["exceptions"]] == ["X-GEN-05-2-R02-1"]
    [commit] = [c for c in deps.output_store.commits if any(getattr(o, "id", None) == run.id for o in c)]
    assert {type(o).__name__ for o in commit} == {"ProcedureRun", "AuditException", "RunPopulation"}
    [population] = [o for o in commit if isinstance(o, RunPopulation)]
    assert (population.id, population.run_id) == ("POP-RUN-GEN-05-2", run.id)
    assert not any(isinstance(o, Procedure) for o in deps.output_store.rows)
    assert rule_calls(deps) == [("RUN-GEN-05-2", FIRST)]

    assert await nodes.scan_increment(state, engagement, GEN05, FIRST) == patch
    assert rule_calls(deps) == [("RUN-GEN-05-2", FIRST)]


async def test_scan_increment_needs_incremental_template_and_entries() -> None:
    """验收 1: 模板 incremental 为假抛 ValueError; 没有要扫的凭证时不执行, 返回 {}."""
    deps = scanning_deps()
    nodes = GraphNodes(deps, 2)
    state = node_state(deps)
    with pytest.raises(ValueError, match="FIN-09 没有增量实例"):
        await nodes.scan_increment(state, xero_engagement(), FIN09, FIRST)
    assert await nodes.scan_increment(state, xero_engagement(), GEN05, []) == {}
    assert rule_calls(deps) == []


def rule_error(run: ProcedureRun) -> list[AuditException]:
    raise RuleError("GEN-05/R02: timeout")


def unknown_run_exception(run: ProcedureRun) -> list[AuditException]:
    """引用不存在的执行 (RUN-GEN-05-9) 的例外, 外键校验不过."""
    return [gen05_exception(make_run(GEN05, 9), "R02", "medium", "AJE-X01-2", 75000, {"entry_no": "AJE-X01"})]


@pytest.mark.parametrize(
    ("exceptions", "error"), [(rule_error, RuleError), (unknown_run_exception, MergeRejected)], ids=["rule", "merge"]
)
async def test_scan_increment_errors_propagate_without_writes(exceptions: Any, error: type[Exception]) -> None:
    """Agent 出错或片段校验不过时照常抛出 (节点错误, 由 runner 记入 run_error), 不写执行记录与例外."""
    deps = scanning_deps(exceptions)
    with pytest.raises(error):
        await GraphNodes(deps, 2).scan_increment(node_state(deps), xero_engagement(), GEN05, FIRST)
    assert [o for o in deps.output_store.rows if isinstance(o, (ProcedureRun, AuditException))] == []


async def at_correction(deps: Any, misstatements: list[Any], **changes: Any) -> Started:
    """主图停在 await_correction 等更正通知 (见模块 docstring); 闸门之外另在 p3_reassess 之前停下."""
    pack = gen05_pack(deps)
    state = node_state(deps, misstatements=misstatements, **changes)
    versions = [m.model_copy(update={"version": v}) for m in misstatements for v in range(1, m.version + 1)]
    stored = [*state["procedure_runs"], *versions]
    runner = await start_at_fan_in(deps, state, pack, ["p3_reassess"], stored)
    return Started(runner, deps.output_store, deps.engagement_store)  # type: ignore[arg-type]


async def test_round1_scans_notice_entries_before_fin09() -> None:
    """验收 2, 3: D-CN1 (AJE-X01, AJE-X05) 生效后 verify_correction 先做 RUN-GEN-05-2 (entry_nos 为这两笔, 0 条例外),
    再执行 FIN-09 (等复核); GEN-05 仍是第 1 版 completed."""
    deps = scanning_deps()
    s = await at_correction(deps, round1_communicated())
    await submit_and_wait(s, notice())

    assert rule_calls(deps) == [("RUN-GEN-05-2", FIRST)]
    assert rule_calls(deps, FIN09) == [("RUN-FIN-09-1", None)]
    run = latest(s, ProcedureRun, "RUN-GEN-05-2")
    assert (run.trigger, run.status) == (RunTrigger.incremental, RunStatus.succeeded)
    assert [x for x in s.output_store.rows if isinstance(x, AuditException)] == []
    gen05 = latest(s, Procedure, GEN05)
    assert (gen05.version, gen05.status) == (1, ProcedureStatus.completed)
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert [w["procedure_id"] for w in waiting_payloads(snapshot)] == [FIN09]


async def test_increment_exception_stays_open_and_joins_fin09() -> None:
    """验收 7: 第 1 轮增量命中 AJE-X01 的例外为 open, 只有第 1 版 (不经 GEN-02 归因); FIN-09 把它放进匹配该凭证的
    错报 (第 1 条) 判断的规则例外, 第 2 条 (匹配 AJE-X05) 没有."""
    deps = scanning_deps(x01_hit)
    s = await at_correction(deps, round1_communicated())
    await submit_and_wait(s, notice())

    stored = [x for x in s.output_store.rows if isinstance(x, AuditException)]
    assert [(x.id, x.version, x.disposition) for x in stored] == [("X-GEN-05-2-R02-1", 1, ExceptionDisposition.open)]
    judgments = [x for x in s.output_store.rows if isinstance(x, Judgment) and x.procedure_id == FIN09]
    assert [j.exception_ids for j in judgments] == [["X-GEN-05-2-R02-1"], []]


@pytest.mark.parametrize("entry_nos", [SECOND, []], ids=["listed", "empty"])
async def test_round2_scans_only_new_entries(entry_nos: list[str]) -> None:
    """验收 3: 第 1 轮已扫过 AJE-X01, AJE-X05 (RUN-GEN-05-2): 第 2 轮通知 D-CN2 生效后 RUN-GEN-05-3 只扫 AJE-X02 至
    X04; 通知的凭证号为空 (载入全部未载入的) 时同样只扫这三笔 (可载入的全部减去已扫过的), 之后执行 FIN-09."""
    deps = scanning_deps(entries=ALL)
    history = {
        "procedure_runs": [finished(make_run(GEN05)), incremental(2, FIRST), finished(make_run(FIN09))],
        "correction_notices": [notice()],
        "judgments": [*fin03_state()["judgments"], *round2_sources()],
    }
    s = await at_correction(deps, round2_communicated(), **history)
    ids = [m.id for m in round2_communicated()]
    await submit_and_wait(s, notice("D-CN2", misstatement_ids=ids, adjustment_entry_nos=entry_nos))

    assert rule_calls(deps) == [("RUN-GEN-05-3", SECOND)]
    assert rule_calls(deps, FIN09) == [("RUN-FIN-09-2", None)]
