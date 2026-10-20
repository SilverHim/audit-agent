"""状态字段缺省值与旧检查点兼容: 登记表覆盖全部键, 检查点缺键时各处读到缺省值, 检查点类型与审计产出表的守卫.

字段登记清单见开发接口文档第 3 节, 审计产出表见第 9 节.
"""

import types
from collections.abc import Iterable
from datetime import date
from enum import Enum, StrEnum
from typing import Any, Literal, TypedDict, Union, get_args, get_origin, get_type_hints, is_typeddict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AgentId,
    AuditPlan,
    AutonomyLevel,
    CheckItemStatus,
    ClauseStatus,
    CommunicationForm,
    ComplianceCheck,
    CriticReasonCode,
    CriticVerdict,
    DeclarationForm,
    EngagementTerms,
    GateDecision,
    GateId,
    GateVerdict,
    GovernanceCommunication,
    IndependenceDeclaration,
    Judgment,
    MaterialityChoice,
    Misstatement,
    ObjectRef,
    OpinionType,
    OwnershipGraph,
    Phase,
    PlanStatus,
    ProposedAction,
    ReportStatus,
    RepresentationStatus,
    ReviewRecord,
    ReviewStatus,
    TermsCheckStatus,
    TrailKind,
    UncorrectedSchedule,
    Versioned,
    WrittenRepresentationLetter,
)
from backend.contracts.state import REQUIRED_KEYS, STATE_DEFAULTS, EngagementState, fill_defaults, initial_state
from backend.contracts.views import CompletionMaterial
from backend.orchestration.checkpoint import (
    TOP_LEVEL_ENUMS,
    allowed_types,
    checkpoint_serde,
    restore_enums,
    top_level_enums,
)
from backend.orchestration.graph import (
    build_graph,
    fieldwork_with_defaults,
    gate_route,
    node_with_defaults,
    route_with_defaults,
)
from backend.orchestration.nodes import FieldworkInput, apply_patch
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.recovery import catch_up_review
from backend.orchestration.runner import GraphRunner, resume_required
from backend.persistence.models import OUTPUT_KINDS
from backend.services.features import FEATURES
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    audit_plan,
    compliance_check,
    engagement_terms,
    governance_communication,
    independence_declaration,
    make_deps,
    make_graph_nodes,
    make_procedure,
    make_runner,
    numbat_all_p2_agents,
    ownership_graph,
    representation_letter,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision
from tests.test_flow import FIN_APPROVE, approve_materiality, assert_numbat_result, review_all
from tests.test_review_decisions import NORMAL, record, waiting_with_failure

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)


# ── 验收 1: 缺省值登记表 ──


def strip_optional(hint: Any) -> tuple[Any, bool]:
    """X | None → (X, True); 其余 → (hint, False). 多于一个非空成员的联合原样返回."""
    if get_origin(hint) in (Union, types.UnionType):
        members = [a for a in get_args(hint) if a is not type(None)]
        if len(members) < len(get_args(hint)) and len(members) == 1:
            return members[0], True
    return hint, False


def default_problems(
    state: Any, defaults: dict[str, Any] = STATE_DEFAULTS, required: Iterable[str] = REQUIRED_KEYS
) -> list[str]:
    """登记表与状态注解对不上的地方: 未登记的键, 多登记的键, 缺省值与类型不符 (列表须为 [], 可空须为 None)."""
    hints = get_type_hints(state)
    required = set(required)
    problems = [f"未登记缺省值: {k}" for k in hints if k not in required and k not in defaults]
    problems += [f"状态中没有登记的键: {k}" for k in defaults if k not in hints]
    problems += [f"必有键不在状态中: {k}" for k in sorted(required) if k not in hints]
    for key, value in defaults.items():
        if key not in hints:
            continue
        hint, optional = strip_optional(hints[key])
        if get_origin(hint) is list:
            if value != [] or not isinstance(value, list):
                problems.append(f"{key} 是列表, 缺省值须为 []")
        elif optional:
            if value is not None:
                problems.append(f"{key} 可空, 缺省值须为 None")
        else:
            problems.append(f"{key} 既不是列表也不可空, 须由 initial_state 给出 (列入 REQUIRED_KEYS)")
    return problems


