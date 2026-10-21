"""闸门登记表 (orchestration/gates.py, 接口文档第 7 节): 现有 4 行, 一个节点挂多道闸门的组合路由, 专属字段规则,
按功能加入的闸门节点."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    AcceptanceScorecard,
    Annotation,
    AuditPlan,
    GateDecision,
    GateId,
    GateVerdict,
    MaterialityCandidates,
    MaterialityChoice,
    OpinionType,
    Phase,
    PlanStatus,
    ProcedureStatus,
    ReportSection,
)
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration import gates
from backend.orchestration.gates import (
    GATE_SPECIFIC,
    GATES,
    FieldRule,
    allowed_fields,
    check_fields,
    check_registry,
    current_gate,
    current_plan,
    gate_at,
    node_route,
    path_map,
    required_fields,
    spec_of,
)
from backend.orchestration.graph import build_graph, compile_graph
from backend.orchestration.runner import waiting_for_human
from backend.services.features import BASELINE
from tests.fakes import (
    ENGAGEMENT,
    PLAN_CONFIRMATION,
    PLANNING_FEATURES,
    FakeRegistry,
    FakeTrail,
    audit_plan,
    make_deps,
    make_graph_nodes,
    make_procedure,
)

NOW = datetime(2026, 10, 18, tzinfo=UTC)
JOURNALS = frozenset({"critic", "journals"})


def decision(gate: GateId, verdict: GateVerdict, target_id: str, version: int = 1, n: int = 1) -> GateDecision:
    return GateDecision(
        id=f"D-{gate.value}-{n}",
        gate=gate,
        target_id=target_id,
        target_version=version,
        decision=verdict,
        reason=None if verdict is GateVerdict.approve else "test",
        decided_by="U-1",
        decided_at=NOW,
    )


# ── 验收 1: 现有 4 道闸门 ──


def exits_in(spec: Any, features: frozenset[str]) -> dict[str, str]:
    """闸门在功能集下的出口去向 (去向为函数时按功能集取)."""
    return {name: target(features) if callable(target) else target for name, target in spec.exits.items()}


def test_four_gates_registered_as_before() -> None:
    """基线的 4 道闸门: 节点, 材料, 留痕程序与出口同改造前; 每个节点另有 represent 回自身 (path_map). 挂功能名的
    闸门只有 PLN-15 (功能 planning, 需求 R205, 见 test_pln15_gate_row)."""
    rows = {
        spec.gate: (spec.node, spec.material_field, spec.material_id, spec.procedure_id, exits_in(spec, BASELINE))
        for spec in GATES
        if spec.feature is None
    }
    assert rows == {
        GateId.ACC_01: (
            "gate_acc01",
            "acceptance_scorecard",
            "SCORECARD",
            "ACC-01",
            {"approve": "p1_await_data", "reject": "declined", "rerun": "p0_inputs"},
        ),
        GateId.PLN_01_03: (
            "gate_p1_risk",
            "materiality_candidates",
            "MATERIALITY",
            "PLN-01",
            {"approve": "assemble_pack", "reject": "p1_analytics"},
        ),
        GateId.FIN_01: (
            "gate_fin",
            "uncorrected_schedule",
            "SCHEDULE",
            "FIN-01",
            {"approve": "report_draft", "reject": "completion"},
        ),
        GateId.RPT_01: (
            "gate_rpt01",
            "report_versions",
            None,
            "RPT-01",
            {"approve": "archive", "reject": "report_draft"},
        ),
    }
    assert spec_of(GateId.RPT_01).is_list and not spec_of(GateId.FIN_01).is_list
    assert path_map([spec_of(GateId.FIN_01)], BASELINE) == {
        "represent": "gate_fin",
        "approve": "report_draft",
        "reject": "completion",
    }
    # 功能 misstatement 打开时 FIN-01 驳回回 summarize 重做 FIN-04 (需求 R160)
    assert exits_in(spec_of(GateId.FIN_01), BASELINE | {"misstatement"})["reject"] == "summarize"
    assert {spec.gate: spec.feature for spec in GATES if spec.feature is not None} == {GateId.PLN_15: "planning"}


def test_pln15_gate_row() -> None:
    """需求 R205 验收 1: PLN-15 的登记行. 节点 gate_p1_plan, 留痕程序 PLN-15, 材料为 audit_plans 中的当前计划 (pick:
    status 不是 superseded 的修订中 revision 最大的), 出口 approve → schedule, reject → plan_draft (represent 回自身),
    通过须带 plan_confirmation, 驳回与信息不足没有必填 (理由选填, 需求 R358); 登记了 "通过时材料出新版本仍放行"
    (passed, 同 RPT-01 签字); 功能 planning. 基线下不在主图中."""
    spec = spec_of(GateId.PLN_15)
    assert (spec.node, spec.procedure_id, spec.material_field, spec.material_model, spec.feature) == (
        "gate_p1_plan",
        "PLN-15",
        "audit_plans",
        AuditPlan,
        "planning",
    )
    assert spec.is_list and spec.pick is current_plan and spec.passed is not None and spec.rebuild is not None
    assert spec.check is gates.pln15_check  # 需求 R206: 项目质量复核的依据不能为空
    assert path_map([spec], PLANNING_FEATURES) == {
        "represent": "gate_p1_plan",
        "approve": "schedule",
        "reject": "plan_draft",
    }
    assert dict(spec.fields) == {GateVerdict.approve: FieldRule(("plan_confirmation",))}
    assert "gate_p1_plan" not in gates.gate_nodes(BASELINE)
    assert gates.gate_nodes(PLANNING_FEATURES) == [
        "gate_acc01",
        "gate_p1_risk",
        "gate_p1_plan",
        "gate_fin",
        "gate_rpt01",
    ]


def test_current_plan_skips_superseded_revisions() -> None:
    """PLN-15 的材料: status 不是 superseded 的修订中 revision 最大的 (第 0 节); 驳回重拟后上一修订已取代, 当前为新
    修订; 都已取代或没有计划时为 None."""
    plan1, plan2 = audit_plan(1), audit_plan(2)
    retired = audit_plan(1, 2, PlanStatus.superseded)
    approved = audit_plan(1, 2, PlanStatus.approved)
    assert current_plan([plan1]) == plan1 and current_plan([approved]) == approved
    assert current_plan([retired, plan2]) == plan2
    assert current_plan([retired]) is None and current_plan([]) is None


def test_pln15_effects_is_pure() -> None:
    """需求 R206 验收 7: pln15_effects 是 (程序包, 决定, 当前计划) 的纯函数: 同一输入两次结果相等, 输入不变. 通过出
    批准版 (下一版, 批准时间取决定时刻), PLN-15 completed, 阶段 P2_Executing; 驳回与信息不足只返回决定."""
    pack = [make_procedure("PLN-15", Phase.P1_Planning), make_procedure("REV-08", Phase.P2_Executing)]
    plan = audit_plan()
    approve = decision(GateId.PLN_15, GateVerdict.approve, "PLAN-1").model_copy(
        update={"plan_confirmation": PLAN_CONFIRMATION}
    )
    inputs = (list(pack), plan.model_copy())

    effects = gates.pln15_effects(pack, approve, plan)

    assert effects == gates.pln15_effects(pack, approve, plan) and (pack, plan) == inputs
    [approved] = effects["audit_plans"]
    assert (approved.id, approved.version, approved.status, approved.approved_at) == (
        "PLAN-1",
        2,
        PlanStatus.approved,
        NOW,
    )
    assert [(p.id, p.version, p.status) for p in effects["procedure_pack"]] == [
        ("PLN-15", 2, ProcedureStatus.completed),
        ("REV-08", 1, ProcedureStatus.planned),
    ]
    assert (effects["gate_decisions"], effects["phase"]) == ([approve], Phase.P2_Executing)
    for verdict in (GateVerdict.reject, GateVerdict.insufficient_info):
        d = decision(GateId.PLN_15, verdict, "PLAN-1")
        assert gates.pln15_effects(pack, d, plan) == {"gate_decisions": [d]}


# ── 验收 3: 一个节点挂两道闸门的组合路由 (纯函数) ──

A = replace(spec_of(GateId.ACC_01), node="gate_combo")
B = replace(spec_of(GateId.PLN_01_03), node="gate_combo")


def combo_state(*decisions: GateDecision) -> EngagementState:
    """A 的材料为评分卡 v1 (依据画像第 1 版, 不触发重跑), B 的材料为重要性候选 v1."""
    state = initial_state(ENGAGEMENT, [], 1)
    state["acceptance_scorecard"] = AcceptanceScorecard(
        id="SCORECARD", registry_checks=[], exception_ids=[], client_profile_version=1
    )
    state["materiality_candidates"] = MaterialityCandidates(id="MATERIALITY", candidates=[])
    state["gate_decisions"] = list(decisions)
    return state


def test_combined_route_decides_gates_in_order() -> None:
    """A 无决定 → represent, 当前为 A; A 通过且绑定当前材料 → 看 B, B 无决定 → represent, 当前为 B; B 通过 → B 的
    通过出口; A 驳回 → A 的驳回出口; A 的决定绑定旧材料 → represent, 当前为 A."""
    route = node_route([A, B])
    exits = path_map([A, B], BASELINE)
    a_ok = decision(GateId.ACC_01, GateVerdict.approve, "SCORECARD")
    cases: list[tuple[tuple[GateDecision, ...], str, GateId]] = [
        ((), "represent", GateId.ACC_01),
        ((a_ok,), "represent", GateId.PLN_01_03),
        ((a_ok, decision(GateId.PLN_01_03, GateVerdict.approve, "MATERIALITY")), "PLN-01/03 approve", GateId.PLN_01_03),
        ((decision(GateId.ACC_01, GateVerdict.reject, "SCORECARD"),), "ACC-01 reject", GateId.ACC_01),
        ((decision(GateId.ACC_01, GateVerdict.approve, "SCORECARD", version=0),), "represent", GateId.ACC_01),
    ]
    for decisions, key, current in cases:
        state = combo_state(*decisions)
        assert route(state) == key, decisions
        assert gate_at([A, B], state).gate is current, decisions
    assert exits == {
        "represent": "gate_combo",
        "ACC-01 reject": "declined",
        "ACC-01 rerun": "p0_inputs",
        "PLN-01/03 approve": "assemble_pack",
        "PLN-01/03 reject": "p1_analytics",
    }  # 前一道的 approve 不出节点, 不列
    assert exits[route(combo_state(a_ok, decision(GateId.PLN_01_03, GateVerdict.approve, "MATERIALITY")))] == (
        "assemble_pack"
    )


def test_combined_node_gets_one_conditional_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一节点只加一条条件边; current_gate 按项目冻结的功能集取节点上的闸门."""
    combo = [replace(A, feature="journals"), replace(B, feature="journals")]
    monkeypatch.setattr(gates, "GATES", [*GATES, *combo])
    deps = make_deps(FakeRegistry(), FakeTrail(), features=JOURNALS)
    graph = build_graph(make_graph_nodes(deps), JOURNALS)
    assert list(graph.branches["gate_combo"]) == ["route"]
    state = combo_state(decision(GateId.ACC_01, GateVerdict.approve, "SCORECARD"))
    state["version_snapshot"] = {"features": "critic,journals"}
    assert current_gate(("gate_combo",), state) is GateId.PLN_01_03
    assert current_gate(("gate_combo",), {**state, "version_snapshot": {}}) is None  # 基线下没有这个节点


