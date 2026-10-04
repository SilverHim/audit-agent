"""R30 恢复补做: 闸门决定已写入 Store 而检查点没跟上 (写检查点失败, 或进程在两者之间中断) 时, recover 与 resume
从 Store 与该决定重建效果, 以该闸门节点身份写入检查点后继续 (接口文档第 7 节 recover 规则第 2 条)."""

from datetime import timedelta
from typing import Any

import pytest

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AuditObjective,
    AuditReportVersion,
    GateVerdict,
    OpinionType,
    Phase,
    ProcedureStatus,
    ReportStatus,
    RiskItem,
    RiskLevel,
    TrailEntry,
    TrailKind,
)
from backend.orchestration.decisions import acc01_effects
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from tests.fakes import ENGAGEMENT, FakeTrail, wait_idle
from tests.test_decisions import Started, acc01_decision, at_pln_gate, edit, pln_decision, started
from tests.test_fin_gate import at_fin_gate, fin_decision
from tests.test_rpt_gate import at_rpt_gate, reject_and_redraft, rpt_decision

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)


def lose_checkpoint_write(s: Started) -> None:
    """下一次以闸门身份写检查点失败 (之后恢复正常): 决定与效果已提交 Store, 留痕已写, 检查点停在闸门前."""
    app = s.runner.app
    real = app.aupdate_state

    async def broken(*args: Any, **kwargs: Any) -> Any:
        app.aupdate_state = real  # type: ignore[method-assign]
        raise RuntimeError("检查点写入失败")

    app.aupdate_state = broken  # type: ignore[method-assign]


async def submit_lost(s: Started, decision: Any) -> None:
    """提交决定, 写检查点时失败: submit 抛出, Store 领先检查点."""
    lose_checkpoint_write(s)
    with pytest.raises(RuntimeError, match="检查点写入失败"):
        await s.submit.submit(EID, decision)
    assert not s.runner.is_running(EID)


async def written_at(s: Started, next_node: str) -> dict[str, Any]:
    """补做写入的检查点 (越过闸门后下一步为 next_node) 的状态."""
    history = [h async for h in s.runner.app.aget_state_history(CONFIG) if h.next == (next_node,)]
    return history[0].values


def transitions(s: Started) -> list[tuple[str, str, str]]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [
        (e.payload["id"], e.payload["from"], e.payload["to"]) for e in trail.entries if e.kind is TrailKind.transition
    ]


def decision_keys(s: Started) -> list[str | None]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e.idempotency_key for e in trail.entries if e.kind is TrailKind.decision]


async def test_acc01_approve_caught_up_by_resume() -> None:
    """验收 1: ACC-01 approve 已提交 (ACC-01 completed, 阶段 P1), 检查点仍停在闸门且没有该决定:
    resume 以闸门身份补写后继续, 走到 PLN-01/03 闸门; 之后提交不再报 RESUME_REQUIRED."""
    s = await started()
    await submit_lost(s, acc01_decision(GateVerdict.approve))
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_acc01",) and snapshot.values["gate_decisions"] == []
    overview = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)
    assert overview.run_error is not None and "RESUME_REQUIRED" in overview.run_error

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    values = await written_at(s, "p1_ingest")
    assert [d.id for d in values["gate_decisions"]] == ["D-1"]
    assert next(p for p in values["procedure_pack"] if p.id == "ACC-01").status is ProcedureStatus.completed
    assert values["phase"] is Phase.P1_Planning
    assert await s.next_nodes() == ("gate_p1_risk",) and s.runner.run_error(EID) is None
    await s.submit.submit(EID, pln_decision(GateVerdict.reject))  # 不报 RESUME_REQUIRED
    await wait_idle(s.runner, EID)


async def test_acc01_reject_caught_up_then_declined_without_put() -> None:
    """验收 2: 拒绝承接已提交 (Declined 且 locked): recover 补做后走 declined 到 END, 期间不写 Store."""
    s = await started()
    await submit_lost(s, acc01_decision(GateVerdict.reject))
    commits = len(s.output_store.commits)

    runner = GraphRunner(s.runner.app, s.runner.deps)  # 重启
    await runner.recover()
    await wait_idle(runner, EID)

    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == () and snapshot.values["phase"] is Phase.Declined
    assert runner.run_error(EID) is None
    assert len(s.output_store.commits) == commits