def test_state_defaults_cover_every_key() -> None:
    """验收 1: 除 5 个必有键外的全部键都登记了缺省值, 列表为 [], 单对象与 int | None 为 None."""
    assert REQUIRED_KEYS == ("engagement_id", "phase", "version_snapshot", "client_profile_version", "procedure_pack")
    assert default_problems(EngagementState) == []
    assert set(STATE_DEFAULTS) | set(REQUIRED_KEYS) == set(get_type_hints(EngagementState))


class ExtendedState(EngagementState):
    """模拟某组加了状态字段而忘了登记."""

    going_concern: Judgment | None
    forgotten_items: list[Judgment]  # 列表字段 (原名 audit_plans, 需求 R203 起是真字段)


def test_default_check_names_unregistered_and_wrong_defaults() -> None:
    """验收 1: 新增键未登记时报出键名; 缺省值与类型不符, 或多登记了不存在的键, 也报出."""
    assert default_problems(ExtendedState) == ["未登记缺省值: going_concern", "未登记缺省值: forgotten_items"]
    assert default_problems(ExtendedState, {**STATE_DEFAULTS, "going_concern": None, "forgotten_items": []}) == []
    wrong = {**STATE_DEFAULTS, "misstatements": None, "uncorrected_schedule": [], "extra": None}
    assert default_problems(EngagementState, wrong) == [
        "状态中没有登记的键: extra",
        "misstatements 是列表, 缺省值须为 []",
        "uncorrected_schedule 可空, 缺省值须为 None",
    ]
    # 必有键少列一个时报未登记; 把不可空的单值键当作有缺省值登记时报须由 initial_state 给出
    assert default_problems(EngagementState, STATE_DEFAULTS, REQUIRED_KEYS[1:]) == ["未登记缺省值: engagement_id"]
    others = [k for k in REQUIRED_KEYS if k != "phase"]
    assert default_problems(EngagementState, {**STATE_DEFAULTS, "phase": None}, others) == [
        "phase 既不是列表也不可空, 须由 initial_state 给出 (列入 REQUIRED_KEYS)"
    ]


def test_initial_state_equals_previous_literal() -> None:
    """验收 1: 由登记表生成的初始状态与原来逐个写出的逐键相等; 每次的列表都是新的, 不与登记表共用."""
    pack = [make_procedure("ACC-04")]
    state = initial_state(ENGAGEMENT, pack, 3)
    previous = dict(
        engagement_id=ENGAGEMENT.id,
        phase=ENGAGEMENT.phase,
        version_snapshot=dict(ENGAGEMENT.version_snapshot),
        acceptance_scorecard=None,
        engagement_terms=None,  # 需求 R198 加
        client_profile_version=3,
        analytics=None,
        materiality_candidates=None,
        materiality_cents=None,
        performance_materiality_cents=None,
        clearly_trivial_cents=None,
        audit_objectives=[],
        risk_register=[],
        audit_plans=[],  # 需求 R203 加
        procedure_pack=pack,
        schedule_table=[],
        documents=[],
        staged_results=[],
        procedure_runs=[],
        exceptions=[],
        judgments=[],
        review_tasks=[],
        confirmations=[],
        ownership_graph=None,  # 需求 R136 加
        report_versions=[],
        review_records=[],
        gate_decisions=[],
        misstatements=[],
        rerun_requests=[],  # 需求 R274 加
        uncorrected_schedule=None,
        representation_letter=None,  # 以下四项需求 R215 加
        compliance_checks=[],
        governance_communications=[],
        independence_declaration=None,
        opinion_route=None,
    )
    assert dict(state) == previous
    assert state["procedure_pack"] is pack
    other = initial_state(ENGAGEMENT, pack, 3)
    assert state["exceptions"] is not other["exceptions"]
    state["exceptions"].append("x")  # type: ignore[arg-type]
    assert other["exceptions"] == [] and STATE_DEFAULTS["exceptions"] == []


def test_fill_defaults_fills_missing_keys_only() -> None:
    """缺的登记键补缺省值, 已有的原样保留 (同一对象), 入参不改; 空检查点只补登记的键."""
    pack = [make_procedure("ACC-04")]
    state = dict(initial_state(ENGAGEMENT, pack, 1))
    for key in ("misstatements", "uncorrected_schedule", "opinion_route"):
        del state[key]
    filled = fill_defaults(state)
    assert (filled["misstatements"], filled["uncorrected_schedule"], filled["opinion_route"]) == ([], None, None)
    assert filled["procedure_pack"] is pack and "misstatements" not in state
    assert fill_defaults({"opinion_route": OpinionType.qualified})["opinion_route"] is OpinionType.qualified
    assert set(fill_defaults({})) == set(STATE_DEFAULTS)