# ── 验收 4: 专属字段与原规则逐条等价 ──


def previous_required(gate: GateId, verdict: GateVerdict) -> list[str]:
    """R86 之前 decisions.required_fields 的规则 (原样抄录), 去掉理由: 需求 R358 起各种决定的理由都选填."""
    approve = verdict is GateVerdict.approve
    required: list[str] = []
    if approve and gate is GateId.PLN_01_03:
        required.append("materiality")
    if approve and gate is GateId.FIN_01:
        required.append("disposition")
    if verdict is GateVerdict.reject and gate is GateId.RPT_01:
        required.append("annotations")
    return required


def previous_allowed(gate: GateId, verdict: GateVerdict) -> set[str]:
    """R86 之前 decisions.allowed_fields 的规则 (原样抄录)."""
    approve = verdict is GateVerdict.approve
    if approve and gate is GateId.PLN_01_03:
        return {"materiality", "edits"}
    if approve and gate is GateId.FIN_01:
        return {"disposition"}
    if verdict is GateVerdict.reject and gate is GateId.RPT_01:
        return {"annotations"}
    return set()


@pytest.mark.parametrize("gate", [spec.gate for spec in GATES if spec.feature is None])
@pytest.mark.parametrize("verdict", list(GateVerdict))
def test_field_rules_equal_previous(gate: GateId, verdict: GateVerdict) -> None:
    """基线的每道闸门每种决定: 必填与只允许的字段同改造前. 挂功能名的新闸门 (ACC-08 等) 没有 "改造前", 字段规则由
    各自的需求另写用例 (ACC-08 为 R201, R202; 审核 R198-06)."""
    d = decision(gate, verdict, "X")
    assert required_fields(spec_of(gate), d) == previous_required(gate, verdict)
    assert allowed_fields(spec_of(gate), d) == previous_allowed(gate, verdict)


