"""StateQuery: 读检查点判断停在哪道闸门, 组装闸门材料与项目总览. 依赖用 tests/fakes.py 的假实现."""

from datetime import UTC, datetime

import pytest

from backend.contracts.models import (
    AuditPlan,
    ClientProfile,
    GateDecision,
    GateId,
    GateVerdict,
    PlanItem,
    PlanStatus,
    ProcedureStatus,
    ProfileSource,
    ReviewTask,
    RiskLevel,
    TargetType,
    TaskKind,
    TaskStatus,
)
from backend.contracts.state import initial_state
from backend.contracts.views import PlanMaterial, PlanningMaterial, PlanView
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.gates import acc01_effects, spec_of
from backend.orchestration.query import GraphStateQuery, current_gate, plan_view
from backend.orchestration.runner import GraphRunner
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_OBJECTIVES,
    NUMBAT_PROFILE,
    NUMBAT_RISKS,
    PLANNING_FEATURES,
    TYPO_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    api_client,
    audit_plan,
    engagement_terms,
    make_deps,
    make_runner,
    wait_idle,
)
from tests.test_decisions import Agents, inventory
from tests.test_runner import BrokenOutputStore, BrokenRegistry

EID = ENGAGEMENT.id
NOW = datetime(2026, 10, 3, tzinfo=UTC)


async def started(
    profile: ClientProfile = NUMBAT_PROFILE, agents: Agents | None = None
) -> tuple[GraphRunner, GraphStateQuery, FakeOutputStore]:
    """新项目启动并在后台跑到 ACC-01 闸门前. 返回 runner, query 与审计产出存储. agents 见 make_runner."""
    runner, store = make_runner(engagement_store=FakeEngagementStore([profile], [ENGAGEMENT]), agents=agents)
    await runner.start(EID)
    await wait_idle(runner, EID)
    return runner, GraphStateQuery(runner.app, runner.deps, runner), store


# ── 3a 闸门材料 ──


def test_current_gate() -> None:
    """current_gate 按 (next, 状态) 取: 节点只挂一道闸门时即该闸门 (R86 起状态用来在一个节点的多道闸门间选)."""
    values = initial_state(ENGAGEMENT, [], 1)
    assert current_gate(("gate_acc01",), values) is GateId.ACC_01
    assert current_gate(("gate_p1_risk",), values) is GateId.PLN_01_03
    assert current_gate(("p0_intel",), values) is None
    assert current_gate((), values) is None


async def test_gate_is_none_before_start() -> None:
    runner, _ = make_runner()
    query = GraphStateQuery(runner.app, runner.deps, runner)

    assert await query.gate(EID) is None


async def test_gate_shows_acc01_material() -> None:
    _, query, _ = await started()

    view = await query.gate(EID)

    assert view is not None
    assert view.gate is GateId.ACC_01
    assert (view.target_id, view.target_version) == ("SCORECARD", 1)
    assert view.material.kind == "ACC-01"
    assert view.material.scorecard.version == 1
    assert view.material.exceptions == []  # Numbat 三项检查都通过
    assert view.material.client_profile == NUMBAT_PROFILE


async def test_gate_lists_scorecard_exceptions() -> None:
    _, query, _ = await started(TYPO_PROFILE)

    view = await query.gate(EID)

    assert view is not None
    assert view.material.kind == "ACC-01"
    assert [x.id for x in view.material.exceptions] == ["X-ACC-04-1-name_match-1"]
    assert view.material.client_profile.legal_name == TYPO_PROFILE.legal_name


async def test_gate_after_profile_update_shows_only_current_exceptions() -> None:
    """名称改对后 ACC-04 重跑: 状态里仍留着第 1 次的例外, 材料只列评分卡第 2 版引用的 (没有)."""
    runner, query, _ = await started(TYPO_PROFILE)
    config = GraphRunner.config(EID)
    saved = await runner.deps.engagement_store.save_profile(
        NUMBAT_PROFILE.model_copy(update={"version": 2}), source=ProfileSource.update, updated_by="U-1"
    )
    await runner.app.aupdate_state(config, {"client_profile_version": saved.version}, as_node="gate_acc01")
    await runner.app.ainvoke(None, config)

    view = await query.gate(EID)

    assert view is not None
    assert view.material.kind == "ACC-01"
    assert (view.target_id, view.target_version) == ("SCORECARD", 2)
    assert view.material.exceptions == []
    assert view.material.client_profile.version == 2


