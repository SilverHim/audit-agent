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
    MaterialityCandidates,
    OpinionType,
    Phase,
    ProcedureStatus,
    ProfileSource,
    ReportStatus,
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
    after_fan_in,
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
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    make_deps,
    make_graph_nodes,
    make_procedure,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


# 各闸门的呈报材料 ID, 决定须绑定其第 1 版. MATERIALS 为各闸门材料的样例 (闸门路由测试用)
TARGETS = {
    GateId.ACC_01: "SCORECARD",
    GateId.PLN_01_03: "MATERIALITY",
    GateId.FIN_01: "SCHEDULE",
    GateId.RPT_01: "REPORT-1",
}
MATERIALS: dict[GateId, dict[str, Any]] = {
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

    assert (await app.aget_state(config)).next == ("p0_intel",)
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

    assert (await app.aget_state(config)).next == ("p0_intel",)


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


def test_after_fan_in_completes_when_all_done() -> None:
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": DONE, "REV-03": DONE, "REV-02": DONE})

    assert after_fan_in(nodes)(state) == "completion"


def test_after_fan_in_raises_when_awaiting_without_task() -> None:
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": AWAITING, "REV-03": DONE, "REV-02": DONE})

    with pytest.raises(ScheduleStuck, match="REV-08 为 awaiting_human"):
        after_fan_in(nodes)(state)


def test_after_fan_in_raises_when_planned_cannot_be_dispatched() -> None:
    """REV-02 等的 REV-08 在等人却没有待决任务: 永远派不出, 报错并说明卡在哪."""
    nodes = _p2_nodes()
    state = _p2_state(nodes, {"REV-08": AWAITING, "REV-03": DONE, "REV-02": PLANNED})

    with pytest.raises(ScheduleStuck, match=r"REV-02 等 REV-08 \(awaiting_human\)"):
        after_fan_in(nodes)(state)


# ── R09 (BASE-07): 闸门材料缺失时不放行 ──


@pytest.mark.parametrize("gate", [GateId.PLN_01_03, GateId.FIN_01, GateId.RPT_01])
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


# 基线功能集下的主图 (R75 验收 8): 写死现状. 各功能组加节点与闸门时这张表不变, 新节点只在功能开启时出现
BASELINE_NODES = {
    "__start__",
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
    ("__start__", "p0_intel", False, None),
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
    ("gate_acc01", "p0_intel", True, "rerun"),
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
    ("p0_intel", "gate_acc01", False, None),
    ("p1_analytics", "gate_p1_risk", False, None),
    ("p1_await_data", "p1_ingest", False, None),
    ("p1_ingest", "fan_in", True, None),
    ("p1_ingest", "p1_analytics", True, None),
    ("report_draft", "gate_rpt01", False, None),
    ("schedule", "fan_in", True, None),
    ("schedule", "fieldwork", True, None),
]


def test_baseline_graph_structure() -> None:
    """R75 验收 8: 基线功能集下 compile_graph 的节点集合, 边与条件边去向逐一同现状, 静态中断为 4 个闸门节点."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=NUMBAT_FEATURES)
    app = compile_graph(make_graph_nodes(deps), InMemorySaver())
    graph = app.get_graph()
    assert set(graph.nodes) == BASELINE_NODES and len(graph.nodes) == 20
    edges = sorted((e.source, e.target, e.conditional, e.data) for e in graph.edges)
    assert edges == BASELINE_EDGES
    assert list(app.interrupt_before_nodes) == GATES == ["gate_acc01", "gate_p1_risk", "gate_fin", "gate_rpt01"]