# 必填字段的一个合法取值, 只用来补齐必填以便看别的字段
FILLED = {
    "reason": "test",
    "materiality": MaterialityChoice(basis="revenue", pct=0.01, performance_pct=0.75, clearly_trivial_pct=0.05),
    "disposition": OpinionType.unqualified,
    "annotations": [Annotation(section=ReportSection.opinion, comment="test")],
}


@pytest.mark.parametrize("gate", [spec.gate for spec in GATES])
@pytest.mark.parametrize("verdict", list(GateVerdict))
def test_plan_confirmation_only_on_pln15_approve(gate: GateId, verdict: GateVerdict) -> None:
    """需求 R203, R205: plan_confirmation 是闸门专属字段, 只有 PLN-15 的通过决定能填 (且必填); 其余闸门的决定与 PLN-15
    的驳回, 信息不足带它报 FIELD_NOT_ALLOWED. 必填字段照各闸门的规则补齐, 只看这一项."""
    assert "plan_confirmation" in GATE_SPECIFIC
    spec = spec_of(gate)
    if (gate, verdict) == (GateId.PLN_15, GateVerdict.approve):
        d = decision(gate, verdict, "PLAN-1").model_copy(update={"plan_confirmation": PLAN_CONFIRMATION})
        assert required_fields(spec, d) == ["plan_confirmation"] and allowed_fields(spec, d) == {"plan_confirmation"}
        check_fields(spec, d)
        return
    filled = {name: FILLED[name] for name in required_fields(spec, decision(gate, verdict, "X"))}
    d = decision(gate, verdict, "X").model_copy(update={**filled, "plan_confirmation": PLAN_CONFIRMATION})
    assert "plan_confirmation" not in allowed_fields(spec, d)
    with pytest.raises(DecisionRejected, match="plan_confirmation") as e:
        check_fields(spec, d)
    assert e.value.code == "FIELD_NOT_ALLOWED"