async def test_pln_approve_rebuilds_materiality_and_takes_store_risks() -> None:
    """验收 3: PLN-01/03 approve (改一项风险等级) 已提交: 重要性三项按决定重算; 风险清单与审计目标取 Store 最新版;
    补做后进 assemble_pack."""
    s = await started()
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(s.runner, EID)
    await submit_lost(s, pln_decision(GateVerdict.approve, edits=[edit()]))
    edited = await s.output_store.get(EID, RiskItem, "RISK-CASH-EXI")
    assert edited is not None and (edited.version, edited.level) == (2, RiskLevel.significant)

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    values = await written_at(s, "assemble_pack")
    assert (
        values["materiality_cents"],
        values["performance_materiality_cents"],
        values["clearly_trivial_cents"],
    ) == (12000000, 9000000, 600000)
    assert next(r for r in values["risk_register"] if r.id == "RISK-CASH-EXI") == edited
    objectives = {o.id: o for o in await s.output_store.latest(EID, AuditObjective)}
    assert values["audit_objectives"] == [objectives[o.id] for o in values["audit_objectives"]]
    pack = {p.id: p.status for p in values["procedure_pack"]}
    assert (pack["PLN-01"], pack["PLN-03"]) == (ProcedureStatus.completed, ProcedureStatus.completed)
    assert values["phase"] is Phase.P1_Planning  # 阶段由 assemble_pack 推进


async def test_fin01_approve_caught_up() -> None:
    """验收 4: FIN-01 approve 保留意见已提交: 补做写意见类型与阶段 P4, 继续出报告, 停在 RPT-01 闸门."""
    s = await at_fin_gate()
    await submit_lost(s, fin_decision(GateVerdict.approve))

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    values = await written_at(s, "report_draft")
    assert (values["opinion_route"], values["phase"]) == (OpinionType.qualified, Phase.P4_Reporting)
    assert await s.next_nodes() == ("gate_rpt01",)


async def test_rpt01_reject_caught_up() -> None:
    """验收 4: RPT-01 退回已提交: 补做后回 report_draft 出 REPORT-2, 再停在闸门."""
    s = await at_rpt_gate()
    await submit_lost(s, rpt_decision(GateVerdict.reject))

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    assert await s.next_nodes() == ("gate_rpt01",)
    assert await s.output_store.get(EID, AuditReportVersion, "REPORT-2") is not None


async def test_rpt01_approve_caught_up_then_archived_without_put() -> None:
    """验收 5: 签字已提交 (REPORT-2 v2 signed, Archived 且 locked): recover 补做, 签字版进检查点, 走 archive 到 END,
    期间不写 Store."""
    s = await at_rpt_gate()
    await reject_and_redraft(s)
    await submit_lost(s, rpt_decision(GateVerdict.approve, id="D-RPT-APPROVE", target_id="REPORT-2"))
    commits = len(s.output_store.commits)

    runner = GraphRunner(s.runner.app, s.runner.deps)  # 重启
    await runner.recover()
    await wait_idle(runner, EID)

    snapshot = await runner.app.aget_state(CONFIG)
    assert snapshot.next == () and runner.run_error(EID) is None
    assert [(r.id, r.version, r.status) for r in snapshot.values["report_versions"]][-1] == (
        "REPORT-2",
        2,
        ReportStatus.signed,
    )
    assert snapshot.values["phase"] is Phase.Archived
    assert len(s.output_store.commits) == commits


async def test_lost_trail_is_backfilled_once() -> None:
    """验收 6 与补 (F2): 决定提交后留痕也没写成 (进程在提交后即中断): 补做补写 decision 留痕与 ACC-01, 阶段的
    transition 留痕; 再补做一次不重复."""
    s = await started()
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    real = trail.record

    async def crash(entry: Any) -> str:
        raise RuntimeError("进程中断")

    trail.record = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="进程中断"):
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))
    trail.record = real  # type: ignore[method-assign]
    assert decision_keys(s) == []

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)
    await s.runner.resume(EID)  # 已补做, 停在闸门: 不动
    await wait_idle(s.runner, EID)

    assert decision_keys(s) == ["D-1"]
    assert transitions(s)[:3] == [
        ("ACC-04", "planned", "completed"),
        ("ACC-01", "planned", "completed"),
        (EID, "P0_Acceptance", "P1_Planning"),
    ]
    assert len(transitions(s)) == len(set(transitions(s)))


