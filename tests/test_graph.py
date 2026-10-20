"""主图: 验证拓扑, 四道静态中断, 闸门决定经 aupdate_state(as_node=闸门) 越过闸门. p0_intel 已实装, 其余节点尚为占位."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import AgentDeps
from backend.contracts.models import (
    AuditReportVersion,
    ClientProfile,
    Engagement,
    GateDecision,
    GateId,
    GateVerdict,
    Judgment,
    MaterialityCandidates,
    Misstatement,
    MisstatementStatus,
    OpinionType,
    Phase,
    PlanStatus,
    ProcedureStatus,
    ProfileSource,
    ReportStatus,
    RerunRequest,
    ReviewTask,
    TargetType,
    TaskKind,
    UncorrectedSchedule,
)
from backend.contracts.state import EngagementState, initial_state
from backend.core.config import Settings, get_settings
from backend.orchestration.checkpoint import checkpoint_serde, open_checkpointer
from backend.orchestration.gates import acc01_effects
from backend.orchestration.graph import (
    GATE_NODES,
    GATES,
    ScheduleStuck,
    after_communicate,
    after_fan_in,
    after_final_review,
    after_triage,
    after_verify,
    compile_graph,
    gate_route,
    route_schedule,
)
from backend.orchestration.nodes import GraphNodes
from backend.orchestration.runner import GraphRunner, fixed_procedures
from backend.orchestration.scheduler import build_schedule
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    NUMBAT_FEATURES,
    NUMBAT_PROFILE,
    PLANNING_FEATURES,
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    audit_plan,
    make_deps,
    make_graph_nodes,
    make_procedure,
    make_runner,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


# 各闸门的呈报材料 ID, 决定须绑定其第 1 版. MATERIALS 为各闸门材料的样例 (闸门路由测试用)
TARGETS = {
    GateId.ACC_01: "SCORECARD",
    GateId.PLN_01_03: "MATERIALITY",
    GateId.FIN_01: "SCHEDULE",
    GateId.RPT_01: "REPORT-1",
    GateId.PLN_15: "PLAN-1",
}
MATERIALS: dict[GateId, dict[str, Any]] = {
    GateId.PLN_15: {"audit_plans": [audit_plan()]},
    GateId.FIN_01: {
        "uncorrected_schedule": UncorrectedSchedule(
            id="SCHEDULE",
            items=[],
            excluded_trivial_ids=[],
            total_cents=0,
            materiality_cents=12000000,
            candidate_opinion=OpinionType.unqualified,
        )
    },
    GateId.RPT_01: {
        "report_versions": [
            AuditReportVersion(
                id="REPORT-1", revision=1, opinion_type=OpinionType.unqualified, sections={}, judgment_refs=[]
            )
        ]
    },
}


# 遍历时随决定一并写入状态的值: PLN-01/03 通过写入重要性三项 (同 DecisionSubmit, FIN-04 按它们取参数).
# 各闸门的材料由节点产出
WALK_WRITES: dict[GateId, dict[str, Any]] = {
    GateId.PLN_01_03: {
        "materiality_cents": 12000000,
        "performance_materiality_cents": 9000000,
        "clearly_trivial_cents": 600000,
    },
}


def _decision(n: int, gate: GateId, verdict: GateVerdict, target_version: int = 1) -> GateDecision:
    return GateDecision(
        id=f"D-{n}",
        gate=gate,
        target_id=TARGETS[gate],
        target_version=target_version,
        decision=verdict,
        reason=None if verdict is GateVerdict.approve else "test",
        decided_by="U-1",
        decided_at=NOW,
    )


async def test_walk_through_all_gates() -> None:
    await _walk(InMemorySaver(serde=checkpoint_serde()), "ENG-2026-001")


@pytest.mark.slow
async def test_walk_through_all_gates_postgres() -> None:
    """需先 docker compose up -d db 并建检查点表; 用 .env 中的 DATABASE_URL (应用角色)."""
    settings = Settings(CHECKPOINT_BACKEND="postgres")
    async with open_checkpointer(settings) as checkpointer:
        await _walk(checkpointer, f"ENG-TEST-{datetime.now(UTC):%H%M%S%f}")


def _fake_deps(engagement: Engagement, store: FakeEngagementStore | None = None) -> AgentDeps:
    """B 的存储与登记 mock 未就绪, 用 tests/fakes.py 的假实现. 审计产出存储预置固定集程序第 1 版, 同 runner.start."""
    registry = FakeRegistry({"68900042015": NUMBAT_ABN}, {"900042015": NUMBAT_ASIC})
    output_store = FakeOutputStore()
    deps = make_deps(registry, FakeTrail(), store or FakeEngagementStore([NUMBAT_PROFILE], [engagement]), output_store)
    output_store.rows_of(engagement.id).extend(fixed_procedures(deps.library))  # 按项目写入, 连库测试的项目编号是随机的
    return deps


Graph = CompiledStateGraph[EngagementState]


async def _run_to_acc01(profile: ClientProfile = NUMBAT_PROFILE) -> tuple[Graph, RunnableConfig, FakeEngagementStore]:
    """新项目从 START 跑到 ACC-01 闸门前停下. 返回图, 配置与项目存储 (用来更新客户画像)."""
    store = FakeEngagementStore([profile], [ENGAGEMENT])
    deps = _fake_deps(ENGAGEMENT, store)
    app = compile_graph(make_graph_nodes(deps, presets=False), InMemorySaver(serde=checkpoint_serde()))
    config = GraphRunner.config(ENGAGEMENT.id)
    await app.ainvoke(initial_state(ENGAGEMENT, fixed_procedures(deps.library), profile.version), config)
    return app, config, store


async def _decide_acc01(app: Graph, config: RunnableConfig, d: GateDecision) -> None:
    """模拟 DecisionSubmit.submit: 把 ACC-01 决定及其效果以闸门节点的身份写入状态."""
    pack = (await app.aget_state(config)).values["procedure_pack"]
    await app.aupdate_state(config, acc01_effects(pack, d), as_node="gate_acc01")


async def _update_profile(app: Graph, config: RunnableConfig, store: FakeEngagementStore, p: ClientProfile) -> None:
    """模拟 DecisionSubmit.update_client_profile: 保存新版本画像, 再以闸门节点的身份写入最新版本号."""
    saved = await store.save_profile(p, source=ProfileSource.update, updated_by="U-1")
    await app.aupdate_state(config, {"client_profile_version": saved.version}, as_node="gate_acc01")


async def test_p0_runs_acc04_and_stops_at_acc01() -> None:
    app, config, _ = await _run_to_acc01()

    snapshot = await app.aget_state(config)
    assert snapshot.next == ("gate_acc01",)
    values = snapshot.values
    assert values["acceptance_scorecard"].id == "SCORECARD"
    assert [r.id for r in values["procedure_runs"]] == ["RUN-ACC-04-1"]
    pack = {p.id: p for p in values["procedure_pack"]}  # 程序编号 -> 程序
    assert pack["ACC-04"].status == ProcedureStatus.completed
    assert pack["ACC-04"].version == 2
    assert pack["ACC-01"].status == ProcedureStatus.planned
    assert values["phase"] is Phase.P0_Acceptance  # 检查点读回的顶层枚举仍是枚举, 不是字符串


async def _walk(checkpointer: BaseCheckpointSaver[str], eid: str) -> None:
    settings = get_settings()
    engagement = Engagement(
        id=eid,
        client_id="CLIENT-001",
        period_start=date(2025, 7, 1),
        period_end=date(2026, 6, 30),
        dataset="demo",
        owner_id="U-1",
        client_profile_version=1,
        phase=Phase.P0_Acceptance,
        created_at=NOW,
        updated_at=NOW,
    )
    deps = _fake_deps(engagement)
    app = compile_graph(make_graph_nodes(deps, settings.PARALLEL_CAP, presets=False), checkpointer)
    config = GraphRunner.config(engagement.id)
    await app.ainvoke(initial_state(engagement, fixed_procedures(deps.library), NUMBAT_PROFILE.version), config)

    steps = [
        (GateId.ACC_01, GateVerdict.approve, "p1_await_data", "gate_p1_risk"),
        (GateId.PLN_01_03, GateVerdict.approve, "assemble_pack", "gate_fin"),
        (GateId.FIN_01, GateVerdict.approve, "report_draft", "gate_rpt01"),
        (GateId.RPT_01, GateVerdict.reject, "report_draft", "gate_rpt01"),
        (GateId.RPT_01, GateVerdict.approve, "archive", None),
    ]
    for n, (gate, verdict, after_decision, next_gate) in enumerate(steps):
        snapshot = await app.aget_state(config)
        assert snapshot.next == (GATE_NODES[gate],)
        decision = _decision(n, gate, verdict)
        if gate is GateId.RPT_01:  # 报告由 report_draft 产出, 退回后出下一修订: 决定绑定当前报告
            current = max(snapshot.values["report_versions"], key=lambda r: r.revision)
            decision = decision.model_copy(update={"target_id": current.id, "target_version": current.version})
        values = {"gate_decisions": [decision], **WALK_WRITES.get(gate, {})}
        await app.aupdate_state(config, values, as_node=GATE_NODES[gate])
        assert (await app.aget_state(config)).next == (after_decision,)
        await app.ainvoke(None, config)
        assert (await app.aget_state(config)).next == ((next_gate,) if next_gate else ())

    final = (await app.aget_state(config)).values
    assert [d.id for d in final["gate_decisions"]] == ["D-0", "D-1", "D-2", "D-3", "D-4"]
    assert isinstance(final["gate_decisions"][0], GateDecision)


# ── 4 ACC-01 闸门: 通过, 驳回, 信息不足留在闸门, 客户画像更新后重跑 ──


async def test_acc01_approve_goes_to_p1() -> None:
    app, config, _ = await _run_to_acc01()

    await _decide_acc01(app, config, _decision(0, GateId.ACC_01, GateVerdict.approve))

    assert (await app.aget_state(config)).next == ("p1_await_data",)
    await app.ainvoke(None, config)
    snapshot = await app.aget_state(config)
    assert snapshot.next == ("gate_p1_risk",)  # P1 节点尚为占位, 一路走到下一道闸门
    assert snapshot.values["phase"] == Phase.P1_Planning


async def test_acc01_reject_declines() -> None:
    app, config, _ = await _run_to_acc01()

    await _decide_acc01(app, config, _decision(0, GateId.ACC_01, GateVerdict.reject))

    assert (await app.aget_state(config)).next == ("declined",)
    await app.ainvoke(None, config)
    snapshot = await app.aget_state(config)
    assert snapshot.next == ()  # 流程结束
    assert snapshot.values["phase"] == Phase.Declined


async def test_acc01_insufficient_info_waits_at_gate() -> None:
    app, config, _ = await _run_to_acc01()

    await _decide_acc01(app, config, _decision(0, GateId.ACC_01, GateVerdict.insufficient_info))

    snapshot = await app.aget_state(config)
    assert snapshot.next == ("gate_acc01",)  # 不重跑, 留在闸门前等客户补资料
    assert [r.id for r in snapshot.values["procedure_runs"]] == ["RUN-ACC-04-1"]
    assert snapshot.values["acceptance_scorecard"].version == 1


async def test_acc01_profile_update_reruns_acc04() -> None:
    app, config, store = await _run_to_acc01(TYPO_PROFILE)
    first = (await app.aget_state(config)).values["acceptance_scorecard"]
    assert first.exception_ids == ["X-ACC-04-1-name_match-1"]
    await _decide_acc01(app, config, _decision(0, GateId.ACC_01, GateVerdict.insufficient_info))

    await _update_profile(app, config, store, NUMBAT_PROFILE.model_copy(update={"version": 2}))

    assert (await app.aget_state(config)).next == ("p0_inputs",)  # 先看要不要等 ASIC 摘录 (需求 R290)
    await app.ainvoke(None, config)
    snapshot = await app.aget_state(config)
    assert snapshot.next == ("gate_acc01",)  # 重跑后回到闸门前, 重新呈报
    values = snapshot.values
    assert [r.id for r in values["procedure_runs"]] == ["RUN-ACC-04-1", "RUN-ACC-04-2"]
    scorecard = values["acceptance_scorecard"]
    assert scorecard.version == 2
    assert scorecard.client_profile_version == 2
    assert scorecard.exception_ids == []  # 名称改对后三项都通过


async def test_acc01_profile_update_without_decision_reruns() -> None:
    """审计师自己发现画像有误, 不必先点信息不足, 直接更新也会重跑."""
    app, config, store = await _run_to_acc01(TYPO_PROFILE)

    await _update_profile(app, config, store, NUMBAT_PROFILE.model_copy(update={"version": 2}))

    assert (await app.aget_state(config)).next == ("p0_inputs",)  # 先看要不要等 ASIC 摘录 (需求 R290)


async def test_acc01_decision_on_old_scorecard_is_represented() -> None:
    """画像更新重跑后评分卡已是第 2 版, 绑定第 1 版的决定视为过期, 停回闸门; 绑定第 2 版才放行."""
    app, config, store = await _run_to_acc01(TYPO_PROFILE)
    await _update_profile(app, config, store, NUMBAT_PROFILE.model_copy(update={"version": 2}))
    await app.ainvoke(None, config)

    await _decide_acc01(app, config, _decision(1, GateId.ACC_01, GateVerdict.approve, target_version=1))
    assert (await app.aget_state(config)).next == ("gate_acc01",)

    await _decide_acc01(app, config, _decision(2, GateId.ACC_01, GateVerdict.approve, target_version=2))
    assert (await app.aget_state(config)).next == ("p1_await_data",)


# ── P2 路由兜底: 状态与任务对不上时报错, 不空转 ──

NOW_TASK = datetime(2026, 10, 1, tzinfo=UTC)
DONE, PLANNED, AWAITING = ProcedureStatus.completed, ProcedureStatus.planned, ProcedureStatus.awaiting_human
MISSTATEMENT_ON = NUMBAT_FEATURES | {"misstatement"}  # 功能 misstatement 打开 (需求 R160)
TRIAGE = "misstatement_triage"


def _p2_nodes() -> GraphNodes:
    return GraphNodes(_fake_deps(ENGAGEMENT), parallel_cap=4)


def _p2_state(nodes: GraphNodes, statuses: dict[str, ProcedureStatus], pending_tasks: int = 0) -> EngagementState:
    """P2 进行中的状态: 给定几条 P2 程序的状态 (依赖取真实 YAML: REV-02 依赖 REV-03, REV-08), 调度表按程序包重建."""
    pack = [make_procedure(pid, Phase.P2_Executing, status=st) for pid, st in statuses.items()]
    state = initial_state(ENGAGEMENT, pack, NUMBAT_PROFILE.version)
    state["schedule_table"] = build_schedule(pack, nodes.deps.library, 4)
    state["review_tasks"] = [
        ReviewTask(
            id=f"T-{n}",
            kind=TaskKind.normal,
            procedure_id="REV-08",
            target_type=TargetType.Judgment,
            target_id=f"J-{n}",
            target_version=1,
            run_id="RUN-REV-08-1",
            created_at=NOW_TASK,
        )
        for n in range(pending_tasks)
    ]
    return state


def test_route_schedule_sends_ready_procedure() -> None:
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": DONE, "REV-03": DONE, "REV-02": PLANNED})

    result = route_schedule(nodes)(state)

    assert isinstance(result, list)
    assert [s.node for s in result] == ["fieldwork"]
    assert result[0].arg["procedure_ids"] == ["REV-02"]


def test_route_schedule_goes_to_fan_in_when_all_done() -> None:
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": DONE, "REV-03": DONE, "REV-02": DONE})

    assert route_schedule(nodes)(state) == "fan_in"


def test_stuck_is_reported_after_fan_in_when_unfinished_but_none_ready() -> None:
    """REV-08 等人处理却没有任务, REV-02 永远等不到前置: route_schedule 回 fan_in, 由 after_fan_in 报错, 不空转
    (需求 R272 起卡住只在 after_fan_in 一处判断)."""
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": AWAITING, "REV-03": DONE, "REV-02": PLANNED})

    assert route_schedule(nodes)(state) == "fan_in"
    with pytest.raises(ScheduleStuck, match=r"REV-02 等 REV-08 \(awaiting_human\)"):
        after_fan_in(nodes)(state)


def test_after_fan_in_dispatches_before_waiting() -> None:
    """R15 验收 1: REV-03 有待决任务, 而 DOC-06 的前置 CSH-04 已完成: 先派发 DOC-06, 不停下等复核."""
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"CSH-04": DONE, "DOC-06": PLANNED, "REV-03": AWAITING}, pending_tasks=1)

    assert after_fan_in(nodes)(state) == "schedule"


def test_after_fan_in_waits_for_pending_task() -> None:
    """R15 验收 2: 没有可派发的 (REV-02 等 REV-08 复核), 有待决任务: 等复核."""
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": AWAITING, "REV-03": DONE, "REV-02": PLANNED}, pending_tasks=1)

    assert after_fan_in(nodes)(state) == "await_events"


@pytest.mark.parametrize(("features", "settled"), [(NUMBAT_FEATURES, "completion"), (MISSTATEMENT_ON, TRIAGE)])
def test_after_fan_in_completes_when_all_done(features: frozenset[str], settled: str) -> None:
    """P2 全部结清且没有待决任务: 进完成阶段; 功能 misstatement 打开时先进错报分诊 (需求 R160 验收 3)."""
    nodes = GraphNodes(make_deps(FakeRegistry(), FakeTrail(), features=features), parallel_cap=4)
    state = _p2_state(nodes, {"REV-08": DONE, "REV-03": DONE, "REV-02": DONE})

    assert after_fan_in(nodes)(state) == settled


def test_after_fan_in_raises_when_awaiting_without_task() -> None:
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": AWAITING, "REV-03": DONE, "REV-02": DONE})

    with pytest.raises(ScheduleStuck, match="REV-08 为 awaiting_human"):
        after_fan_in(nodes)(state)


def test_after_fan_in_with_program_in_dispute() -> None:
    """R151 验收 4: 争议中 (in_dispute) 的 REV-08 有待决任务时等复核, 以它为前置的 REV-02 不派发 (只认 completed);
    没有待决任务时报 ScheduleStuck, 消息写明程序与 in_dispute; 前置卡在它上面时也写明."""
    nodes = _p2_nodes()
    disputed = ProcedureStatus.in_dispute
    state = _p2_state(nodes, {"REV-08": disputed, "REV-03": DONE, "REV-02": PLANNED}, pending_tasks=1)
    assert after_fan_in(nodes)(state) == "await_events"
    assert route_schedule(nodes)(state) == "fan_in"  # REV-02 派不出
    state = _p2_state(nodes, {"REV-08": disputed, "REV-03": DONE, "REV-02": DONE})
    with pytest.raises(ScheduleStuck, match="REV-08 为 in_dispute, 却没有待决的复核任务"):
        after_fan_in(nodes)(state)
    state = _p2_state(nodes, {"REV-08": disputed, "REV-03": AWAITING, "REV-02": DONE})
    with pytest.raises(ScheduleStuck, match="REV-08 为 in_dispute, REV-03 为 awaiting_human"):
        after_fan_in(nodes)(state)
    state = _p2_state(nodes, {"REV-08": disputed, "REV-03": DONE, "REV-02": PLANNED})
    with pytest.raises(ScheduleStuck, match=r"REV-02 等 REV-08 \(in_dispute\)"):
        after_fan_in(nodes)(state)


def test_after_fan_in_raises_when_planned_cannot_be_dispatched() -> None:
    """REV-02 等的 REV-08 在等人却没有待决任务: 永远派不出, 报错并说明卡在哪."""
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": AWAITING, "REV-03": DONE, "REV-02": PLANNED})

    with pytest.raises(ScheduleStuck, match=r"REV-02 等 REV-08 \(awaiting_human\)"):
        after_fan_in(nodes)(state)


# ── R09 (BASE-07): 闸门材料缺失时不放行 ──


@pytest.mark.parametrize("gate", [GateId.PLN_01_03, GateId.FIN_01, GateId.RPT_01, GateId.PLN_15])
def test_gate_route_represents_when_material_missing(gate: GateId) -> None:
    """有本闸门的通过决定, 但状态里没有材料 (与流程不符): 不按决定放行, 留在闸门重新呈报; 补上材料后按决定通过."""
    state = initial_state(ENGAGEMENT, [], 1)
    state["gate_decisions"] = [_decision(0, gate, GateVerdict.approve)]

    assert gate_route(gate)(state) == "represent"

    material = MATERIALS.get(gate) or {"materiality_candidates": MaterialityCandidates(id="MATERIALITY", candidates=[])}
    state.update(material)  # type: ignore[typeddict-item]
    assert gate_route(gate)(state) == "approve"


# ── R26: RPT-01 签字后放行 ──


@pytest.mark.parametrize(
    ("status", "version", "verdict", "route"),
    [
        (ReportStatus.signed, 2, GateVerdict.approve, "approve"),  # 签字出了 v2, 决定绑定 v1
        (ReportStatus.draft, 2, GateVerdict.approve, "represent"),  # 未签字的新版本: 决定过期
        (ReportStatus.signed, 3, GateVerdict.approve, "represent"),  # 不是决定那一版的下一版
        (ReportStatus.signed, 2, GateVerdict.reject, "represent"),  # 只有通过决定会签字
    ],
)
def test_gate_route_rpt01_after_signing(status: ReportStatus, version: int, verdict: GateVerdict, route: str) -> None:
    """RPT-01 通过使报告出签字版, 决定绑定的是签字前一版: 仍放行到 archive; 其余版本不符仍重新呈报."""
    state = initial_state(ENGAGEMENT, [], 1)
    state["gate_decisions"] = [_decision(0, GateId.RPT_01, verdict)]
    [report] = MATERIALS[GateId.RPT_01]["report_versions"]
    state["report_versions"] = [report.model_copy(update={"version": version, "status": status})]

    assert gate_route(GateId.RPT_01)(state) == route


# ── R205: PLN-15 批准后放行 (同 RPT-01 签字) ──


@pytest.mark.parametrize(
    ("status", "version", "verdict", "route"),
    [
        (PlanStatus.approved, 2, GateVerdict.approve, "approve"),  # 验收 6: 批准出了 v2, 决定绑定 v1
        (PlanStatus.draft, 1, GateVerdict.approve, "approve"),  # 决定绑定当前版 (效果写入之前)
        (PlanStatus.draft, 2, GateVerdict.approve, "represent"),  # 未批准的新版本: 决定过期
        (PlanStatus.approved, 3, GateVerdict.approve, "represent"),  # 不是决定那一版的下一版
        (PlanStatus.draft, 1, GateVerdict.reject, "reject"),
        (PlanStatus.draft, 1, GateVerdict.insufficient_info, "represent"),
    ],
)
def test_gate_route_pln15_after_approval(status: PlanStatus, version: int, verdict: GateVerdict, route: str) -> None:
    """PLN-15 通过使计划出 approved 新版本, 决定绑定的是批准前一版: 仍放行到 schedule; 其余版本不符仍重新呈报;
    驳回走 reject 出口 (回 plan_draft), 信息不足重新呈报."""
    state = initial_state(ENGAGEMENT, [], 1)
    state["gate_decisions"] = [_decision(0, GateId.PLN_15, verdict)]
    state["audit_plans"] = [audit_plan(1, version, status)]

    assert gate_route(GateId.PLN_15)(state) == route


@pytest.mark.parametrize("verdict", list(GateVerdict))
def test_gate_route_pln15_represents_new_revision(verdict: GateVerdict) -> None:
    """需求 R206 验收 5: 驳回后起草了 PLAN-2 v1 (PLAN-1 置 superseded), 最后一条 PLN-15 决定仍绑定 PLAN-1 v1: 不论哪种
    决定都已过期, 重新呈报 (回 gate_p1_plan 呈报 PLAN-2), 不按旧决定放行或再驳回."""
    state = initial_state(ENGAGEMENT, [], 1)
    state["gate_decisions"] = [_decision(0, GateId.PLN_15, verdict)]
    state["audit_plans"] = [audit_plan(1, 2, PlanStatus.superseded), audit_plan(2)]

    assert gate_route(GateId.PLN_15)(state) == "represent"


# 功能 planning 打开时 (需求 R205 验收 1) 比基线多的边; 基线的 assemble_pack → schedule 改为 → plan_draft
PLANNING_EDGES = [
    ("assemble_pack", "plan_draft", False, None),
    ("gate_p1_plan", "gate_p1_plan", True, "represent"),
    ("gate_p1_plan", "plan_draft", True, "reject"),
    ("gate_p1_plan", "schedule", True, "approve"),
    ("plan_draft", "gate_p1_plan", False, None),
]


# 基线功能集下的主图 (R75 验收 8): 写死现状. 各功能组加节点与闸门时这张表不变, 新节点只在功能开启时出现
BASELINE_NODES = {
    "__start__",
    "p0_inputs",
    "p0_intel",
    "gate_acc01",
    "p1_await_data",
    "p1_ingest",
    "p1_analytics",
    "gate_p1_risk",
    "assemble_pack",
    "schedule",
    "fieldwork",
    "fan_in",
    "await_events",
    "await_data",
    "completion",
    "gate_fin",
    "report_draft",
    "gate_rpt01",
    "archive",
    "declined",
    "__end__",
}
# (起点, 终点, 是否条件边, 闸门出口名)
BASELINE_EDGES = [
    ("__start__", "p0_inputs", False, None),
    ("archive", "__end__", False, None),
    ("assemble_pack", "schedule", False, None),
    ("await_data", "fan_in", False, None),
    ("await_events", "fan_in", False, None),
    ("completion", "gate_fin", False, None),
    ("declined", "__end__", False, None),
    ("fan_in", "await_data", True, None),
    ("fan_in", "await_events", True, None),
    ("fan_in", "completion", True, None),
    ("fan_in", "p1_ingest", True, None),
    ("fan_in", "schedule", True, None),
    ("fieldwork", "fan_in", False, None),
    ("gate_acc01", "declined", True, "reject"),
    ("gate_acc01", "gate_acc01", True, "represent"),
    ("gate_acc01", "p0_inputs", True, "rerun"),
    ("gate_acc01", "p1_await_data", True, "approve"),
    ("gate_fin", "completion", True, "reject"),
    ("gate_fin", "gate_fin", True, "represent"),
    ("gate_fin", "report_draft", True, "approve"),
    ("gate_p1_risk", "assemble_pack", True, "approve"),
    ("gate_p1_risk", "gate_p1_risk", True, "represent"),
    ("gate_p1_risk", "p1_analytics", True, "reject"),
    ("gate_rpt01", "archive", True, "approve"),
    ("gate_rpt01", "gate_rpt01", True, "represent"),
    ("gate_rpt01", "report_draft", True, "reject"),
    ("p0_inputs", "p0_inputs", True, None),
    ("p0_inputs", "p0_intel", True, None),
    ("p0_intel", "gate_acc01", False, None),
    ("p1_analytics", "gate_p1_risk", False, None),
    ("p1_await_data", "p1_ingest", False, None),
    ("p1_ingest", "fan_in", True, None),
    ("p1_ingest", "p1_analytics", True, None),
    ("report_draft", "gate_rpt01", False, None),
    ("schedule", "fan_in", True, None),
    ("schedule", "fieldwork", True, None),
]


# 功能 misstatement 打开时 (需求 R160 验收 1, 2) 比基线多的边: 完成阶段的错报闭环; 基线的 fan_in → completion 改为
# → misstatement_triage, gate_fin 驳回由 → completion 改为 → summarize
P3_NODES = {TRIAGE, "communicate", "await_correction", "verify_correction", "p3_reassess", "summarize", "final_review"}
MISSTATEMENT_REPLACED = [("fan_in", "completion", True, None), ("gate_fin", "completion", True, "reject")]
MISSTATEMENT_EDGES = [
    ("await_correction", "verify_correction", False, None),
    ("communicate", "await_correction", True, None),
    ("communicate", "verify_correction", True, None),
    ("fan_in", TRIAGE, True, None),
    ("final_review", "completion", True, None),
    ("final_review", TRIAGE, True, None),
    ("gate_fin", "summarize", True, "reject"),
    (TRIAGE, "communicate", True, None),
    (TRIAGE, TRIAGE, True, None),
    ("p3_reassess", "summarize", False, None),
    ("summarize", "final_review", False, None),
    ("verify_correction", "await_correction", True, None),
    ("verify_correction", "communicate", True, None),
    ("verify_correction", TRIAGE, True, None),
    ("verify_correction", "p3_reassess", True, None),
]


def test_baseline_graph_structure() -> None:
    """R75 验收 8: 基线功能集下 compile_graph 的节点集合, 边与条件边去向逐一同现状, 静态中断为 4 个闸门节点."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=NUMBAT_FEATURES)
    app = compile_graph(make_graph_nodes(deps), InMemorySaver())
    graph = app.get_graph()
    assert set(graph.nodes) == BASELINE_NODES and len(graph.nodes) == 21
    edges = sorted((e.source, e.target, e.conditional, e.data) for e in graph.edges)
    assert edges == BASELINE_EDGES
    assert list(app.interrupt_before_nodes) == GATES == ["gate_acc01", "gate_p1_risk", "gate_fin", "gate_rpt01"]