# ── 验收 5: 挂功能名的闸门 ──


def journals_gate() -> Any:
    """测试用的临时闸门 (功能 journals, 新节点): 驳回出口按功能集取."""
    return replace(
        spec_of(GateId.FIN_01),
        node="gate_journals",
        exits={"approve": "completion", "reject": lambda features: "report_draft" if "journals" in features else "x"},
        feature="journals",
    )


def test_feature_gate_node_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """基线下该节点不在主图中; 启用 journals 后出现, 为静态中断, 驳回出口按功能集取到 report_draft."""
    monkeypatch.setattr(gates, "GATES", [*GATES, journals_gate()])
    base = compile_graph(make_graph_nodes(make_deps(FakeRegistry(), FakeTrail())), InMemorySaver())
    assert "gate_journals" not in base.get_graph().nodes
    assert list(base.interrupt_before_nodes) == ["gate_acc01", "gate_p1_risk", "gate_fin", "gate_rpt01"]
    nodes = make_graph_nodes(make_deps(FakeRegistry(), FakeTrail(), features=JOURNALS))
    app = compile_graph(nodes, InMemorySaver())
    graph = app.get_graph()
    assert "gate_journals" in graph.nodes and "gate_journals" in app.interrupt_before_nodes
    # 新节点没有入边 (由各功能组的需求接入), 图示不画它的出口, 直接看登记的条件边
    [branch] = build_graph(nodes, JOURNALS).branches["gate_journals"].values()
    assert branch.ends == {"represent": "gate_journals", "approve": "completion", "reject": "report_draft"}
    # recover 判断是否停在闸门前: 按项目冻结的功能集取闸门节点
    values: dict[str, Any] = {"version_snapshot": {"features": "critic,journals"}}
    stopped = SimpleNamespace(next=("gate_journals",), tasks=(), values=values)
    assert waiting_for_human(stopped)  # type: ignore[arg-type]
    assert not waiting_for_human(SimpleNamespace(next=("gate_journals",), tasks=(), values={}))  # type: ignore[arg-type]


# ── 登记行的自检: 写错在构造 (模块加载) 时报出 ──


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        (
            {"exits": {"approve": "completion", "rejct": "report_draft"}},
            "出口名 rejct 不是 approve, reject, rerun 之一",
        ),
        ({"exits": {"reject": "report_draft"}}, "没有 approve 出口"),
        ({"pick": max}, "material_id 与 pick 须恰好给一个"),
        ({"material_id": None}, "material_id 与 pick 须恰好给一个"),
        ({"material_field": "schedule"}, "材料字段 schedule 不是状态字段"),
        (
            {"fields": {GateVerdict.reject: FieldRule(("reason", "anotations"))}},
            "reject 的字段 anotations 不是 GateDecision 的字段",
        ),
        ({"feature": "jurnals"}, "功能 jurnals 未登记"),
    ],
)
def test_registration_typos_rejected_when_built(changes: dict[str, Any], problem: str) -> None:
    """功能行的出口名拼错, 缺 approve, 材料的取法, 材料字段, 专属字段名与功能名写错: 构造登记行即抛 ValueError,
    消息含闸门与问题 (不等到审计师作出那种决定时路由键不在 path_map 中, 或 check_fields 抛 AttributeError)."""
    with pytest.raises(ValueError, match=f"闸门 FIN-01 的登记行不对: .*{problem}"):
        replace(spec_of(GateId.FIN_01), **changes)


def test_gate_registered_once() -> None:
    """一道闸门只登记一行 (spec_of 按编号取行): 现有登记表通过; 同一 GateId 再登记一行即报错."""
    check_registry(GATES)
    with pytest.raises(ValueError, match="FIN-01 登记了不止一行"):
        check_registry([*GATES, replace(spec_of(GateId.FIN_01), node="gate_fin2", feature="journals")])
