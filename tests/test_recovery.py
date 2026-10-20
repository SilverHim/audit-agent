"""恢复补做: 决定已写入 Store 而检查点没跟上 (写检查点失败, 或进程在两者之间中断) 时, recover 与 resume 先补做再继续
(接口文档第 7 节 recover 规则第 2 条). R30: 闸门决定从 Store 与该决定重建效果, 以该闸门节点身份写入检查点.
R31: 客户画像版本写入检查点后回 p0_intel; 复核决定以 Command(resume=决定 ID) 交给等待中的节点."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from langgraph.types import Command

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditObjective,
    AuditReportVersion,
    Decision,
    DisputeResolution,
    GateVerdict,
    Judgment,
    OpinionType,
    Phase,
    ProcedureRun,
    ProcedureStatus,
    ProfileSource,
    ProposedAction,
    ReportStatus,
    ReviewRecord,
    ReviewTask,
    RiskItem,
    RiskLevel,
    TaskKind,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.orchestration.gates import acc01_effects
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.presentation import record_presentation
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    TYPO_PROFILE,
    FakeTrail,
    ScriptA3,
    ScriptA4,
    finished,
    make_runner,
    preset_rerun,
    profile_update,
    wait_idle,
)
from tests.test_collab import CASH_DRAFT, COLLAB, CONSULT, disputed
from tests.test_decisions import (
    FIXED_PROFILE,
    Started,
    acc01_decision,
    at_pln_gate,
    edit,
    pln_decision,
    started,
    stop_after_submit,
)
from tests.test_dispute import JID, resolve
from tests.test_fin_gate import at_fin_gate, fin_decision
from tests.test_flow import REVIEWS, approve_materiality, start_numbat
from tests.test_presentation import DWELL_TASK, SHOWN_AT, presented_ba01
from tests.test_rerun import CrashAfterCommit, FlakyRegistry, failed_at_p0
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


# ── R31: 客户画像与复核决定 ──


async def test_profile_update_caught_up_then_reruns_acc04() -> None:
    """R31 验收 1: 更新客户资料已提交 (项目采用第 2 版), 检查点仍记第 1 版: resume 写入第 2 版, 回 p0_intel 重跑,
    出 RUN-ACC-04-2 与 SCORECARD v2; 提交时已写的留痕不重复."""
    s = await started(TYPO_PROFILE)
    lose_checkpoint_write(s)
    with pytest.raises(RuntimeError, match="检查点写入失败"):
        await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_acc01",) and snapshot.values["client_profile_version"] == 2
    assert await s.output_store.get(EID, ProcedureRun, "RUN-ACC-04-2") is not None
    scorecard = await s.output_store.get(EID, AcceptanceScorecard, "SCORECARD")
    assert scorecard is not None and (scorecard.version, scorecard.client_profile_version) == (2, 2)
    assert decision_keys(s) == ["CLIENT-001-v2"]


async def test_other_engagement_profile_does_not_trigger_catch_up() -> None:
    """R31 验收 1: 同客户的另一项目把全局最新画像升到第 2 版, 本项目采用的仍是第 1 版: 不补做, 检查点不动."""
    s = await started(TYPO_PROFILE)
    await s.engagement_store.save_profile(FIXED_PROFILE, source=ProfileSource.update, updated_by="U-1")
    before = (await s.runner.app.aget_state(CONFIG)).config

    await s.runner.recover()

    assert not s.runner.is_running(EID) and s.runner.run_error(EID) is None
    assert (await s.runner.app.aget_state(CONFIG)).config == before


async def test_review_decision_at_await_events_resumed() -> None:
    """R31 验收 2: P2 等复核时复核决定已提交, 进程在恢复节点前中断: resume 以 Command(resume=决定 ID) 交给
    await_events, 决定进检查点; 再 recover 一次, 检查点与 Store 不变 (验收 3)."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    launch = s.runner.launch
    stop_after_submit(s)
    decision = REVIEWS[0]
    await s.submit.submit(EID, decision)
    s.runner.launch = launch  # type: ignore[method-assign]
    assert decision.id not in {r.id for r in (await s.runner.app.aget_state(CONFIG)).values["review_records"]}

    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)

    snapshot = await s.runner.app.aget_state(CONFIG)
    assert decision.id in {r.id for r in snapshot.values["review_records"]}
    assert snapshot.next == ("await_events",) and s.runner.run_error(EID) is None
    rows = list(s.output_store.rows)
    await s.runner.recover()
    assert not s.runner.is_running(EID)
    assert (await s.runner.app.aget_state(CONFIG)).config == snapshot.config and s.output_store.rows == rows