def test_misstatement_graph_structure() -> None:
    """需求 R160 验收 1, 2: 功能 misstatement 打开时多出完成阶段的 8 个节点与 MISSTATEMENT_EDGES, P2 结清改去
    misstatement_triage, FIN-01 驳回改回 summarize; 静态中断仍是 4 个闸门. 其余同基线."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=MISSTATEMENT_ON)
    app = compile_graph(make_graph_nodes(deps), InMemorySaver())
    graph = app.get_graph()
    assert set(graph.nodes) == BASELINE_NODES | P3_NODES
    edges = sorted((e.source, e.target, e.conditional, e.data) for e in graph.edges)
    baseline = [e for e in BASELINE_EDGES if e not in MISSTATEMENT_REPLACED]
    assert edges == sorted([*baseline, *MISSTATEMENT_EDGES])
    assert list(app.interrupt_before_nodes) == GATES


def test_planning_graph_structure() -> None:
    """需求 R205 验收 1: 功能 planning 打开时 gate_p1_risk 通过 → assemble_pack → plan_draft → gate_p1_plan;
    gate_p1_plan 的出口 approve → schedule, reject → plan_draft, represent 回自身; 静态中断加 gate_p1_plan (按登记
    顺序排在 gate_p1_risk 之后). 其余节点与边同基线 (基线下的主图见 test_baseline_graph_structure, 不变)."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=PLANNING_FEATURES)
    app = compile_graph(make_graph_nodes(deps), InMemorySaver())
    graph = app.get_graph()
    assert set(graph.nodes) == BASELINE_NODES | {"plan_draft", "gate_p1_plan"}
    edges = sorted((e.source, e.target, e.conditional, e.data) for e in graph.edges)
    baseline = [e for e in BASELINE_EDGES if e[:2] != ("assemble_pack", "schedule")]
    assert edges == sorted([*baseline, *PLANNING_EDGES])
    assert list(app.interrupt_before_nodes) == ["gate_acc01", "gate_p1_risk", "gate_p1_plan", "gate_fin", "gate_rpt01"]