def old_state() -> dict[str, Any]:
    """缺 uncorrected_schedule 与 opinion_route 的状态, 如加这两个字段之前存的检查点读回."""
    state = dict(initial_state(ENGAGEMENT, [make_procedure("ACC-04")], 1))
    del state["uncorrected_schedule"], state["opinion_route"]
    return state


async def test_wrappers_fill_defaults_before_calling() -> None:
    """节点, fieldwork 与路由的包装先补缺省值再调用: 不包时 FIN-01 路由取决定绑定的汇总表抛 KeyError,
    包了读到 None, 留在闸门重新呈报."""
    route = gate_route(GateId.FIN_01)
    state = {**old_state(), "gate_decisions": [FIN_APPROVE]}
    with pytest.raises(KeyError, match="uncorrected_schedule"):
        route(state)  # type: ignore[arg-type]
    assert route_with_defaults(route)(state) == "represent"  # type: ignore[arg-type]

    seen: list[Any] = []

    async def node(state: EngagementState) -> dict[str, Any]:
        seen.append(state["opinion_route"])
        return {}

    async def fieldwork(payload: FieldworkInput) -> dict[str, Any]:
        seen.append((payload["instance"], payload["procedure_ids"], payload["state"]["uncorrected_schedule"]))
        return {}

    await node_with_defaults(node)(old_state())  # type: ignore[arg-type]
    payload = {"instance": "fw-1-1", "procedure_ids": ["REV-08"], "state": old_state()}
    await fieldwork_with_defaults(fieldwork)(payload)  # type: ignore[arg-type]
    assert seen == [None, ("fw-1-1", ["REV-08"], None)]


def test_every_node_and_route_is_wrapped() -> None:
    """build_graph 登记的每个节点函数与路由函数都经过包装 (按包装函数的代码对象认), 全部功能启用时也是."""
    deps = make_deps(FakeRegistry({}, {}), FakeTrail(), FakeEngagementStore([], []), FakeOutputStore())
    nodes = make_graph_nodes(deps)
    node_code = {node_with_defaults(nodes.gate).__code__, fieldwork_with_defaults(nodes.fieldwork).__code__}
    route_code = route_with_defaults(len).__code__
    for features in (deps.features, frozenset(FEATURES)):
        graph = build_graph(nodes, features)
        unwrapped = [name for name, spec in graph.nodes.items() if spec.runnable.afunc.__code__ not in node_code]
        assert unwrapped == [], features
        routes = [b.path.func for branches in graph.branches.values() for b in branches.values()]
        assert len(routes) == 8 and all(r.__code__ is route_code for r in routes), features  # 含 R290 的 p0_inputs


# ── 验收 2: 模拟旧检查点 ──

# 加字段之前的检查点没有的通道: 原有三个, 加需求 R215 的四个 (签字前提的产出)
DROPPED = (
    "misstatements",
    "uncorrected_schedule",
    "opinion_route",
    "representation_letter",
    "compliance_checks",
    "governance_communications",
    "independence_declaration",
)


def drop_channels(saver: object, thread_id: str, channels: Iterable[str]) -> None:
    """模拟加字段之前存的检查点: 从该线程最新检查点的通道版本表中去掉这些通道, 读回时它们没有值."""
    assert isinstance(saver, InMemorySaver)
    checkpoints = saver.storage[thread_id][""]
    latest = max(checkpoints)
    checkpoint, metadata, parent = checkpoints[latest]
    data = saver.serde.loads_typed(checkpoint)
    for channel in channels:
        del data["channel_versions"][channel]
    checkpoints[latest] = (saver.serde.dumps_typed(data), metadata, parent)