async def test_a7_upheld_with_draft_and_consultation_recovered() -> None:
    """R150 验收 7 (collab 打开, 沿用 R31): 争议变体 a7_upheld 带错报草稿与咨询记录 (D-ARB) 写 Store 后, 进程在
    恢复节点前中断 (检查点未写): recover 后由 await_events 读回 -H1 (建议 misstatement), M-D-ARB 与任务 v2, 流程
    继续 (再停在等 REV-03 的复核); decision 留痕 1 行, 不重复."""
    s, _ = await disputed(COLLAB)
    launch = s.runner.launch
    stop_after_submit(s)
    decision = resolve(
        DisputeResolution.a7_upheld, misstatement_draft=CASH_DRAFT, consulted=True, consultation_record=CONSULT
    )
    await s.submit.submit(EID, decision)
    s.runner.launch = launch  # type: ignore[method-assign]
    assert decision.id not in {r.id for r in (await s.runner.app.aget_state(CONFIG)).values["review_records"]}

    await s.runner.recover()
    await wait_idle(s.runner, EID)

    snapshot = await s.runner.app.aget_state(CONFIG)
    values = snapshot.values
    assert snapshot.next == ("await_events",) and s.runner.run_error(EID) is None
    assert decision.id in {r.id for r in values["review_records"]}
    [human] = [j for j in values["judgments"] if j.id == f"{JID}-H1"]
    assert (human.proposed_action, human.misstatement_draft) == (ProposedAction.misstatement, CASH_DRAFT)
    assert [m.judgment_id for m in values["misstatements"] if m.id == "M-D-ARB"] == [human.id]
    [task] = [t for t in values["review_tasks"] if t.id == "T-J-CSH-02-1-BA-01-v1"]
    assert (task.version, task.status) == (2, TaskStatus.decided)
    assert decision_keys(s).count(decision.id) == 1


async def test_review_dwell_kept_when_trail_backfilled() -> None:
    """R153 验收 6 (collab 打开): 10:00:00 呈递, 10:01:35 提交 D-CSH02, 决定写 Store 后 decision 留痕没写成 (进程中断,
    检查点也未跟上): recover 补写的留痕 dwell_seconds 95 与 presentation_id 取记录上存的, 不重算 (中断后又补了一次
    10:01:00 的呈递, 重算会得 35); 留痕 1 行, 再 recover 不重复."""
    s, shown = await presented_ba01(COLLAB)
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    real = trail.record

    async def crash(entry: TrailEntry) -> str:
        if entry.kind is TrailKind.decision:
            raise RuntimeError("进程中断")
        return await real(entry)

    trail.record = crash  # type: ignore[method-assign]
    [decision] = [r for r in REVIEWS if r.id == "D-CSH02"]
    with pytest.raises(RuntimeError, match="进程中断"):
        await s.submit.submit(EID, decision.model_copy(update={"decided_at": SHOWN_AT + timedelta(seconds=95)}))
    trail.record = real  # type: ignore[method-assign]
    assert "D-CSH02" not in decision_keys(s)
    task = await s.output_store.get(EID, ReviewTask, DWELL_TASK)
    assert task is not None
    await record_presentation(trail, EID, task, "U-1", SHOWN_AT + timedelta(seconds=60))

    for _ in range(2):
        await s.runner.recover()
        await wait_idle(s.runner, EID)

    assert s.runner.run_error(EID) is None
    [entry] = [e for e in trail.entries if e.kind is TrailKind.decision and e.idempotency_key == "D-CSH02"]
    assert (entry.payload["dwell_seconds"], entry.payload["presentation_id"]) == (95, shown)