def test_make_runner_compiles_by_features() -> None:
    """审核意见 R205-03: make_runner 另给 interrupt_before 时, 主图与闸门照样按功能集取 (同 compile_graph): 打开
    planning 时有 plan_draft 与 gate_p1_plan (静态中断), 另加的节点排在闸门之后; 基线下同现状."""
    runner, _ = make_runner(features=PLANNING_FEATURES, interrupt_before=["fieldwork"])
    assert {"plan_draft", "gate_p1_plan"} <= set(runner.app.get_graph().nodes)
    assert list(runner.app.interrupt_before_nodes) == [
        "gate_acc01",
        "gate_p1_risk",
        "gate_p1_plan",
        "gate_fin",
        "gate_rpt01",
        "fieldwork",
    ]
    baseline, _ = make_runner(interrupt_before=["fieldwork"])
    assert list(baseline.app.interrupt_before_nodes) == [*GATES, "fieldwork"]
    assert "plan_draft" not in baseline.app.get_graph().nodes


# ── 需求 R160 验收 3: 完成阶段的路由纯函数 ──


def p3_misstatement(oid: str, status: MisstatementStatus, **fields: Any) -> Misstatement:
    """一条错报: 不是 identified 的都已分类, 其余字段按状态补齐 (模型校验)."""
    base: dict[str, Any] = {"classification_judgment_id": None if status is MisstatementStatus.identified else "J-1"}
    if status in (MisstatementStatus.communicated, MisstatementStatus.uncorrected):
        base |= {"communicated_at": NOW_TASK, "management_response": "OK"}
    if status is MisstatementStatus.uncorrected:
        base["uncorrected_basis"] = "refused"
    if status is MisstatementStatus.verifying:
        base |= {"communicated_at": NOW_TASK, "correction_notice_id": "D-CN"}
    if status is MisstatementStatus.corrected:
        base |= {"verified_by_judgment_id": "J-V", "corrected_cents": 100, "residual_cents": 0}
    return Misstatement.model_validate(
        {
            "id": oid,
            "judgment_id": "J-0",
            "exception_id": None,
            "review_record_id": "D-0",
            "type": "factual",
            "account": "Revenue",
            "assertion": "Cutoff",
            "amount_cents": 100,
            "direction": "overstated",
            "status": status,
        }
        | base
        | fields
    )