async def numbat_to_fin(drop: bool) -> Started:
    """Numbat 脚本 Agent 跑到 ACC-01 闸门; drop 时删去 DROPPED 的通道; 再按 test_flow 的决定走到 FIN-01 闸门前."""
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(engagement_store=engagement_store, agents=numbat_all_p2_agents)
    s = Started(runner, store, engagement_store)
    query = GraphStateQuery(runner.app, runner.deps, runner)
    await runner.start(EID)
    await wait_idle(runner, EID)
    assert await s.next_nodes() == ("gate_acc01",)
    if drop:
        drop_channels(runner.app.checkpointer, EID, DROPPED)
        values = (await runner.app.aget_state(CONFIG)).values
        # 没有 reducer 的两个通道确实缺键; misstatements 带 reducer, LangGraph 读回时可能自行补空列表
        assert "uncorrected_schedule" not in values and "opinion_route" not in values
        assert "representation_letter" not in values and "independence_declaration" not in values
        assert values.get("compliance_checks", []) == values.get("governance_communications", []) == []
        filled = fill_defaults(values)
        assert (filled["representation_letter"], filled["independence_declaration"]) == (None, None)
        assert filled["compliance_checks"] == filled["governance_communications"] == []
        assert values.get("misstatements", []) == []
        assert values["phase"] is Phase.P0_Acceptance
        overview = await query.overview(EID)
        assert (overview.current_gate, overview.opinion_route) == (GateId.ACC_01, None)
        view = await query.gate(EID)
        assert view is not None and view.target_id == "SCORECARD"
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await approve_materiality(s)
    await wait_idle(runner, EID)
    await review_all(s)  # 每次提交前读总览
    await assert_numbat_result(s)  # FIN-04 读到 misstatements, 出 SCHEDULE
    return s


def trail_rows(s: Started) -> list[dict[str, Any]]:
    """留痕各行. 数据管理的 data 留痕 (体检, 补交请求) 的时间与防重复键取当时的当前时间, 两次运行必然不同,
    比较时置空."""
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    rows = [e.model_dump() for e in trail.entries]
    for row in rows:
        if row["kind"] is TrailKind.data:
            row["created_at"] = row["idempotency_key"] = None
    return rows


async def test_old_checkpoint_without_new_channels_runs_to_fin() -> None:
    """验收 2: ACC-01 闸门处的检查点删去 DROPPED 的通道 (R215 起含签字前提的四个) 后照常走到 FIN-01: 查询与决定
    提交不抛错, FIN-04 出 SCHEDULE; 程序包, SCHEDULE 与留痕与不删通道时相同. 之后 FIN-01 决定写入 opinion_route,
    走到 RPT-01."""
    dropped = await numbat_to_fin(drop=True)
    baseline = await numbat_to_fin(drop=False)

    query = GraphStateQuery(dropped.runner.app, dropped.runner.deps, dropped.runner)
    overview = await query.overview(EID)
    assert (overview.current_gate, overview.opinion_route, overview.run_error) == (GateId.FIN_01, None, None)
    view = await query.gate(EID)
    assert view is not None and isinstance(view.material, CompletionMaterial)
    assert view.material.misstatements == await dropped.output_store.latest(EID, Misstatement)
    packs = [(await s.runner.app.aget_state(CONFIG)).values["procedure_pack"] for s in (dropped, baseline)]
    assert packs[0] == packs[1]
    schedules = [await s.output_store.get(EID, UncorrectedSchedule, "SCHEDULE") for s in (dropped, baseline)]
    assert schedules[0] is not None and schedules[0] == schedules[1]
    assert trail_rows(dropped) == trail_rows(baseline)

    await dropped.submit.submit(EID, FIN_APPROVE)
    await wait_idle(dropped.runner, EID)
    assert await dropped.next_nodes() == ("gate_rpt01",)
    assert (await dropped.runner.app.aget_state(CONFIG)).values["opinion_route"] is OpinionType.qualified


# ── 验收 3: 检查点类型白名单与顶层枚举 ──


def state_types(hint: Any, seen: set[Any] | None = None) -> set[type]:
    """注解里出现的 Pydantic 类与枚举, 递归到 TypedDict 的键与 Pydantic 模型的字段类型."""
    seen = set() if seen is None else seen
    if hint in seen or hint is Any:
        return set()
    seen.add(hint)
    found: set[type] = set()
    if is_typeddict(hint):
        for value in get_type_hints(hint).values():
            found |= state_types(value, seen)
    elif isinstance(hint, type) and issubclass(hint, Enum):
        found.add(hint)
    elif isinstance(hint, type) and issubclass(hint, BaseModel):
        found.add(hint)
        for field in hint.model_fields.values():
            found |= state_types(field.annotation, seen)
    elif get_origin(hint) is not None and get_origin(hint) is not Literal:
        for arg in get_args(hint):
            found |= state_types(arg, seen)
    return found