async def test_rerun_decision_in_p0_node_resumed_with_trail_backfilled() -> None:
    """R31 验收 2, 4 与补 (F2): ACC-04 失败等人时 rerun 决定已写入 Store, 没写留痕, 节点未恢复: recover 补写
    decision 留痕与 ACC-04 awaiting_human→executing, 以该决定恢复节点, 重跑成功停在 ACC-01 闸门; 再 resume 不重复."""
    runner, store, _ = await failed_at_p0()
    await preset_rerun(store, (await store.latest(EID, ReviewTask))[0], "D-1")
    s = Started(runner, store, runner.deps.engagement_store)  # type: ignore[arg-type]

    await runner.recover()
    await wait_idle(runner, EID)
    await runner.resume(EID)
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(CONFIG)).next == ("gate_acc01",)
    assert await store.get(EID, ProcedureRun, "RUN-ACC-04-2") is not None
    assert decision_keys(s) == ["D-1"]
    acc04 = [(b, a) for pid, b, a in transitions(s) if pid == "ACC-04"]
    assert acc04 == [("planned", "awaiting_human"), ("awaiting_human", "executing"), ("executing", "completed")]


async def test_profile_trail_failure_during_catch_up_is_retried() -> None:
    """更新客户资料提交后留痕没写成, 补做时这行留痕又写失败: 画像版本仍不进检查点 (记 run_error);
    再次重启 recover 补齐留痕 (只一行) 后回 p0_intel 重跑."""
    s = await started(TYPO_PROFILE)
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    real = trail.record

    async def crash(entry: TrailEntry) -> str:
        raise RuntimeError("进程中断")

    trail.record = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="进程中断"):
        await s.submit.update_client_profile(EID, *profile_update(FIXED_PROFILE), updated_by="U-1")
    runner = GraphRunner(s.runner.app, s.runner.deps)
    await runner.recover()  # 留痕仍写不了
    assert "进程中断" in (runner.run_error(EID) or "")
    assert (await runner.app.aget_state(CONFIG)).values["client_profile_version"] == 1

    trail.record = real  # type: ignore[method-assign]
    runner = GraphRunner(s.runner.app, s.runner.deps)
    await runner.recover()
    await wait_idle(runner, EID)

    snapshot = await runner.app.aget_state(CONFIG)
    assert (snapshot.next, snapshot.values["client_profile_version"]) == (("gate_acc01",), 2)
    assert decision_keys(s) == ["CLIENT-001-v2"]


async def crash_after_rerun(failures: int) -> tuple[GraphRunner, FlakyRegistry, Any]:
    """ACC-04 第 1 次失败等人; rerun 决定 D-1 只写入 Store (节点未恢复); 第一次 recover 以 D-1 恢复节点, 重跑的
    RUN-ACC-04-2 提交后进程被杀 (节点出错, 下一次等待还没登记). failures: 登记系统前几次查询失败."""
    store = CrashAfterCommit("RUN-ACC-04-2")
    registry = FlakyRegistry(failures)
    runner, _ = make_runner(output_store=store, registry=registry)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await preset_rerun(store, (await store.latest(EID, ReviewTask))[0], "D-1")
    first = GraphRunner(runner.app, runner.deps)
    await first.recover()
    await wait_idle(first, EID)
    assert "被杀" in (first.run_error(EID) or "")
    return runner, registry, store