NEW = p3_misstatement("M-NEW", MisstatementStatus.identified)  # 完成阶段新认定, 未分类
CLASSIFIED = p3_misstatement("M-CLS", MisstatementStatus.identified, classification_judgment_id="J-FIN-03-1-M-CLS")
COMMUNICATED = p3_misstatement("M-COM", MisstatementStatus.communicated)
RECOMMUNICATE = p3_misstatement("M-RE", MisstatementStatus.communicated, needs_recommunication=True)
VERIFYING = p3_misstatement("M-VER", MisstatementStatus.verifying)
SETTLED_ONES = [
    p3_misstatement("M-COR", MisstatementStatus.corrected),
    p3_misstatement("M-UNC", MisstatementStatus.uncorrected),
]


def with_misstatements(*misstatements: Misstatement) -> EngagementState:
    return initial_state(ENGAGEMENT, [], NUMBAT_PROFILE.version) | {"misstatements": list(misstatements)}  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("route", "misstatements", "expected"),
    [
        (after_triage, [NEW, CLASSIFIED], TRIAGE),
        (after_triage, [CLASSIFIED, *SETTLED_ONES], "communicate"),
        (after_triage, [], "communicate"),
        (after_communicate, [CLASSIFIED, COMMUNICATED], "await_correction"),
        (after_communicate, [CLASSIFIED, *SETTLED_ONES], "verify_correction"),
        (after_verify, [NEW, RECOMMUNICATE, COMMUNICATED], TRIAGE),  # 先分类
        (after_verify, [RECOMMUNICATE, COMMUNICATED], "communicate"),  # 再重新沟通
        (after_verify, [COMMUNICATED, *SETTLED_ONES], "await_correction"),  # 再等其余的更正
        (after_verify, [CLASSIFIED, *SETTLED_ONES], "p3_reassess"),
        (after_final_review, [NEW, *SETTLED_ONES], TRIAGE),
        (after_final_review, SETTLED_ONES, "completion"),
    ],
)
def test_p3_routes(route: Any, misstatements: list[Misstatement], expected: str) -> None:
    """表驱动: 回 misstatement_triage 的条件都是有未分类的 identified 错报 (完成阶段新认定的); after_communicate 看有无
    communicated; after_verify 依次看未分类, 须重新沟通, 仍在等更正."""
    assert route(with_misstatements(*misstatements)) == expected