def unregistered_types(state: Any) -> list[str]:
    """状态里出现而不在检查点白名单中的类型名 (定义在 _STATE_MODULES 之外)."""
    allowed = set(allowed_types())
    return sorted(t.__name__ for t in state_types(state) if t not in allowed)


def test_state_types_are_in_checkpoint_allowlist() -> None:
    """验收 3: EngagementState 注解里出现的全部 Pydantic 类与枚举都在白名单中."""
    found = state_types(EngagementState)
    assert {Phase, OpinionType, AgentId, Judgment, UncorrectedSchedule} <= found
    assert unregistered_types(EngagementState) == []


class LocalKind(StrEnum):
    """定义在 _STATE_MODULES 之外的枚举."""

    a = "a"


class LocalItem(BaseModel):
    kind: LocalKind


class LocalPlan(Versioned):
    """定义在 _STATE_MODULES 之外, 字段里还套着另一个外来类型."""

    items: list[LocalItem]


class StateWithLocalTypes(EngagementState):
    local_plans: list[LocalPlan]
    plan_kind: LocalKind | None


async def checkpoint_round_trip(state_type: Any, values: dict[str, Any]) -> dict[str, Any]:
    """一个节点写出 values, 存进用检查点白名单 serde 的 InMemorySaver, 读回状态. state_type 为只含这些键 (无 reducer)
    的 TypedDict, 输入各键为 None."""

    async def write(state: Any) -> dict[str, Any]:
        return values

    graph = StateGraph(state_type)
    graph.add_node("a", write)
    graph.add_edge(START, "a")
    graph.add_edge("a", END)
    app = graph.compile(checkpointer=InMemorySaver(serde=checkpoint_serde()))
    config: RunnableConfig = {"configurable": {"thread_id": "T-1"}}
    await app.ainvoke(dict.fromkeys(values), config)
    return dict((await app.aget_state(config)).values)


class GraphOnly(TypedDict):
    ownership_graph: OwnershipGraph | None


async def test_ownership_graph_survives_checkpoint_and_defaults_to_none() -> None:
    """R136 验收 3, 4: 旧检查点没有 ownership_graph 键时读到 None; 经检查点白名单序列化的 InMemorySaver 存取一轮后
    仍是 OwnershipGraph 实例 (不退化为 dict), 内容不变."""
    assert fill_defaults({})["ownership_graph"] is None
    back = (await checkpoint_round_trip(GraphOnly, {"ownership_graph": ownership_graph()}))["ownership_graph"]
    assert isinstance(back, OwnershipGraph) and back == ownership_graph()


class TermsOnly(TypedDict):
    engagement_terms: EngagementTerms | None


async def test_engagement_terms_survive_checkpoint_and_default_to_none() -> None:
    """R198 验收 4: 旧检查点没有 engagement_terms 键时读到 None; 经检查点白名单序列化的 InMemorySaver 存取一轮后仍是
    EngagementTerms 实例, 内容不变, 核对结果的状态仍是 TermsCheckStatus (不退化为字符串)."""
    assert fill_defaults({})["engagement_terms"] is None
    back = (await checkpoint_round_trip(TermsOnly, {"engagement_terms": engagement_terms()}))["engagement_terms"]
    assert type(back) is EngagementTerms and back == engagement_terms()
    assert all(type(c.status) is TermsCheckStatus for c in back.checks)


class CompletionOnly(TypedDict):
    representation_letter: WrittenRepresentationLetter | None
    compliance_checks: list[ComplianceCheck]
    governance_communications: list[GovernanceCommunication]
    independence_declaration: IndependenceDeclaration | None