async def test_gate_is_none_after_decline() -> None:
    runner, query, _ = await started()
    config = GraphRunner.config(EID)
    pack = (await runner.app.aget_state(config)).values["procedure_pack"]
    reject = GateDecision(
        id="D-1",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=GateVerdict.reject,
        reason="客户不符合承接条件",
        decided_by="U-1",
        decided_at=NOW,
    )
    await runner.app.aupdate_state(config, acc01_effects(pack, reject), as_node="gate_acc01")
    await runner.app.ainvoke(None, config)

    assert await query.gate(EID) is None  # 已走到 declined 结束


# ── 3b 项目总览 ──


async def test_overview_at_acc01() -> None:
    runner, query, _ = await started()

    o = await query.overview(EID)

    assert o.engagement == await runner.deps.engagement_store.get(EID)  # 启动时写入了版本快照
    assert o.client_name == NUMBAT_PROFILE.legal_name
    assert (o.running, o.run_error) == (False, None)
    assert o.current_gate is GateId.ACC_01
    assert o.awaiting_review is False
    assert o.pending_tasks == 0
    assert o.materiality_cents is None  # P1 才定重要性
    ids = [p.id for p in o.procedures]
    assert ids[:2] == ["ACC-01", "ACC-04"]  # 按阶段 P0 ... P4 排, 同阶段按编号
    assert ids[-2:] == ["RPT-01", "RPT-03"]
    status = {p.id: p.status for p in o.procedures}  # 程序编号 -> 状态
    assert status["ACC-04"] is ProcedureStatus.completed
    assert status["ACC-01"] is ProcedureStatus.planned
    acc04 = next(p for p in o.procedures if p.id == "ACC-04")
    assert acc04.name == "ASIC / ABN 核查"  # 取自程序模板


async def test_overview_while_running() -> None:
    runner, _ = make_runner()
    query = GraphStateQuery(runner.app, runner.deps, runner)

    await runner.start(EID)
    o = await query.overview(EID)  # 后台任务还没开始执行

    assert o.running is True
    assert o.current_gate is None
    assert all(p.status is ProcedureStatus.planned for p in o.procedures)  # start 已把固定集写入 Store
    await wait_idle(runner, EID)


async def test_overview_shows_run_error() -> None:
    runner, _ = make_runner(output_store=BrokenOutputStore())
    query = GraphStateQuery(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)

    o = await query.overview(EID)

    assert o.running is False
    assert o.run_error is not None
    assert "Store 不可用" in o.run_error
    assert o.current_gate is None  # 停在出错的 p0_intel, 不在闸门
    assert o.awaiting_review is False


async def test_overview_while_waiting_after_failure() -> None:
    """R03 验收 3: ACC-04 失败后停在节点内等待: 等复核, 不在执行, 无后台错误, 1 个待办."""
    runner, _ = make_runner(registry=BrokenRegistry())
    query = GraphStateQuery(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)

    o = await query.overview(EID)

    assert (o.awaiting_review, o.running, o.run_error, o.pending_tasks) == (True, False, None, 1)
    assert o.current_gate is None
    acc04 = next(p for p in o.procedures if p.id == "ACC-04")
    assert (acc04.status, acc04.pending_tasks) == (ProcedureStatus.awaiting_human, 1)


async def test_overview_counts_pending_tasks_from_store() -> None:
    _, query, store = await started()
    task = ReviewTask(
        id="T-X-ACC-04-1-v1",
        kind=TaskKind.normal,
        procedure_id="ACC-04",
        target_type=TargetType.AuditException,
        target_id="X-ACC-04-1",
        target_version=1,
        run_id="RUN-ACC-04-1",
        created_at=NOW,
    )
    decided = task.model_copy(update={"id": "T-other", "status": TaskStatus.decided})
    await store.put(EID, [task, decided], actor="A1")

    o = await query.overview(EID)

    assert o.pending_tasks == 1  # 已决定的不算
    acc04 = next(p for p in o.procedures if p.id == "ACC-04")
    assert acc04.pending_tasks == 1