def test_after_verify_raises_while_verifying_left() -> None:
    """验证之后仍有 verifying 的错报 (FIN-09 没有给出结果): 抛 ScheduleStuck, 列出错报, 不把未验证的错报带进累计."""
    with pytest.raises(ScheduleStuck, match="M-VER"):
        after_verify(with_misstatements(VERIFYING, *SETTLED_ONES))


def test_routes_ignore_misstatements_of_superseded_runs() -> None:
    """路由只看当前结果 (同 FIN-03, FIN-04): 来源判断的执行被过期重跑取代, 其错报 (未分类的, 已沟通的, 须重新沟通的,
    验证中的) 都不算, 各路由走 "没有这类错报" 的出口."""
    request = RerunRequest(
        id="RR-1",
        procedure_ids=["REV-02"],
        superseded_run_ids=["RUN-REV-02-1"],
        data_version=2,
        requested_by="U-1",
        requested_at=NOW_TASK,
    )
    source = Judgment.model_construct(id="J-0", run_id="RUN-REV-02-1", procedure_id="REV-02")
    stale = with_misstatements(NEW, COMMUNICATED, RECOMMUNICATE, VERIFYING)
    state = stale | {"rerun_requests": [request], "judgments": [source]}
    routes = (after_triage, after_communicate, after_verify, after_final_review)
    assert [route(state) for route in routes] == [  # type: ignore[arg-type]
        "communicate",
        "verify_correction",
        "p3_reassess",
        "completion",
    ]