async def test_completion_records_survive_checkpoint_as_enums() -> None:
    """R215 验收 5: 四类产出经检查点白名单序列化的 InMemorySaver 存取一轮后仍是各自的类, 内容不变; 状态等枚举字段仍是
    枚举 (is 比较为真, 不退化为字符串), 嵌套在列表里的声明, 检查项也一样."""
    written = {
        "representation_letter": representation_letter(),
        "compliance_checks": [compliance_check()],
        "governance_communications": [governance_communication()],
        "independence_declaration": independence_declaration(),
    }
    back = await checkpoint_round_trip(CompletionOnly, written)
    assert back == written
    letter, [check], [communication], declaration = (back[k] for k in written)
    assert isinstance(letter, WrittenRepresentationLetter) and letter.status is RepresentationStatus.obtained
    assert [c.status for c in letter.representations][-1] is ClauseStatus.refused_accepted
    assert isinstance(check, ComplianceCheck) and check.items[1].status is CheckItemStatus.failed
    assert check.failed_codes == ["E02"]
    assert isinstance(communication, GovernanceCommunication) and communication.form is CommunicationForm.written
    assert isinstance(declaration, IndependenceDeclaration) and declaration.form is DeclarationForm.no_contraventions
    assert declaration.status is ReportStatus.draft and isinstance(declaration.based_on, ObjectRef)


class PlansOnly(TypedDict):
    audit_plans: list[AuditPlan]


async def test_audit_plans_survive_checkpoint_and_default_to_empty() -> None:
    """R203 验收 6: 旧检查点没有 audit_plans 键时读到 []; 经检查点白名单序列化存取一轮后仍是 AuditPlan, 状态仍是
    PlanStatus 枚举, 嵌套的重要性选择与时间表日期不退化."""
    assert fill_defaults({})["audit_plans"] == []
    written = [audit_plan(version=2, status=PlanStatus.superseded), audit_plan(revision=2)]
    back = (await checkpoint_round_trip(PlansOnly, {"audit_plans": written}))["audit_plans"]
    assert back == written
    assert all(isinstance(p, AuditPlan) for p in back) and back[0].status is PlanStatus.superseded
    assert isinstance(back[1].materiality_choice, MaterialityChoice) and back[1].timetable[0].due == date(2026, 8, 14)


def test_audit_plans_merge_by_id_and_version() -> None:
    """R203 验收 6: audit_plans 按 upsert_by_id 并入: 驳回后的下一修订与旧修订的新版本并入后两份并存, 不整体替换;
    同 ID 版本更低的抛 VersionConflict."""
    state = initial_state(ENGAGEMENT, [], 1)
    state = apply_patch(state, {"audit_plans": [audit_plan()]})
    state = apply_patch(
        state, {"audit_plans": [audit_plan(revision=2), audit_plan(version=2, status=PlanStatus.superseded)]}
    )
    assert [(p.id, p.version, p.status) for p in state["audit_plans"]] == [
        ("PLAN-1", 2, PlanStatus.superseded),
        ("PLAN-2", 1, PlanStatus.draft),
    ]
    with pytest.raises(VersionConflict):
        apply_patch(state, {"audit_plans": [audit_plan()]})


class JudgmentsOnly(TypedDict):
    judgments: list[Judgment]


async def test_critic_reason_code_survives_checkpoint_as_enum() -> None:
    """R309: 判断的原因代码经检查点白名单序列化的 InMemorySaver 存取一轮后仍是 CriticReasonCode (不退化为字符串),
    技术细节不变; 没有代码的 (一致, 不一致, 人工建的, R309 之前的) 读回为空."""
    base = Judgment(
        id="J-REV-02-1-SI-1",
        procedure_id="REV-02",
        run_id="RUN-REV-02-1",
        subject_id="SI-1",
        conclusion="synthetic",
        confidence=0.8625,
        confidence_parts={"evidence": 0.7, "rule": 1.0, "llm": 0.9},
        reasoning_chain=[],
        evidence=[],
        counter_argument="synthetic",
        proposed_action=ProposedAction.accept,
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-1",
        created_at=NOW,
    )
    failed = base.model_copy(
        update={
            "id": "J-REV-02-1-SI-2",
            "critic_verdict": CriticVerdict.not_applicable,
            "critic_reason": "独立复核没有完成: 第一轮输出未通过 syntax 校验 (not json)",
            "critic_reason_code": CriticReasonCode.first_round_invalid,
            "critic_reason_detail": "syntax: not json",
        }
    )
    back = (await checkpoint_round_trip(JudgmentsOnly, {"judgments": [base, failed]}))["judgments"]
    assert back == [base, failed] and back[0].critic_reason_code is None
    assert back[1].critic_reason_code is CriticReasonCode.first_round_invalid