async def test_overview_raises_when_engagement_missing() -> None:
    runner, _ = make_runner(engagement_store=FakeEngagementStore([NUMBAT_PROFILE], []))
    query = GraphStateQuery(runner.app, runner.deps, runner)

    with pytest.raises(LookupError):
        await query.overview(EID)


# ── R08 PLN-01/03 闸门材料 ──


async def at_pln_gate(agents: Agents | None = None) -> tuple[GraphRunner, GraphStateQuery, FakeOutputStore]:
    """新项目跑到 ACC-01, 提交 approve, P1 用 Numbat 脚本 Agent 跑完, 停在 PLN-01/03 闸门前."""
    runner, query, store = await started(agents=agents)
    submit = GraphDecisionSubmit(runner.app, runner.deps, runner)
    approve = GateDecision(
        id="D-ACC",
        gate=GateId.ACC_01,
        target_id="SCORECARD",
        target_version=1,
        decision=GateVerdict.approve,
        decided_by="U-001",
        decided_at=NOW,
    )
    await submit.submit(EID, approve)
    await wait_idle(runner, EID)
    return runner, query, store


async def test_gate_shows_planning_material() -> None:
    _, query, _ = await at_pln_gate()

    view = await query.gate(EID)

    assert view is not None
    assert (view.gate, view.target_id, view.target_version) == (GateId.PLN_01_03, "MATERIALITY", 1)
    material = view.material
    assert isinstance(material, PlanningMaterial) and material.kind == "PLN-01/03"
    assert (material.analytics.id, material.analytics.version) == ("ANALYTICS", 1)
    assert [x.id for x in material.analytics_exceptions] == ["X-PLN-07-1-A01-1"]
    assert [c.basis for c in material.candidates.candidates] == ["revenue", "total_assets", "profit_before_tax"]
    assert material.objectives == NUMBAT_OBJECTIVES
    assert material.risks == NUMBAT_RISKS
    assert material.uncovered_objective_ids == []  # Numbat 的 3 条目标都有程序覆盖


async def test_planning_material_lists_uncovered_objectives() -> None:
    """R60: PLN-01 另带存货存在性目标, knowledge 中没有存货的程序: 材料列出它, 其余目标不在缺口中."""
    _, query, _ = await at_pln_gate(inventory())

    view = await query.gate(EID)

    assert view is not None and isinstance(view.material, PlanningMaterial)
    assert [o.id for o in view.material.objectives] == [*(o.id for o in NUMBAT_OBJECTIVES), "OBJ-INV-EXI"]
    assert view.material.uncovered_objective_ids == ["OBJ-INV-EXI"]


async def test_gate_after_pln_reject_shows_rerun_material_only() -> None:
    """驳回后 p1_analytics 重跑再停: 材料为第 2 版, 分析例外只列第 2 次 PLN-07 的 (状态里两次的例外都在)."""
    runner, query, _ = await at_pln_gate()
    config = GraphRunner.config(EID)
    reject = GateDecision(
        id="D-PLN",
        gate=GateId.PLN_01_03,
        target_id="MATERIALITY",
        target_version=1,
        decision=GateVerdict.reject,
        reason="收入口径应剔除关联方销售",
        decided_by="U-001",
        decided_at=NOW,
    )
    # 模拟 DecisionSubmit 写入驳回 (PLN-01/03 决定的写入由后续需求实现): 以闸门节点身份写入后继续
    await runner.app.aupdate_state(config, {"gate_decisions": [reject]}, as_node="gate_p1_risk")
    await runner.app.ainvoke(None, config)

    view = await query.gate(EID)

    assert view is not None and (view.gate, view.target_version) == (GateId.PLN_01_03, 2)
    material = view.material
    assert isinstance(material, PlanningMaterial)
    assert material.analytics.version == 2
    assert [x.id for x in material.analytics_exceptions] == ["X-PLN-07-2-A01-1"]
    state_exceptions = (await runner.app.aget_state(config)).values["exceptions"]
    assert [x.id for x in state_exceptions] == ["X-PLN-07-1-A01-1", "X-PLN-07-2-A01-1"]