async def test_node_error_after_reading_decision_recovered() -> None:
    """R31-01: 以 D-1 恢复后重跑成功, 但写检查点前被杀: 再次 recover 仍以 D-1 恢复, 到 ACC-01 闸门, 不再查登记."""
    runner, registry, _ = await crash_after_rerun(failures=1)

    restarted = GraphRunner(runner.app, runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    snapshot = await runner.app.aget_state(CONFIG)
    assert (snapshot.next, restarted.run_error(EID)) == (("gate_acc01",), None)
    assert [r.id for r in snapshot.values["review_records"]] == ["D-1"] and registry.calls == 2


async def submit_rerun(
    runner: GraphRunner, store: Any, decision_id: str, decided_at: datetime = NOW, subject_id: str | None = None
) -> None:
    """经正式入口对当前待决的失败任务 (有多个时取 subject_id 那个) 提交 rerun, 等后台停下. decided_at 可故意
    倒置, 验证恢复不靠决定时间排序."""
    pending = await store.latest(EID, ReviewTask, status=TaskStatus.pending)
    [task] = [t for t in pending if subject_id is None or t.subject_id == subject_id]
    s = Started(runner, store, runner.deps.engagement_store)
    await s.submit.submit(
        EID,
        ReviewRecord(
            id=decision_id,
            task_id=task.id,
            target_version=task.target_version,
            decision=Decision.rerun,
            reason="重新执行",
            reviewed_by="U-1",
            decided_at=decided_at,
        ),
    )
    await wait_idle(runner, EID)


async def restarted(runner: GraphRunner, entry: str) -> GraphRunner:
    """共用检查点与存储重启, 以 recover 或 resume 恢复, 等后台停下; 恢复不出错."""
    again = GraphRunner(runner.app, runner.deps)
    if entry == "recover":
        await again.recover()
    else:
        await again.resume(EID)
    await wait_idle(again, EID)
    assert again.run_error(EID) is None
    return again


@pytest.mark.parametrize("entry", ["recover", "resume"])
async def test_node_error_before_second_wait_does_not_reuse_decision(entry: str) -> None:
    """R31-01, R31-02: 经正式入口提交 D-1, 重跑又失败, 第 2 次等待登记前被杀 (节点带着错误). 再次 recover 或 resume
    后停在第 2 次等待, 节点不再带错误, 不再查登记; 之后正式提交 D-2 照常重跑到 ACC-01 闸门."""
    store = CrashAfterCommit("RUN-ACC-04-2")
    registry = FlakyRegistry(failures=2)
    runner, _ = make_runner(output_store=store, registry=registry)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit_rerun(runner, store, "D-1")
    assert "被杀" in (runner.run_error(EID) or "")

    runner = await restarted(runner, entry)

    snapshot = await runner.app.aget_state(CONFIG)
    assert waiting_payloads(snapshot) == [
        {"awaiting": "human", "procedure_id": "ACC-04", "run_id": "RUN-ACC-04-2", "records": ["D-1"]}
    ]
    assert not any(t.error for t in snapshot.tasks) and registry.calls == 2
    await submit_rerun(runner, store, "D-2")
    final = await runner.app.aget_state(CONFIG)
    assert (final.next, runner.run_error(EID), registry.calls) == (("gate_acc01",), None, 3)
    assert [r.id for r in final.values["review_records"]] == ["D-1", "D-2"]


async def test_node_error_after_second_decision_redelivers_in_order() -> None:
    """R31-02: 节点已读回 D-1, 停在第 2 次等待; 正式提交 D-2 后重跑又失败, 第 3 次等待登记前被杀. recover 复制检查点后
    依次交回 D-1, D-2, 停在第 3 次等待且不带错误, 不再查登记; 之后提交 D-3 到 ACC-01 闸门."""
    store = CrashAfterCommit("RUN-ACC-04-3")
    registry = FlakyRegistry(failures=3)
    runner, _ = make_runner(output_store=store, registry=registry)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await submit_rerun(runner, store, "D-1")
    await submit_rerun(runner, store, "D-2")
    assert "被杀" in (runner.run_error(EID) or "")

    runner = await restarted(runner, "recover")

    snapshot = await runner.app.aget_state(CONFIG)
    assert waiting_payloads(snapshot) == [
        {"awaiting": "human", "procedure_id": "ACC-04", "run_id": "RUN-ACC-04-3", "records": ["D-1", "D-2"]}
    ]
    assert not any(t.error for t in snapshot.tasks) and registry.calls == 3
    await submit_rerun(runner, store, "D-3")
    final = await runner.app.aget_state(CONFIG)
    assert (final.next, registry.calls) == (("gate_acc01",), 4)
    assert [r.id for r in await store.latest(EID, ProcedureRun)] == [f"RUN-ACC-04-{n}" for n in range(1, 5)]


async def test_node_error_across_two_procedures_redelivers_in_order() -> None:
    """R31-02: p1_analytics 内 PLN-07 失败一次 (D-PLN07 重跑), PLN-01 失败两次; 提交 D-PLN01-1 后 PLN-01 重跑又失败,
    下一次等待登记前被杀. recover 依次交回 D-PLN07, D-PLN01-1, 停在 PLN-01 第 2 次等待, Agent 不多执行;
    之后提交 D-PLN01-2 到 PLN-01/03 闸门."""
    store = CrashAfterCommit("RUN-PLN-01-2")
    seen: list[tuple[str, int]] = []

    def agents(deps: Any) -> dict[str, Any]:
        def pln07(ctx: Any) -> Any:
            seen.append(("PLN-07", ctx.run.run_no))
            if ctx.run.run_no == 1:
                raise RuntimeError("分析失败")
            return {"procedure_runs": [finished(ctx.run)]}

        def pln01(ctx: Any) -> Any:
            seen.append(("PLN-01", ctx.run.run_no))
            if ctx.run.run_no <= 2:
                raise RuntimeError("重要性失败")
            return {"procedure_runs": [finished(ctx.run)]}

        return {"PLN-07": ScriptA4(deps, pln07), "PLN-01": ScriptA4(deps, pln01)}

    runner, _ = make_runner(output_store=store, agents=agents)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await Started(runner, store, runner.deps.engagement_store).submit.submit(  # type: ignore[arg-type]
        EID, acc01_decision(GateVerdict.approve, id="D-GATE")
    )
    await wait_idle(runner, EID)
    await submit_rerun(runner, store, "D-PLN07", NOW + timedelta(seconds=2))  # 决定时间倒置
    await submit_rerun(runner, store, "D-PLN01-1", NOW + timedelta(seconds=1))
    assert "被杀" in (runner.run_error(EID) or "")

    runner = await restarted(runner, "recover")

    snapshot = await runner.app.aget_state(CONFIG)
    assert waiting_payloads(snapshot) == [
        {"awaiting": "human", "procedure_id": "PLN-01", "run_id": "RUN-PLN-01-2", "records": ["D-PLN07", "D-PLN01-1"]}
    ]
    assert not any(t.error for t in snapshot.tasks)
    await submit_rerun(runner, store, "D-PLN01-2")
    assert (await runner.app.aget_state(CONFIG)).next == ("gate_p1_risk",)
    assert seen == [("PLN-07", 1), ("PLN-07", 2), ("PLN-01", 1), ("PLN-01", 2), ("PLN-01", 3)]


async def test_await_events_error_after_reading_decision_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """R31-01: await_events 读回复核决定后读 Store (判断) 临时失败 (节点出错): 再次 recover 仍以该决定恢复,
    决定进检查点."""
    s = await start_numbat()
    await approve_materiality(s)
    await wait_idle(s.runner, EID)
    launch = s.runner.launch
    stop_after_submit(s)
    decision = REVIEWS[0]
    await s.submit.submit(EID, decision)
    s.runner.launch = launch  # type: ignore[method-assign]
    latest = s.output_store.latest
    failed: list[bool] = []

    async def flaky_latest(eid: str, kind: type[Any], **filters: Any) -> list[Any]:
        if kind is Judgment and not failed:  # 判断只有等复核的节点读回时才读, 恢复补做不读
            failed.append(True)
            raise RuntimeError("读回时临时失败")
        return await latest(eid, kind, **filters)

    monkeypatch.setattr(s.output_store, "latest", flaky_latest)
    await s.runner.resume(EID)
    await wait_idle(s.runner, EID)
    assert "临时失败" in (s.runner.run_error(EID) or "")

    restarted = GraphRunner(s.runner.app, s.runner.deps)
    await restarted.recover()
    await wait_idle(restarted, EID)

    assert restarted.run_error(EID) is None
    assert decision.id in {r.id for r in (await s.runner.app.aget_state(CONFIG)).values["review_records"]}


@pytest.mark.parametrize("cut", ["after_copy", "after_first_redelivery"])
async def test_catch_up_interrupted_after_copy_continues(cut: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """R31-03: 节点已读回 D-1, D-2, 正式提交 D-3 后重跑又失败, 下一次等待登记前被杀. 补做复制检查点后, 或交回 D-1 后
    又中断 (记 run_error); 再次 recover 从 Store 重算顺序接着交, 停在第 4 次等待 (records D-1, D-2, D-3) 且不带错误,
    不再查登记, Store 不变."""
    store = CrashAfterCommit("RUN-ACC-04-4")
    registry = FlakyRegistry(failures=4)
    runner, _ = make_runner(output_store=store, registry=registry)
    await runner.start(EID)
    await wait_idle(runner, EID)
    for n in (1, 2, 3):  # 决定时间倒置: D-1 最晚
        await submit_rerun(runner, store, f"D-{n}", NOW + timedelta(seconds=4 - n))
    assert "被杀" in (runner.run_error(EID) or "")
    rows = list(store.rows)
    app = runner.app
    update, invoke = app.aupdate_state, app.ainvoke

    async def cut_after_copy(*args: Any, **kwargs: Any) -> Any:
        result = await update(*args, **kwargs)
        if kwargs.get("as_node") == "__copy__":
            raise RuntimeError("复制后中断")
        return result

    async def cut_after_first(graph_input: Any, *args: Any, **kwargs: Any) -> Any:
        result = await invoke(graph_input, *args, **kwargs)
        if isinstance(graph_input, Command) and graph_input.resume == "D-1":
            raise RuntimeError("交回 D-1 后中断")
        return result

    if cut == "after_copy":
        monkeypatch.setattr(app, "aupdate_state", cut_after_copy)
    else:
        monkeypatch.setattr(app, "ainvoke", cut_after_first)
    interrupted = GraphRunner(app, runner.deps)
    await interrupted.recover()
    await wait_idle(interrupted, EID)
    assert "中断" in (interrupted.run_error(EID) or "")
    monkeypatch.setattr(app, "aupdate_state", update)
    monkeypatch.setattr(app, "ainvoke", invoke)

    await restarted(runner, "recover")

    after = await app.aget_state(CONFIG)
    assert waiting_payloads(after) == [
        {"awaiting": "human", "procedure_id": "ACC-04", "run_id": "RUN-ACC-04-4", "records": ["D-1", "D-2", "D-3"]}
    ]
    assert not any(t.error for t in after.tasks)
    assert registry.calls == 4 and store.rows == rows


async def test_group_rerun_recovered_in_run_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q17: DOC-04 第 1 次 CON-001, CON-002 失败, 对 CON-002 提交 D-Z 整组重跑; 第 2 次 CON-001 又失败, 提交 D-A;
    第 3 次 CON-001 仍失败, 写检查点前被杀. 决定时间倒置 (D-A 早于 D-Z), 补做复制后又中断; 再次 recover 按执行轮次
    交回 D-Z, D-A, 停在第 3 次等待且不带错误, 不多执行; D-Z 的留痕列出一并关闭的 CON-001 任务."""
    store = CrashAfterCommit("RUN-DOC-04-3")
    seen: list[tuple[int, list[str]]] = []

    def agents(deps: Any) -> dict[str, Any]:
        def doc04(ctx: Any) -> Any:
            seen.append((ctx.run.run_no, list(ctx.procedure.rerun_subject_ids)))
            run = finished(ctx.run)
            failing = {1: ["CON-001", "CON-002"], 2: ["CON-001"], 3: ["CON-001"]}.get(ctx.run.run_no, [])
            tasks = [
                deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW, subject_id=subject)
                for subject in failing
            ]
            return {"procedure_runs": [run], "review_tasks": tasks}

        return {"DOC-04": ScriptA3(deps, doc04)}

    runner, _ = make_runner(output_store=store, agents=agents)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await Started(runner, store, runner.deps.engagement_store).submit.submit(  # type: ignore[arg-type]
        EID, acc01_decision(GateVerdict.approve, id="D-GATE")
    )
    await wait_idle(runner, EID)
    await submit_rerun(runner, store, "D-Z", NOW + timedelta(seconds=2), subject_id="CON-002")
    await submit_rerun(runner, store, "D-A", NOW + timedelta(seconds=1))
    assert "被杀" in (runner.run_error(EID) or "")
    update = runner.app.aupdate_state

    async def cut_after_copy(*args: Any, **kwargs: Any) -> Any:
        result = await update(*args, **kwargs)
        if kwargs.get("as_node") == "__copy__":
            raise RuntimeError("复制后中断")
        return result

    monkeypatch.setattr(runner.app, "aupdate_state", cut_after_copy)
    interrupted = GraphRunner(runner.app, runner.deps)
    await interrupted.recover()
    assert "复制后中断" in (interrupted.run_error(EID) or "")
    monkeypatch.setattr(runner.app, "aupdate_state", update)

    await restarted(runner, "recover")

    snapshot = await runner.app.aget_state(CONFIG)
    assert waiting_payloads(snapshot) == [
        {"awaiting": "human", "procedure_id": "DOC-04", "run_id": "RUN-DOC-04-3", "records": ["D-Z", "D-A"]}
    ]
    assert not any(t.error for t in snapshot.tasks)
    assert seen == [(1, []), (2, ["CON-001", "CON-002"]), (3, ["CON-001"])]
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.idempotency_key == "D-Z"]
    assert entry.payload["closed_task_ids"] == ["T-RUN-DOC-04-1-CON-001-v1"]


async def test_group_rerun_trail_backfilled_with_closed_tasks() -> None:
    """Q17: DOC-04 第 1 次两份合同失败, rerun 决定只写入 Store (留痕没写成, 节点未恢复): recover 补写的留痕列出
    一并关闭的另一份合同的任务, 节点整组重跑后走到重要性闸门."""
    seen: list[list[str]] = []

    def agents(deps: Any) -> dict[str, Any]:
        def doc04(ctx: Any) -> Any:
            seen.append(list(ctx.procedure.rerun_subject_ids))
            run = finished(ctx.run)
            failing = ["CON-001", "CON-002"] if ctx.run.run_no == 1 else []
            tasks = [
                deps.queue.make_task(TaskKind.failed, run, "DOC-04", run.id, NOW, subject_id=subject)
                for subject in failing
            ]
            return {"procedure_runs": [run], "review_tasks": tasks}

        return {"DOC-04": ScriptA3(deps, doc04)}

    runner, store = make_runner(agents=agents)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await Started(runner, store, runner.deps.engagement_store).submit.submit(  # type: ignore[arg-type]
        EID, acc01_decision(GateVerdict.approve, id="D-GATE")
    )
    await wait_idle(runner, EID)
    pending = sorted(await store.latest(EID, ReviewTask, status=TaskStatus.pending), key=lambda t: t.id)
    await preset_rerun(store, pending[1], "D-1")

    await runner.recover()
    await wait_idle(runner, EID)

    assert (await runner.app.aget_state(CONFIG)).next == ("gate_p1_risk",) and runner.run_error(EID) is None
    assert seen == [[], ["CON-001", "CON-002"]]
    trail = runner.deps.trail
    assert isinstance(trail, FakeTrail)
    [entry] = [e for e in trail.entries if e.idempotency_key == "D-1"]
    assert entry.payload["closed_task_ids"] == ["T-RUN-DOC-04-1-CON-001-v1"]