def test_completion_list_fields_merge_by_id_and_version() -> None:
    """R215 验收 5 (审核意见 01): compliance_checks 与 governance_communications 按 upsert_by_id 并入状态: 另一程序
    (另一次执行) 的写入与已有的并存, 不整体替换; 同 ID 版本更低的抛 VersionConflict."""
    state = initial_state(ENGAGEMENT, [], 1)
    state = apply_patch(state, {"compliance_checks": [compliance_check(version=2)]})
    state = apply_patch(state, {"compliance_checks": [compliance_check("FIN-12")]})
    assert [(c.id, c.version) for c in state["compliance_checks"]] == [("CHECK-FIN-10", 2), ("CHECK-FIN-12", 1)]
    with pytest.raises(VersionConflict):
        apply_patch(state, {"compliance_checks": [compliance_check()]})
    state = apply_patch(state, {"governance_communications": [governance_communication(version=2)]})
    state = apply_patch(state, {"governance_communications": [governance_communication(run_no=2)]})
    assert [c.id for c in state["governance_communications"]] == ["GC-RPT-07-1", "GC-RPT-07-2"]
    with pytest.raises(VersionConflict):
        apply_patch(state, {"governance_communications": [governance_communication()]})


def test_type_guard_names_classes_outside_state_modules() -> None:
    """验收 3: 新类型定义在 _STATE_MODULES 之外时报出类名, 含字段里套着的类型."""
    assert unregistered_types(StateWithLocalTypes) == ["LocalItem", "LocalKind", "LocalPlan"]


def test_top_level_enums_follow_annotations() -> None:
    """验收 3: 顶层枚举字段由注解推出 (现为 phase 与 opinion_route); 新加的顶层枚举字段自动纳入, 列表里的不算."""
    assert {"phase": Phase, "opinion_route": OpinionType} == TOP_LEVEL_ENUMS
    assert top_level_enums(StateWithLocalTypes) == {**TOP_LEVEL_ENUMS, "plan_kind": LocalKind}


def test_restore_enums_converts_strings_in_place() -> None:
    """Postgres 读回的字符串转回枚举, None 与缺键不动 (连库的读回见 test_persistence_postgres)."""
    channels: dict[str, Any] = {"phase": "P2_Executing", "opinion_route": None, "documents": []}
    restore_enums(channels)
    assert channels == {"phase": Phase.P2_Executing, "opinion_route": None, "documents": []}
    assert channels["phase"] is Phase.P2_Executing
    channels = {"opinion_route": "qualified"}
    restore_enums(channels)
    assert channels == {"opinion_route": OpinionType.qualified}


# ── 验收 4: 审计产出表登记 ──


def versioned_fields(state: Any) -> dict[str, type[Versioned]]:
    """元素为 Versioned 的状态字段 (单对象或列表) → 元素类型."""
    found: dict[str, type[Versioned]] = {}
    for key, hint in get_type_hints(state).items():
        hint, _ = strip_optional(hint)
        if get_origin(hint) is list:
            [hint] = get_args(hint)
        if isinstance(hint, type) and issubclass(hint, Versioned):
            found[key] = hint
    return found


def unregistered_outputs(state: Any) -> list[str]:
    """元素为 Versioned 而不在 OUTPUT_KINDS 中的字段, 写作 "字段: 类型名"."""
    kinds = {kind for names in OUTPUT_KINDS.values() for kind in names}
    return [f"{key}: {t.__name__}" for key, t in versioned_fields(state).items() if t.__name__ not in kinds]


def test_versioned_state_fields_have_output_tables() -> None:
    """验收 4: 状态中元素为 Versioned 的字段, 其类型都在 OUTPUT_KINDS 中 (现有 23 种全部在, R136 加 OwnershipGraph,
    R215 加签字前提的四种, R198 加 EngagementTerms, R203 加 AuditPlan)."""
    names = {t.__name__ for t in versioned_fields(EngagementState).values()}
    assert names >= {
        "AcceptanceScorecard",
        "AnalyticsResult",
        "MaterialityCandidates",
        "UncorrectedSchedule",
        "AuditObjective",
        "RiskItem",
        "Procedure",
        "ProcedureRun",
        "AuditException",
        "Judgment",
        "ReviewTask",
        "ReviewRecord",
        "GateDecision",
        "Confirmation",
        "Misstatement",
        "AuditReportVersion",
        "OwnershipGraph",
        "WrittenRepresentationLetter",  # 以下四种需求 R215 加
        "ComplianceCheck",
        "GovernanceCommunication",
        "IndependenceDeclaration",
        "EngagementTerms",
        "AuditPlan",  # 需求 R203 加
    }
    assert unregistered_outputs(EngagementState) == []