async def test_overview_at_pln_gate() -> None:
    _, query, _ = await at_pln_gate()

    o = await query.overview(EID)

    assert o.current_gate is GateId.PLN_01_03
    assert (o.running, o.awaiting_review, o.run_error) == (False, False, None)
    status = {p.id: p.status for p in o.procedures}
    assert status["DOC-04"] is ProcedureStatus.completed
    assert status["PLN-07"] is ProcedureStatus.completed
    assert status["PLN-01"] is ProcedureStatus.planned  # 闸门批准才 completed
    assert status["ACC-01"] is ProcedureStatus.completed


async def test_gate_material_missing_is_an_error() -> None:
    """停在闸门而材料缺失 (状态与流程不符): 不拼出残缺的材料, 报错."""
    runner, query, _ = await at_pln_gate()
    await runner.app.aupdate_state(GraphRunner.config(EID), {"materiality_candidates": None}, as_node="p1_analytics")

    with pytest.raises(LookupError, match="缺初步分析结果或重要性候选"):
        await query.gate(EID)


# ── R205: PLN-15 闸门的材料 (功能 planning; 停在闸门的流程见 test_decisions 的 Xero 用例) ──


def plan_gate_decision(n: int, gate: GateId, verdict: GateVerdict, target_id: str) -> GateDecision:
    return GateDecision(
        id=f"D-{n}",
        gate=gate,
        target_id=target_id,
        target_version=1,
        decision=verdict,
        reason="test",
        decided_by="U-1",
        decided_at=NOW,
    )