async def test_decision_for_other_gate_is_not_caught_up() -> None:
    """Store 中检查点没有的决定不是当前闸门的 (Store 与检查点对不上): 不补做, 记入 run_error, 检查点不动."""
    s = await started()
    await s.output_store.put(EID, [pln_decision(GateVerdict.reject)], actor="U-1")
    before = (await s.runner.app.aget_state(CONFIG)).config

    await s.runner.recover()

    assert "无法补做" in (s.runner.run_error(EID) or "")
    assert (await s.runner.app.aget_state(CONFIG)).config == before
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-2"))
    assert e.value.code == "RESUME_REQUIRED"


@pytest.mark.parametrize(
    "failed_key",
    ["D-1", f"{EID}:transition:procedure:ACC-01:v2", f"{EID}:transition:engagement:{EID}:P1_Planning"],
)
async def test_trail_failure_during_catch_up_is_retried(failed_key: str) -> None:
    """提交后留痕没写成, 补做时某一行留痕又写失败: 补做中止, 决定仍不在检查点里 (记 run_error);
    再次重启 recover 补齐这一行 (只一行) 后继续推进."""
    s = await started()
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    real = trail.record

    async def crash(entry: TrailEntry) -> str:
        raise RuntimeError("进程中断")

    trail.record = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="进程中断"):
        await s.submit.submit(EID, acc01_decision(GateVerdict.approve))

    async def fail_one(entry: TrailEntry) -> str:
        if entry.idempotency_key == failed_key:
            raise RuntimeError("补做留痕失败")
        return await real(entry)

    trail.record = fail_one  # type: ignore[method-assign]
    runner = GraphRunner(s.runner.app, s.runner.deps)
    await runner.recover()
    assert "补做留痕失败" in (runner.run_error(EID) or "") and not runner.is_running(EID)
    assert (await runner.app.aget_state(CONFIG)).values["gate_decisions"] == []

    trail.record = real  # type: ignore[method-assign]
    runner = GraphRunner(s.runner.app, s.runner.deps)
    await runner.recover()
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(CONFIG)).next == ("gate_p1_risk",) and runner.run_error(EID) is None
    assert [e.idempotency_key for e in trail.entries].count(failed_key) == 1


@pytest.mark.parametrize(
    ("setup", "factory", "gate_node"),
    [
        (started, acc01_decision, "gate_acc01"),
        (at_pln_gate, pln_decision, "gate_p1_risk"),
        (at_fin_gate, fin_decision, "gate_fin"),
        (at_rpt_gate, rpt_decision, "gate_rpt01"),
    ],
)
async def test_insufficient_info_caught_up_stays_at_gate(setup: Any, factory: Any, gate_node: str) -> None:
    """四道闸门的信息不足补做后留在闸门, 不在后台继续, 程序包与阶段不变, 不写 Store; 再 resume 检查点不变."""
    s = await setup()
    before = await s.runner.app.aget_state(CONFIG)
    decision = factory(GateVerdict.insufficient_info, id="D-INFO")
    await submit_lost(s, decision)
    commits = len(s.output_store.commits)

    await s.runner.resume(EID)

    assert not s.runner.is_running(EID)
    after = await s.runner.app.aget_state(CONFIG)
    assert (after.next, after.values["gate_decisions"][-1]) == ((gate_node,), decision)
    assert (after.values["procedure_pack"], after.values["phase"]) == (
        before.values["procedure_pack"],
        before.values["phase"],
    )
    await s.runner.resume(EID)
    assert (await s.runner.app.aget_state(CONFIG)).config == after.config
    assert len(s.output_store.commits) == commits


async def test_missing_decisions_caught_up_in_decision_order() -> None:
    """Store 领先多个决定 (正常提交时校验会拦住, 这里直接写入), 返回顺序与决定时间相反: 按决定时间先补信息不足,
    再补拒绝承接, 最后走 declined 到 END, 不写 Store."""
    s = await started()
    first = acc01_decision(GateVerdict.insufficient_info, id="D-FIRST")
    last = acc01_decision(GateVerdict.reject, id="D-LAST", decided_at=first.decided_at + timedelta(seconds=1))
    pack = (await s.runner.app.aget_state(CONFIG)).values["procedure_pack"]
    await s.output_store.put(EID, [last, first], actor="U-1")
    effects = acc01_effects(pack, last)
    await s.output_store.put(EID, [p for p in effects["procedure_pack"] if p.id == "ACC-01"], actor="A1")
    await s.engagement_store.update(EID, phase=Phase.Declined, locked=True)
    commits = len(s.output_store.commits)

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    after = await s.runner.app.aget_state(CONFIG)
    assert after.next == () and [d.id for d in after.values["gate_decisions"]] == ["D-FIRST", "D-LAST"]
    assert len(s.output_store.commits) == commits