def test_output_guard_names_unregistered_field() -> None:
    """验收 4: 新字段的类型未登记到审计产出表时报出字段与类型."""
    assert unregistered_outputs(StateWithLocalTypes) == ["local_plans: LocalPlan"]


async def test_wrappers_pass_injected_parameters() -> None:
    """包装按原函数的签名转交 LangGraph 注入的参数 (审核 R82-01): 节点与路由声明 config 时, 包上之后照常收到."""

    class Small(TypedDict, total=False):
        x: int

    seen: list[str] = []

    async def node(state: Small, config: RunnableConfig) -> dict[str, Any]:
        seen.append(config["configurable"]["thread_id"])
        return {"x": 1}

    def route(state: Small, config: RunnableConfig) -> str:
        seen.append(config["configurable"]["thread_id"])
        return END

    graph = StateGraph(Small)
    graph.add_node("n", node_with_defaults(node))  # type: ignore[arg-type]
    graph.add_edge(START, "n")
    graph.add_conditional_edges("n", route_with_defaults(route), [END])  # type: ignore[arg-type]
    app = graph.compile(checkpointer=InMemorySaver())
    assert (await app.ainvoke({"x": 0}, {"configurable": {"thread_id": "T-1"}}))["x"] == 1
    assert seen == ["T-1", "T-1"]


def without(values: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in values.items() if k not in keys}


async def test_recovery_and_submit_read_checkpoint_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """须先恢复的判断, 恢复补做与复核决定提交读检查点状态时都补缺省值 (审核 R82-02): 检查点缺 gate_decisions,
    review_records 与 judgments (这些通道从未写过) 时照常判断, 不抛 KeyError."""
    s = await waiting_with_failure()
    engagement = await s.engagement_store.get(EID)
    assert engagement is not None
    snapshot = await s.runner.app.aget_state(CONFIG)
    old = snapshot._replace(values=without(snapshot.values, "gate_decisions", "review_records", "judgments"))
    stored = [*await s.output_store.latest(EID, GateDecision), *await s.output_store.latest(EID, ReviewRecord)]
    assert sorted(d.id for d in stored) == ["D-ACC", "D-PLN"]
    assert await resume_required(s.runner.deps, engagement, old) == "D-ACC, D-PLN"  # 都不在缺键的检查点里
    gates_only = old._replace(values={**old.values, "gate_decisions": snapshot.values["gate_decisions"]})
    assert await resume_required(s.runner.deps, engagement, gates_only) is None
    assert await catch_up_review(s.runner.app, s.runner.deps, engagement, old) is None

    async def stripped(config: Any, **kwargs: Any) -> Any:
        current = await original(config, **kwargs)
        return current._replace(values=without(current.values, "judgments"))

    original = s.runner.app.aget_state
    monkeypatch.setattr(s.runner.app, "aget_state", stripped)
    await s.submit.submit(EID, record(NORMAL, id="D-CONFIRM"))  # 效果按检查点已有的判断 ID 取人工判断编号
    monkeypatch.undo()
    await wait_idle(s.runner, EID)
    confirmed = await s.output_store.get(EID, Judgment, "J-REV-03-1-CON-001")
    assert confirmed is not None and confirmed.review_status is ReviewStatus.confirmed


def test_top_level_enum_unions_must_be_unambiguous() -> None:
    """顶层字段为两个枚举的联合或枚举 | str 时读回分不清, 推导即报错 (审核 R82-03)."""

    class TwoEnums(TypedDict):
        a: Phase | GateId | None

    class EnumOrText(TypedDict):
        b: Phase | str

    for state, key in ((TwoEnums, "a"), (EnumOrText, "b")):
        with pytest.raises(TypeError, match=f"状态字段 {key} "):
            top_level_enums(state)