async def test_plan_material_takes_current_terms_and_rejections() -> None:
    """需求 R205 验收 3: PLN-15 的材料取当前计划 (PLAN-1 已取代, 为 PLAN-2), TERMS 的当前版本 (功能 acceptance 打开
    时状态中有它), 状态中的重要性三项, 此前的 PLN-15 驳回决定 (信息不足的与别的闸门的驳回不列). 缺当前计划或重要性
    时抛 LookupError (停在闸门而材料缺失, 状态与流程不符)."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=PLANNING_FEATURES)
    reject = plan_gate_decision(1, GateId.PLN_15, GateVerdict.reject, "PLAN-1")
    state = initial_state(ENGAGEMENT, [], 1)
    state.update(  # type: ignore[typeddict-item]
        audit_plans=[audit_plan(1, 2, PlanStatus.superseded), audit_plan(2)],
        engagement_terms=engagement_terms(version=2),
        materiality_cents=12_000_000,
        performance_materiality_cents=9_000_000,
        clearly_trivial_cents=600_000,
        gate_decisions=[
            plan_gate_decision(0, GateId.PLN_01_03, GateVerdict.reject, "MATERIALITY"),
            reject,
            plan_gate_decision(2, GateId.PLN_15, GateVerdict.insufficient_info, "PLAN-2"),
        ],
    )
    view = spec_of(GateId.PLN_15).view

    material, shown = await view(deps, EID, state)

    assert material == audit_plan(2)
    assert shown == PlanMaterial(
        plan=audit_plan(2),
        terms=engagement_terms(version=2),
        materiality_cents=12_000_000,
        performance_materiality_cents=9_000_000,
        clearly_trivial_cents=600_000,
        previous_decisions=[reject],
    )
    for missing in ({"audit_plans": [audit_plan(1, 2, PlanStatus.superseded)]}, {"clearly_trivial_cents": None}):
        with pytest.raises(LookupError, match="停在 PLN-15 闸门"):
            await view(deps, EID, {**state, **missing})  # type: ignore[typeddict-item]


# ── R314: 审计计划页 (功能 planning; Xero 停在闸门, 驳回再批准的数见 test_decisions) ──


# 特别风险目标 OBJ-REV-OCC 下另一条等级较低的风险项: 不是特别风险, 计划页不列 (审核意见 R314-01)
LOWER_RISK = NUMBAT_RISKS[0].model_copy(update={"id": "RISK-REV-OCC-2", "level": RiskLevel.medium})


def test_plan_view_rows_and_lists() -> None:
    """需求 R314 验收 1, 2: 当前计划为 PLAN-2 (PLAN-1 已取代); plans 为各修订的最新版本按修订号; decisions 只取 PLN-15
    的, 按决定时间; objectives 为计划涉及的 (按状态顺序, 状态里没有的不列); risks 为特别风险目标下等级为 significant
    的风险项 (同一目标下另有一条 medium 的不列, 审核意见 R314-01). 程序表:
    针对几个目标的程序每个目标一行, 按目标在状态中的顺序分组 (状态里没有的目标排在其后), 组内按程序编号, 没有目标
    的排最后; params 有整数 sample_size 为抽样, 布尔或没有为全量."""
    base = audit_plan(2)
    extra = [
        PlanItem(
            procedure_id="CSH-02",
            name="银行余额核对",
            template_commit="test",
            objective_ids=["OBJ-CASH-EXI", "OBJ-REV-CUT"],
            params={"sample_size": 25},
        ),
        PlanItem(
            procedure_id="GEN-05",
            name="分录扫描",
            template_commit="test",
            objective_ids=["OBJ-X"],
            params={"sample_size": True},
        ),
    ]
    plan = AuditPlan.model_validate({**base.model_dump(), "items": [*base.items, *extra]})
    later = plan_gate_decision(1, GateId.PLN_15, GateVerdict.reject, "PLAN-1")
    earlier = later.model_copy(update={"id": "D-0", "decided_at": NOW.replace(day=1)})
    state = initial_state(ENGAGEMENT, [], 1)
    state.update(  # type: ignore[typeddict-item]
        audit_plans=[plan, audit_plan(1, 2, PlanStatus.superseded)],
        audit_objectives=NUMBAT_OBJECTIVES,
        risk_register=[*NUMBAT_RISKS, LOWER_RISK],
        gate_decisions=[later, plan_gate_decision(2, GateId.PLN_01_03, GateVerdict.approve, "MATERIALITY"), earlier],
    )

    view = plan_view(state)

    assert view.current_plan_id == "PLAN-2" and view.plans == [audit_plan(1, 2, PlanStatus.superseded), plan]
    assert view.decisions == [earlier, later]
    assert [o.id for o in view.objectives] == ["OBJ-REV-OCC", "OBJ-REV-CUT", "OBJ-CASH-EXI"]
    assert [r.id for r in view.risks] == ["RISK-REV-OCC", "RISK-REV-CUT"]
    assert [(r.objective_id, r.procedure_id, r.extent, r.sample_size) for r in view.rows] == [
        ("OBJ-REV-OCC", "REV-08", "full", None),
        ("OBJ-REV-CUT", "CSH-02", "sample", 25),
        ("OBJ-REV-CUT", "REV-08", "full", None),
        ("OBJ-CASH-EXI", "CSH-02", "sample", 25),
        ("OBJ-X", "GEN-05", "full", None),
        (None, "DOC-06", "full", None),
    ]


EMPTY_PLAN = PlanView(current_plan_id=None, plans=[], decisions=[], objectives=[], risks=[], rows=[])


async def test_plan_view_without_plan_over_http() -> None:
    """需求 R314 验收 4: 功能 planning 关闭 (Numbat 基线, 停在 ACC-01) 时 GET /plan 为 200, 没有当前计划, 各列表为空;
    项目不存在 404 NOT_FOUND (同其他项目接口)."""
    runner, query, _ = await started()
    assert await query.plan(EID) == EMPTY_PLAN
    async with api_client(runner) as client:
        ok = await client.get(f"/api/v1/engagements/{EID}/plan")
        missing = await client.get("/api/v1/engagements/ENG-missing/plan")
    assert ok.status_code == 200 and PlanView.model_validate(ok.json()) == EMPTY_PLAN
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "NOT_FOUND"
