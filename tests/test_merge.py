"""Agent 片段合并前的校验 (orchestration/merge.py). 场景: A6 执行 REV-03 (第 1 次), 程序包含 REV-08 与 REV-03."""

import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from backend.agents.client_intel import ClientIntelAgent
from backend.agents.registry import AGENT_CLASSES
from backend.contracts.models import (
    AcceptanceScorecard,
    AgentId,
    Assertion,
    AuditException,
    AuditReportVersion,
    AutonomyLevel,
    CheckItem,
    CheckItemStatus,
    ComplianceCheck,
    Direction,
    DocumentRef,
    DocumentType,
    ExceptionDisposition,
    Judgment,
    Misstatement,
    MisstatementType,
    OpinionType,
    Phase,
    PlanStatus,
    ProcedureRun,
    ProposedAction,
    Reliability,
    ReviewStatus,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskKind,
    TaskStatus,
    UncorrectedSchedule,
)
from backend.contracts.state import EngagementState, StatePatch, initial_state
from backend.core.config import get_settings
from backend.orchestration.merge import (
    AGENT_FIELDS,
    FIELD_REPLAY,
    SHAREABLE,
    STATE_HINTS,
    MergeRejected,
    check_patch,
    check_refs,
    field_type,
    output_problems,
)
from backend.orchestration.nodes import GraphNodes
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_ABN,
    NUMBAT_ASIC,
    FakeRegistry,
    FakeTrail,
    ScriptA6,
    audit_plan,
    compliance_check,
    engagement_terms,
    governance_communication,
    independence_declaration,
    make_ctx,
    make_deps,
    make_procedure,
    make_run,
    ownership_graph,
    representation_letter,
)

PACK = [make_procedure("REV-08", Phase.P2_Executing), make_procedure("REV-03", Phase.P2_Executing)]
RUN = make_run("REV-03")  # 本次执行, running
J1 = "J-REV-03-1-CON-001"


def finished(run: ProcedureRun, status: RunStatus = RunStatus.succeeded) -> ProcedureRun:
    return run.model_copy(update={"status": status, "ended_at": NOW})


def judgment(
    jid: str = J1, procedure_id: str = "REV-03", run_id: str = RUN.id, exception_ids: list[str] | None = None
) -> Judgment:
    return Judgment(
        id=jid,
        procedure_id=procedure_id,
        run_id=run_id,
        subject_id="CON-001",
        conclusion="Revenue recognised at delivery.",
        confidence=0.9,
        confidence_parts={},
        reasoning_chain=[],
        evidence=[],
        counter_argument="",
        proposed_action=ProposedAction.accept,
        exception_ids=exception_ids or [],
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-001",
        created_at=NOW,
    )


def exception(
    xid: str = "X-REV-08-1-R01-1", procedure_id: str = "REV-08", run_id: str = "RUN-REV-08-1"
) -> AuditException:
    return AuditException(
        id=xid,
        procedure_id=procedure_id,
        run_id=run_id,
        rule_id="R01",
        severity="high",
        source_ids=["SI-2026-0612"],
        evidence=[],
    )


def task(
    target_id: str = J1, target_type: TargetType = TargetType.Judgment, target_version: int = 1, run_id: str = RUN.id
) -> ReviewTask:
    return ReviewTask(
        id=f"T-{target_id}-v{target_version}",
        kind=TaskKind.normal,
        procedure_id="REV-03",
        target_type=target_type,
        target_id=target_id,
        target_version=target_version,
        run_id=run_id,
        created_at=NOW,
    )


def make_state(**fields: Any) -> EngagementState:
    state = initial_state(ENGAGEMENT, PACK, 1)
    state.update(fields)  # type: ignore[typeddict-item]
    return state


def a6_patch(**fields: Any) -> StatePatch:
    """A6 执行 REV-03 的合法片段: 一条判断, 一个复核任务, 本次执行成功. fields 覆盖或追加字段."""
    return {"judgments": [judgment()], "review_tasks": [task()], "procedure_runs": [finished(RUN)], **fields}


LIBRARY = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)


def declared(procedure_id: str) -> list[str]:
    """程序模板声明的产出字段 (check_patch 的 outputs)."""
    return LIBRARY.load(procedure_id).outputs


def reasons_of(
    patch: StatePatch, writer: AgentId = AgentId.A6, state: EngagementState | None = None, run: ProcedureRun = RUN
) -> list[str]:
    """按本次执行 run 校验片段: 不过时返回原因, 通过时返回空列表."""
    try:
        check_patch(
            patch,
            writer=writer,
            procedure_id=run.procedure_id,
            run_id=run.id,
            state=state or make_state(),
            outputs=declared(run.procedure_id),
        )
    except MergeRejected as e:
        return e.reasons
    return []


def test_valid_a6_patch_passes() -> None:
    assert reasons_of(a6_patch()) == []


# ── 验收 1: 字段归属 ──


def test_a2_cannot_write_confirmations() -> None:
    """A2 写不在其范围内的字段被拒 (R198 起 A2 可写 judgments, 改用 confirmations)."""
    patch = {"confirmations": [], "procedure_runs": [finished(RUN)]}

    reasons = reasons_of(patch, writer=AgentId.A2)

    assert reasons == ["A2 不能写字段 confirmations"]


def test_a6_cannot_write_acceptance_scorecard() -> None:
    scorecard = AcceptanceScorecard(id="SCORECARD", registry_checks=[], exception_ids=[], client_profile_version=1)

    reasons = reasons_of(a6_patch(acceptance_scorecard=scorecard))

    assert reasons == ["A6 不能写字段 acceptance_scorecard"]


@pytest.mark.parametrize("field", ["phase", "procedure_pack", "gate_decisions", "materiality_cents", "no_such_field"])
def test_agent_cannot_write_a1_or_unknown_fields(field: str) -> None:
    """A1 独占的字段与状态里没有的字段, 任何 Agent 都不能写."""
    assert reasons_of(a6_patch(**{field: []})) == [f"A6 不能写字段 {field}"]


def test_agent_fields_match_section_6_table() -> None:
    """第 6 节返回字段表逐项对照; procedure_runs 另行允许, 不在表里."""
    assert AGENT_FIELDS[AgentId.A2] == {
        "acceptance_scorecard",
        "exceptions",
        "engagement_terms",
        "judgments",
        "documents",
    }
    assert AGENT_FIELDS[AgentId.A3] == {"documents", "confirmations", "review_tasks"}
    assert AGENT_FIELDS[AgentId.A4] == {
        "analytics",
        "materiality_candidates",
        "audit_objectives",
        "risk_register",
        "exceptions",
        "audit_plans",  # 需求 R203 (PLN-15)
    }
    assert AGENT_FIELDS[AgentId.A5] == {
        "confirmations",
        "documents",
        "exceptions",
        "ownership_graph",
        "representation_letter",  # 需求 R215
    }
    assert AGENT_FIELDS[AgentId.A6] == {"exceptions", "judgments", "review_tasks", "confirmations"}
    # 需求 R156; R165 加 FIN-09 的 V02 例外
    assert AGENT_FIELDS[AgentId.A8] == {"judgments", "exceptions", "review_tasks", "misstatements"}
    # 需求 R215: A9 加合规检查与其例外, 任务; A10 加治理层沟通与独立性声明
    assert AGENT_FIELDS[AgentId.A9] == {"uncorrected_schedule", "compliance_checks", "exceptions", "review_tasks"}
    assert AGENT_FIELDS[AgentId.A10] == {
        "report_versions",
        "review_tasks",
        "governance_communications",
        "independence_declaration",
    }


def test_a5_may_write_ownership_graph_and_a6_may_not() -> None:
    """R136 验收 3: A5 执行 GEN-08 的片段带 ownership_graph 与例外通过; A6 的片段带它被拒, 原因与异常文本含字段名."""
    gen08 = make_run("GEN-08")
    graph = ownership_graph(gen08.id)
    found = exception("X-GEN-08-1-R01-1", "GEN-08", gen08.id)
    state = make_state(procedure_pack=[*PACK, make_procedure("GEN-08", Phase.P2_Executing)])
    patch = {"ownership_graph": graph, "exceptions": [found], "procedure_runs": [finished(gen08)]}
    outputs = ["ownership_graph", "exceptions", "documents"]
    check_patch(patch, writer=AgentId.A5, procedure_id="GEN-08", run_id=gen08.id, state=state, outputs=outputs)
    with pytest.raises(MergeRejected, match="merge_rejected: A6 不能写字段 ownership_graph"):
        check_patch(
            a6_patch(ownership_graph=graph),
            writer=AgentId.A6,
            procedure_id="REV-03",
            run_id=RUN.id,
            state=make_state(),
            outputs=[*declared("REV-03"), "ownership_graph"],
        )


def test_a2_may_write_engagement_terms_but_not_review_tasks() -> None:
    """R198 验收 5: A2 执行 ACC-08 的片段 (约定条款 TERMS, 建议判断, 例外与读过的约定文件, outputs 取 ACC-08.yaml)
    通过; 再带复核任务被拒 (闸门程序的判断不建任务, A2 的写入范围不含 review_tasks), 原因含字段名."""
    acc08 = make_run("ACC-08")
    advice = judgment("J-ACC-08-1-TERMS", "ACC-08", acc08.id).model_copy(update={"subject_id": "TERMS"})
    letter = DocumentRef(
        id="DOC-EL",
        type=DocumentType.engagement_letter,
        file_name="engagement_letter.pdf",
        sha256="2" * 64,
        page_count=3,
        reliability=Reliability.external_copy,
        status="extracted",
        uploaded_at=NOW,
    )
    patch: StatePatch = {
        "engagement_terms": engagement_terms(acc08.id),
        "judgments": [advice],
        "exceptions": [exception("X-ACC-08-1-T05-1", "ACC-08", acc08.id)],
        "documents": [letter],
        "procedure_runs": [finished(acc08)],
    }
    state = make_state(procedure_pack=[*PACK, make_procedure("ACC-08", Phase.P0_Acceptance)])
    check = {"writer": AgentId.A2, "procedure_id": "ACC-08", "run_id": acc08.id, "state": state}
    check_patch(patch, **check, outputs=declared("ACC-08"))  # type: ignore[arg-type]
    with pytest.raises(MergeRejected, match="merge_rejected: A2 不能写字段 review_tasks"):
        check_patch(
            {**patch, "review_tasks": [task(advice.id, run_id=acc08.id)]},
            **check,  # type: ignore[arg-type]
            outputs=[*declared("ACC-08"), "review_tasks"],
        )


def test_ownership_graph_must_come_from_this_run() -> None:
    """R136 审核意见 05: 关系图 (带 run_id, 不带 procedure_id) 的 run_id 须为本次执行: 指向 CSH-04 的执行 (在状态中)
    或不存在的执行, 都被拒, 原因含对象 ID 与两个 run_id."""
    gen08, csh04 = make_run("GEN-08"), finished(make_run("CSH-04"))
    state = make_state(procedure_pack=[*PACK, make_procedure("GEN-08", Phase.P2_Executing)], procedure_runs=[csh04])
    for wrong in (csh04.id, "RUN-GEN-08-9"):
        reason = f"ownership_graph 中 OWNERSHIP 的 run_id {wrong} 不是本次执行 {gen08.id}"
        with pytest.raises(MergeRejected, match=reason):
            check_patch(
                {"ownership_graph": ownership_graph(wrong), "procedure_runs": [finished(gen08)]},
                writer=AgentId.A5,
                procedure_id="GEN-08",
                run_id=gen08.id,
                state=state,
                outputs=["ownership_graph", "exceptions", "documents"],
            )


# ── R203: 总体审计计划 ──


def test_a4_writes_audit_plans_of_this_run() -> None:
    """R203 验收 6: A4 执行 PLN-15 第 2 次的片段 (下一修订 PLAN-2 v1 与旧修订置 superseded 的 PLAN-1 v2, 都记本次执行)
    通过; 旧修订的新版本仍记起草它的第 1 次执行时被拒 (merge.check_own_run); A6 的片段带 audit_plans 被拒."""
    first, second = finished(make_run("PLN-15")), make_run("PLN-15", run_no=2)
    state = make_state(
        procedure_pack=[*PACK, make_procedure("PLN-15", Phase.P1_Planning)],
        procedure_runs=[first],
        audit_plans=[audit_plan()],
    )
    retired = audit_plan(version=2, status=PlanStatus.superseded, run_id=second.id)
    patch = {"audit_plans": [audit_plan(revision=2), retired], "procedure_runs": [finished(second)]}
    check_patch(
        patch, writer=AgentId.A4, procedure_id="PLN-15", run_id=second.id, state=state, outputs=declared("PLN-15")
    )
    stale = {**patch, "audit_plans": [audit_plan(revision=2), retired.model_copy(update={"run_id": first.id})]}
    with pytest.raises(MergeRejected, match=f"audit_plans 中 PLAN-1 的 run_id {first.id} 不是本次执行 {second.id}"):
        check_patch(
            stale, writer=AgentId.A4, procedure_id="PLN-15", run_id=second.id, state=state, outputs=["audit_plans"]
        )
    with pytest.raises(MergeRejected, match="merge_rejected: A6 不能写字段 audit_plans"):
        check_patch(
            a6_patch(audit_plans=[audit_plan(run_id=RUN.id)]),
            writer=AgentId.A6,
            procedure_id="REV-03",
            run_id=RUN.id,
            state=make_state(),
            outputs=[*declared("REV-03"), "audit_plans"],
        )


# ── R215: 签字前提的四类审计产出 ──

COMPLETION_PACK = [*PACK, *(make_procedure(p, Phase.P3_Completion) for p in ("FIN-07", "FIN-10", "FIN-18"))]
COMPLETION_PACK += [make_procedure(p, Phase.P4_Reporting) for p in ("RPT-07", "RPT-08")]


def letter_check(exception_ids: list[str]) -> ComplianceCheck:
    """CHECK-FIN-18 第 1 次: W01 一项 (声明书缺 ASA 580 第 10 段的声明) 不通过."""
    item = CheckItem(
        code="W01",
        description="Representation is present in the letter",
        status=CheckItemStatus.failed,
        standard_ref="ASA 580.10",
        detail={"clause": "ASA580-P10"},
    )
    return ComplianceCheck(
        id="CHECK-FIN-18", procedure_id="FIN-18", run_id="RUN-FIN-18-1", items=[item], exception_ids=exception_ids
    )


def fin18_patch(**fields: Any) -> tuple[StatePatch, ProcedureRun]:
    """A9 执行 FIN-18 的合法片段: CHECK-FIN-18 (W01 不通过), W01 的例外与它的 unattributed 任务,
    本次执行成功."""
    run = make_run("FIN-18")
    found = exception("X-FIN-18-1-W01-1", "FIN-18", run.id)
    pending = ReviewTask(
        id=f"T-{found.id}-v1",
        kind=TaskKind.unattributed,
        procedure_id="FIN-18",
        target_type=TargetType.AuditException,
        target_id=found.id,
        target_version=1,
        run_id=run.id,
        subject_id="REPLETTER",
        created_at=NOW,
    )
    patch: StatePatch = {
        "compliance_checks": [letter_check([found.id])],
        "exceptions": [found],
        "review_tasks": [pending],
        "procedure_runs": [finished(run)],
    }
    return {**patch, **fields}, run


def check_a9(patch: StatePatch, run: ProcedureRun, state: EngagementState | None = None) -> None:
    outputs = ["compliance_checks", "exceptions", "review_tasks"]
    state = state or make_state(procedure_pack=COMPLETION_PACK)
    check_patch(patch, writer=AgentId.A9, procedure_id=run.procedure_id, run_id=run.id, state=state, outputs=outputs)


def test_a9_writes_compliance_check_with_its_exceptions_and_tasks() -> None:
    """R215 验收 6: A9 执行 FIN-18 的片段 (合规检查, 例外, unattributed 任务) 通过 check_patch."""
    check_a9(*fin18_patch())


def test_a9_may_not_write_governance_communications() -> None:
    """R215 验收 6: A9 片段含 governance_communications 时 check_patch 抛 MergeRejected, 原因含字段名; 在 outputs 里
    声明了也一样 (不在 A9 的写入范围)."""
    patch, run = fin18_patch(governance_communications=[governance_communication()])
    with pytest.raises(MergeRejected, match="merge_rejected: A9 不能写字段 governance_communications") as caught:
        check_patch(
            patch,
            writer=AgentId.A9,
            procedure_id=run.procedure_id,
            run_id=run.id,
            state=make_state(procedure_pack=COMPLETION_PACK),
            outputs=["compliance_checks", "exceptions", "review_tasks", "governance_communications"],
        )
    assert caught.value.reasons == ["A9 不能写字段 governance_communications"]


def test_compliance_check_references_are_checked() -> None:
    """R215 验收 6: ComplianceCheck.exception_ids 指向状态与片段中都没有的例外时 check_refs 报错; 例外只在状态中
    (之前的执行查出) 时通过. procedure_id 不在程序包, run_id 属于别的程序时也报错 (同判断与例外)."""
    patch, _ = fin18_patch(compliance_checks=[letter_check(["X-FIN-18-1-W01-1", "X-NONE"])])
    state = make_state(procedure_pack=COMPLETION_PACK)
    assert check_refs(patch, state) == ["ComplianceCheck CHECK-FIN-18 引用的例外 X-NONE 不存在"]
    earlier = exception("X-NONE", "FIN-18", "RUN-FIN-18-0")
    assert check_refs(patch, make_state(procedure_pack=COMPLETION_PACK, exceptions=[earlier])) == []

    foreign = compliance_check().model_copy(update={"run_id": "RUN-REV-08-1"})
    reasons = check_refs({"compliance_checks": [foreign]}, make_state(procedure_runs=[finished(make_run("REV-08"))]))
    assert reasons == [
        "ComplianceCheck CHECK-FIN-10 的 procedure_id FIN-10 不在程序包中",
        "ComplianceCheck CHECK-FIN-10 的 run_id RUN-REV-08-1 属于 REV-08, 与 procedure_id 不符",
    ]


@pytest.mark.parametrize(
    ("writer", "procedure_id", "field", "record"),
    [  # 样例的 run_id 都是该程序的第 1 次执行
        (AgentId.A5, "FIN-07", "representation_letter", representation_letter()),
        (AgentId.A10, "RPT-07", "governance_communications", governance_communication()),
        (AgentId.A10, "RPT-08", "independence_declaration", independence_declaration()),
    ],
    ids=["representation_letter", "governance_communications", "independence_declaration"],
)
def test_completion_records_come_from_their_writer_and_this_run(
    writer: AgentId, procedure_id: str, field: str, record: Any
) -> None:
    """R215 验收 6: 声明书 (A5), 治理层沟通与独立性声明 (A10) 由各自的写入者在本次执行写出时通过; 三者带 run_id 而
    不带 procedure_id, run_id 不是本次执行时被拒 (check_own_run, R136 审核意见 05 的规则)."""
    run = make_run(procedure_id)
    is_list = field == "governance_communications"

    def patch(value: Any) -> StatePatch:
        return {field: [value] if is_list else value, "procedure_runs": [finished(run)]}

    state = make_state(procedure_pack=COMPLETION_PACK)
    common: dict[str, Any] = {"writer": writer, "procedure_id": run.procedure_id, "run_id": run.id, "state": state}
    check_patch(patch(record), outputs=[field], **common)
    stale = record.model_copy(update={"run_id": "RUN-X-9"})
    with pytest.raises(MergeRejected, match=f"{field} 中 {record.id} 的 run_id RUN-X-9 不是本次执行 {run.id}"):
        check_patch(patch(stale), outputs=[field], **common)


def test_every_registered_agent_has_field_table() -> None:
    """注册的 Agent 与写入归属表一一对应 (A8 由需求 R158 注册后, 待落地表已删去)."""
    assert {cls.agent_id for cls in AGENT_CLASSES} == set(AGENT_FIELDS)


def test_field_type_follows_state_annotations() -> None:
    assert field_type("judgments") == (Judgment, True)
    assert field_type("acceptance_scorecard") == (AcceptanceScorecard, False)
    assert field_type("procedure_runs") == (ProcedureRun, True)


# ── 验收 2: 类型与 ID 唯一 ──


def test_exceptions_list_with_judgment_rejected() -> None:
    reasons = reasons_of(a6_patch(exceptions=[exception(), judgment("J-REV-03-1-CON-002")]))

    assert reasons == ["exceptions[1] 应为 AuditException, 实为 Judgment"]


def test_list_field_given_single_object_rejected() -> None:
    assert reasons_of(a6_patch(judgments=judgment())) == ["judgments 应为 Judgment 列表, 实为 Judgment"]


def test_single_field_given_wrong_type_rejected() -> None:
    patch = {"acceptance_scorecard": [], "procedure_runs": [finished(RUN)]}

    reasons = reasons_of(patch, writer=AgentId.A2, run=make_run("ACC-04"))  # ACC-04 声明了评分卡
    assert reasons == ["acceptance_scorecard 应为 AcceptanceScorecard, 实为 list"]


def test_duplicate_judgment_ids_rejected() -> None:
    reasons = reasons_of(a6_patch(judgments=[judgment(), judgment()]))

    assert reasons == [f"judgments 中 ID {J1} 重复"]


def test_same_id_in_different_fields_allowed() -> None:
    """ID 只在同一字段内比较: 例外与判断同用 X-1 不算重复."""
    patch = a6_patch(exceptions=[exception("X-1", procedure_id="REV-03", run_id=RUN.id)])
    patch["judgments"] = [judgment(jid="X-1")]
    patch["review_tasks"] = [task(target_id="X-1")]

    assert reasons_of(patch) == []


def test_version_lower_than_state_rejected() -> None:
    """状态已有 J1 v2 (已确认), 片段又给 v1: 并入时 reducer 会抛 VersionConflict."""
    state = make_state(judgments=[judgment().model_copy(update={"version": 2})])
    patch = a6_patch(review_tasks=[])

    assert reasons_of(patch, state=state) == [f"judgments 中 {J1} v1 低于状态中已有的 v2"]


def test_same_version_as_state_allowed() -> None:
    """重放时片段与状态中的对象同版本同内容, 并入时被忽略, 不算错."""
    state = make_state(judgments=[judgment()])

    assert reasons_of(a6_patch(), state=state) == []


def test_same_version_with_different_content_rejected() -> None:
    """状态已有 J1 v1, 片段给内容不同的 v1: 写 Store 时同号跳过, 修改会悄悄丢失, 应拒绝."""
    state = make_state(judgments=[judgment()])
    patch = a6_patch(judgments=[judgment().model_copy(update={"conclusion": "改了结论却没升版本"})])

    assert reasons_of(patch, state=state) == [f"judgments 中 {J1} v1 与状态中已有的同版本内容不同"]


def test_update_that_makes_state_pending_task_stale_rejected() -> None:
    """状态中有指向 J1 v1 的待决任务, 片段把 J1 改为 v2 而没有一并处理该任务: 任务过期, 应拒绝."""
    earlier = finished(make_run("REV-03", 1))
    state = make_state(judgments=[judgment(run_id=earlier.id)], review_tasks=[task(run_id=earlier.id)])
    state["procedure_runs"] = [earlier]
    rerun = make_run("REV-03", 2)
    patch = a6_patch(
        judgments=[judgment(run_id=earlier.id).model_copy(update={"version": 2})],
        review_tasks=[],
        procedure_runs=[finished(rerun)],
    )

    assert reasons_of(patch, state=state, run=rerun) == [f"Judgment {J1} 改为 v2, 待决任务 T-{J1}-v1 记的 v1 随之过期"]


def test_update_with_task_decided_in_same_patch_allowed() -> None:
    """片段把 J1 改为 v2, 同时把指向 v1 的任务置为已决定: 不算过期."""
    earlier = finished(make_run("REV-03", 1))
    state = make_state(judgments=[judgment(run_id=earlier.id)], review_tasks=[task(run_id=earlier.id)])
    state["procedure_runs"] = [earlier]
    decided = task(run_id=earlier.id).model_copy(update={"version": 2, "status": TaskStatus.decided})
    rerun = make_run("REV-03", 2)
    patch = a6_patch(
        judgments=[judgment(run_id=earlier.id).model_copy(update={"version": 2})],
        review_tasks=[decided],
        procedure_runs=[finished(rerun)],
    )

    assert reasons_of(patch, state=state, run=rerun) == []


# ── 验收 3: 外键 ──


def test_judgment_procedure_not_in_pack_rejected() -> None:
    other_run = finished(make_run("CSH-02"))
    patch = a6_patch(
        judgments=[judgment(procedure_id="CSH-02", run_id=other_run.id)],
        review_tasks=[],
    )
    state = make_state(procedure_runs=[other_run])

    assert reasons_of(patch, state=state) == [f"Judgment {J1} 的 procedure_id CSH-02 不在程序包中"]


def test_judgment_run_not_in_patch_or_state_rejected() -> None:
    patch = a6_patch(judgments=[judgment(run_id="RUN-REV-03-7")], review_tasks=[])

    assert reasons_of(patch) == [f"Judgment {J1} 的 run_id RUN-REV-03-7 不在执行记录中"]


def test_judgment_run_found_in_state_allowed() -> None:
    """run_id 指向状态中已有的执行记录 (如上一次执行) 也算成立."""
    earlier = finished(make_run("REV-03", run_no=1))
    current = make_run("REV-03", run_no=2)
    patch = {"judgments": [judgment(run_id=earlier.id)], "procedure_runs": [finished(current)]}

    assert reasons_of(patch, state=make_state(procedure_runs=[earlier]), run=current) == []


def test_judgment_run_of_other_procedure_rejected() -> None:
    rev08 = finished(make_run("REV-08"))
    patch = a6_patch(judgments=[judgment(run_id=rev08.id)], review_tasks=[])

    reasons = reasons_of(patch, state=make_state(procedure_runs=[rev08]))

    assert reasons == [f"Judgment {J1} 的 run_id RUN-REV-08-1 属于 REV-08, 与 procedure_id 不符"]


def test_exception_and_task_foreign_keys_checked_too() -> None:
    patch = a6_patch(
        exceptions=[exception("X-REV-03-1-R01-1", procedure_id="PLN-99", run_id=RUN.id)],
        review_tasks=[task(run_id="RUN-REV-03-9")],
    )

    assert reasons_of(patch) == [
        f"ReviewTask T-{J1}-v1 的 run_id RUN-REV-03-9 不在执行记录中",
        "AuditException X-REV-03-1-R01-1 的 procedure_id PLN-99 不在程序包中",
        "AuditException X-REV-03-1-R01-1 的 run_id RUN-REV-03-1 属于 REV-03, 与 procedure_id 不符",
    ]


def test_task_target_missing_rejected() -> None:
    patch = a6_patch(judgments=[], review_tasks=[task()])

    assert reasons_of(patch) == [f"ReviewTask T-{J1}-v1 的目标 Judgment {J1} 不存在"]


def test_task_target_found_in_state_allowed() -> None:
    """目标是状态中已有的判断 (如上一层建的), 片段只建任务."""
    state = make_state(judgments=[judgment()])

    assert reasons_of(a6_patch(judgments=[]), state=state) == []


@pytest.mark.parametrize(
    ("target_type", "target_id"),
    [(TargetType.AuditException, "X-REV-08-1-R01-1"), (TargetType.ProcedureRun, RUN.id)],
)
def test_task_target_looked_up_by_target_type(target_type: TargetType, target_id: str) -> None:
    """target_type 决定到哪个字段找 (例外在状态中, 执行记录在片段中); 别的字段里的同名对象不算."""
    found = a6_patch(judgments=[], review_tasks=[task(target_id, target_type)])
    state = make_state(exceptions=[exception()])
    assert reasons_of(found, state=state) == []

    wrong_type = a6_patch(review_tasks=[task(J1, target_type)])  # J1 只在 judgments 里
    assert reasons_of(wrong_type, state=state) == [f"ReviewTask T-{J1}-v1 的目标 {target_type} {J1} 不存在"]


def test_pending_task_target_version_mismatch_rejected() -> None:
    """待决任务记的目标版本与目标当前版本不符: 这个任务提交决定时必报 VERSION_MISMATCH."""
    patch = a6_patch(review_tasks=[task(target_version=2)])

    assert reasons_of(patch) == [f"ReviewTask T-{J1}-v2 待决, 其 target_version 2 与目标当前版本 v1 不符"]


def test_decided_task_replayed_with_newer_target_passes() -> None:
    """重放取回 Store 最新对象: confirm 后判断为 v2, 已决定的任务仍记复核时的 v1 (Codex R02-01)."""
    confirmed = judgment().model_copy(update={"version": 2, "review_status": ReviewStatus.confirmed})
    decided = task().model_copy(update={"version": 2, "status": TaskStatus.decided})

    assert reasons_of(a6_patch(judgments=[confirmed], review_tasks=[decided])) == []


def test_decided_task_target_version_above_current_rejected() -> None:
    """已决定任务的目标版本只能是历史版本, 高于目标当前版本说明数据错了."""
    decided = task(target_version=3).model_copy(update={"version": 2, "status": TaskStatus.decided})

    assert reasons_of(a6_patch(review_tasks=[decided])) == [
        f"ReviewTask T-{J1}-v3 的 target_version 3 高于目标当前版本 v1"
    ]


def test_exception_ids_must_exist() -> None:
    patch = a6_patch(judgments=[judgment(exception_ids=["X-REV-08-1-R01-1", "X-REV-08-1-R01-2"])])

    reasons = reasons_of(patch, state=make_state(exceptions=[exception()]))

    assert reasons == [f"Judgment {J1} 引用的例外 X-REV-08-1-R01-2 不存在"]


@pytest.mark.parametrize(
    ("procedure_id", "field", "material", "dangling"),
    [
        (
            "ACC-04",
            "acceptance_scorecard",
            AcceptanceScorecard(
                id="SCORECARD", registry_checks=[], exception_ids=["X-ACC-04-1-abn_active-1"], client_profile_version=1
            ),
            "AcceptanceScorecard SCORECARD 引用的例外 X-ACC-04-1-abn_active-1 不存在",
        ),
        (
            "ACC-08",
            "engagement_terms",
            engagement_terms().model_copy(update={"exception_ids": ["X-ACC-08-1-T05-1"]}),
            "EngagementTerms TERMS 引用的例外 X-ACC-08-1-T05-1 不存在",
        ),
    ],
)
def test_gate_material_exception_ids_must_exist(procedure_id: str, field: str, material: Any, dangling: str) -> None:
    """闸门材料 (评分卡; 约定条款, 审核 R198-01) 的 exception_ids 指向片段与状态中都没有的例外时拒绝, 原因含例外 ID."""
    run = make_run(procedure_id)
    patch = {field: material, "exceptions": [], "procedure_runs": [finished(run)]}
    state = initial_state(ENGAGEMENT, [make_procedure(procedure_id)], 1)

    with pytest.raises(MergeRejected) as info:
        check_patch(
            patch,  # type: ignore[arg-type]
            writer=AgentId.A2,
            procedure_id=procedure_id,
            run_id=run.id,
            state=state,
            outputs=declared(procedure_id),
        )

    assert info.value.reasons == [dangling]


# ── 验收 4: 本次执行的终态 ──


def test_missing_current_run_rejected() -> None:
    assert reasons_of({"procedure_runs": []}) == [f"procedure_runs 缺本次执行 {RUN.id}"]


def test_patch_without_procedure_runs_rejected() -> None:
    """空片段也不行: 本次执行的终态必须由 Agent 返回. 引用本次执行的判断同时报 run_id 不成立."""
    assert reasons_of({}) == [f"procedure_runs 缺本次执行 {RUN.id}"]
    assert reasons_of({"judgments": [judgment()]}) == [
        f"Judgment {J1} 的 run_id {RUN.id} 不在执行记录中",
        f"procedure_runs 缺本次执行 {RUN.id}",
    ]


def test_current_run_still_running_rejected() -> None:
    assert reasons_of(a6_patch(procedure_runs=[RUN])) == [f"本次执行 {RUN.id} 仍为 running, 应为 succeeded 或 failed"]


def test_failed_run_passes() -> None:
    """Agent 自己判定失败 (如 LLM 校验不过) 返回 failed Run 与 failed 任务, 片段合法, 由节点处理等待."""
    failed = finished(RUN, RunStatus.failed)
    failed_task = task(RUN.id, TargetType.ProcedureRun).model_copy(update={"kind": TaskKind.failed})

    assert reasons_of({"procedure_runs": [failed], "review_tasks": [failed_task]}) == []


def test_terminal_run_without_ended_at_rejected() -> None:
    no_end = RUN.model_copy(update={"status": RunStatus.succeeded})

    assert reasons_of(a6_patch(procedure_runs=[no_end])) == [f"本次执行 {RUN.id} 已为 succeeded 但缺 ended_at"]


def test_other_runs_in_patch_rejected() -> None:
    extra = finished(make_run("REV-03", run_no=2))

    reasons = reasons_of(a6_patch(procedure_runs=[finished(RUN), extra]))

    assert reasons == [f"procedure_runs 只能含本次执行 {RUN.id}, 多出 RUN-REV-03-2"]


def test_current_run_of_other_procedure_rejected() -> None:
    """片段里的本次执行属于别的程序: 节点按 ctx.run 校验, 报 procedure_id 不符."""
    with pytest.raises(MergeRejected) as info:
        check_patch(
            a6_patch(),
            writer=AgentId.A6,
            procedure_id="REV-08",
            run_id=RUN.id,
            state=make_state(),
            outputs=declared("REV-03"),
        )

    assert info.value.reasons == [f"本次执行 {RUN.id} 的 procedure_id 为 REV-03, 应为 REV-08"]


# ── 人工程序 owner 整理结果的片段 (run_id 为 None, 需求 R93) ──

COLLECTED = exception("X-REV-03-1-MAN-1", "REV-03", RUN.id)  # owner A6 整理出的例外, 挂在派发的那次执行下


@pytest.mark.parametrize(
    ("patch", "existing", "reasons"),
    [
        ({"exceptions": [COLLECTED]}, [], []),
        (
            {"exceptions": [COLLECTED], "procedure_runs": [finished(RUN)]},
            [],
            ["整理结果的片段不能带派发片段的字段 procedure_runs"],
        ),
        ({"review_tasks": [task()]}, [], ["整理结果的片段不能带派发片段的字段 review_tasks"]),
        (
            {"exceptions": [COLLECTED.model_copy(update={"run_id": "RUN-REV-03-9"})]},
            [],
            [f"AuditException {COLLECTED.id} 的 run_id RUN-REV-03-9 不在执行记录中"],
        ),
        (
            {"exceptions": [COLLECTED]},
            [COLLECTED.model_copy(update={"version": 2})],
            [f"exceptions 中 {COLLECTED.id} v1 低于状态中已有的 v2"],
        ),
    ],
    ids=["passes", "with_runs", "with_tasks", "unknown_run", "lower_version"],
)
def test_collected_patch_checked_without_a_run(
    patch: StatePatch, existing: list[AuditException], reasons: list[str]
) -> None:
    """run_id 为 None (owner 整理结果的片段): 不能带派发片段的字段 (执行记录, 任务; 后者即使模板 outputs 声明了也不行),
    不查终态, ID (版本不低于状态中的) 与外键 (run_id 在执行记录中) 照查. 状态里已有派发的那次执行."""
    state = make_state(procedure_runs=[finished(RUN)], exceptions=existing)
    try:
        check_patch(
            patch, writer=AgentId.A6, procedure_id="REV-03", run_id=None, state=state, outputs=declared("REV-03")
        )
    except MergeRejected as e:
        assert e.reasons == reasons
    else:
        assert reasons == []


# ── 原因汇总与异常文本 ──


def test_all_reasons_collected_and_message_prefixed() -> None:
    patch = a6_patch(judgments=[judgment(), judgment()], procedure_runs=[RUN])

    with pytest.raises(MergeRejected) as info:
        check_patch(
            patch,
            writer=AgentId.A6,
            procedure_id=RUN.procedure_id,
            run_id=RUN.id,
            state=make_state(),
            outputs=declared(RUN.procedure_id),
        )

    assert info.value.reasons == [
        f"judgments 中 ID {J1} 重复",
        f"本次执行 {RUN.id} 仍为 running, 应为 succeeded 或 failed",
    ]
    assert (
        str(info.value)
        == f"merge_rejected: judgments 中 ID {J1} 重复; 本次执行 {RUN.id} 仍为 running, 应为 succeeded 或 failed"
    )


def test_field_errors_stop_further_checks() -> None:
    """字段不过时不再查外键: 否则会对类型不对的对象取不存在的属性."""
    reasons = reasons_of(a6_patch(review_tasks=[judgment()], procedure_runs=[]))

    assert reasons == ["review_tasks[0] 应为 ReviewTask, 实为 Judgment"]


# ── 验收 5: 现有 ACC-04 片段通过 ──


@pytest.mark.parametrize(("abn_status", "exception_count"), [("Active", 0), ("Cancelled", 1)])
async def test_acc04_patch_passes(abn_status: str, exception_count: int) -> None:
    registry = FakeRegistry(
        {NUMBAT_ABN.abn: NUMBAT_ABN.model_copy(update={"status": abn_status})}, {NUMBAT_ASIC.acn: NUMBAT_ASIC}
    )
    agent = ClientIntelAgent(make_deps(registry, FakeTrail()))
    ctx = make_ctx(agent.deps)

    patch = await agent.run(ctx)

    assert len(patch["exceptions"]) == exception_count
    check_patch(
        patch,
        writer=agent.agent_id,
        procedure_id=ctx.run.procedure_id,
        run_id=ctx.run.id,
        state=ctx.state,
        outputs=ctx.template.outputs,
    )


# ── 后续需求的合法片段 (Codex R02 审核探针) ──


def test_doc04_succeeded_run_with_one_subject_failed_passes() -> None:
    """R03 验收 4: DOC-04 一份合同失败, 整次执行仍 succeeded, 片段带一条带 subject_id 的 failed 任务."""
    run = finished(make_run("DOC-04"))
    failed = task(run.id, TargetType.ProcedureRun, run_id=run.id).model_copy(
        update={
            "id": f"T-{run.id}-CON-002-v1",
            "procedure_id": "DOC-04",
            "kind": TaskKind.failed,
            "subject_id": "CON-002",
        }
    )
    patch = {"procedure_runs": [run], "review_tasks": [failed]}

    assert reasons_of(patch, AgentId.A3, initial_state(ENGAGEMENT, [make_procedure("DOC-04")], 1), run) == []


def test_later_layer_updates_previous_layer_exception_passes() -> None:
    """REV-02 判断失败, 把第 0 层 REV-08 的例外置 unattributed 出 v2 并建任务; 例外的 procedure_id, run_id 不变."""
    current = finished(make_run("REV-02"))
    old = exception()
    changed = old.model_copy(update={"version": 2, "disposition": ExceptionDisposition.unattributed})
    unattributed = task(old.id, TargetType.AuditException, target_version=2, run_id=current.id).model_copy(
        update={"procedure_id": "REV-02", "kind": TaskKind.unattributed, "subject_id": "SI-2026-0612"}
    )
    pack = [make_procedure("REV-08", Phase.P2_Executing), make_procedure("REV-02", Phase.P2_Executing)]
    state = initial_state(ENGAGEMENT, pack, 1)
    state["procedure_runs"] = [finished(make_run("REV-08"))]
    state["exceptions"] = [old]
    patch = {"procedure_runs": [current], "exceptions": [changed], "review_tasks": [unattributed]}

    assert reasons_of(patch, state=state, run=current) == []


def test_judgment_references_previous_layer_exception_passes() -> None:
    """REV-03 的判断引用第 0 层 REV-08 的例外, 片段不必重带该例外."""
    state = make_state(exceptions=[exception()], procedure_runs=[finished(make_run("REV-08"))])

    assert reasons_of(a6_patch(judgments=[judgment(exception_ids=[exception().id])]), state=state) == []


def test_report_revision_is_a_new_id_not_a_lower_version() -> None:
    """REPORT-2 v1 与状态中的 REPORT-1 v3 是不同对象, 不算版本倒退."""
    old = AuditReportVersion(
        id="REPORT-1", version=3, revision=1, opinion_type=OpinionType.qualified, sections={}, judgment_refs=[]
    )
    run = finished(make_run("RPT-03", run_no=2))
    revised = AuditReportVersion(
        id="REPORT-2", revision=2, opinion_type=OpinionType.qualified, sections={}, judgment_refs=[], run_id=run.id
    )
    state = initial_state(ENGAGEMENT, [make_procedure("RPT-03")], 1)
    state["report_versions"] = [old]

    assert reasons_of({"report_versions": [revised], "procedure_runs": [run]}, AgentId.A10, state, run) == []


# ── R85: 程序声明的产出 (outputs) 与重放取回方式 ──

X_BASE = "name: 自测\nphase: P2_Executing\ncycle: Journals\nengine: rule\nautonomy_level: L3\n"


def library_with(tmp_path: Path, **extra: str) -> ProcedureLibrary:
    """knowledge 目录的副本另加程序 YAML (文件名主干 → 内容)."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    for name, text in extra.items():
        (root / "procedures" / f"{name}.yaml").write_text(f"id: {name}\n{X_BASE}{text}", encoding="utf-8")
    return ProcedureLibrary(root)


def test_outputs_match_agent_fields_and_replay_registry() -> None:
    """R85 验收 1-3: 全部模板 (含未启用的) 的 outputs 都在 owner 的写入范围内且登记了取回方式, 其中包含下列 25 条 (含
    功能 journals 的 GEN-05, GEN-02, related_parties 的 GEN-08, GEN-03, acceptance 的 ACC-08, planning 的 PLN-15 与
    misstatement 的 FIN-03); 每个可由 Agent 写的字段都登记了取回方式, 登记的都是状态字段; 两条以上程序声明的共享
    字段都不是 singleton."""
    assert output_problems(LIBRARY) == []
    # 集合包含 (X-01): 之后新加的模板 (如 GEN-07, 需求 R177) 由各自的用例核对 outputs
    assert {t.id: t.outputs for t in LIBRARY.all(include_disabled=True)}.items() >= {
        "ACC-01": [],
        "ACC-04": ["acceptance_scorecard", "exceptions"],
        "ACC-08": ["engagement_terms", "judgments", "exceptions", "documents"],
        "CSH-02": ["judgments", "exceptions", "review_tasks", "confirmations"],
        "CSH-04": ["confirmations", "documents"],
        "CSH-05": ["exceptions"],
        "DOC-04": ["documents", "review_tasks"],
        "DOC-06": ["documents", "confirmations", "review_tasks"],
        "FIN-01": [],
        "FIN-03": ["judgments", "review_tasks", "misstatements"],  # 需求 R157, 功能 misstatement
        "FIN-04": ["uncorrected_schedule"],
        "GEN-02": ["judgments", "exceptions", "review_tasks"],
        "GEN-03": ["judgments", "exceptions", "review_tasks"],  # 需求 R139
        "GEN-05": ["exceptions"],
        "GEN-08": ["ownership_graph", "exceptions", "documents"],  # 需求 R137
        "GEN-09": [],
        "PLN-01": ["materiality_candidates", "audit_objectives", "risk_register"],
        "PLN-03": [],
        "PLN-07": ["analytics", "exceptions"],
        "PLN-15": ["audit_plans"],  # 需求 R203, 功能 planning
        "REV-02": ["judgments", "exceptions", "review_tasks"],
        "REV-03": ["judgments", "exceptions", "review_tasks"],
        "REV-08": ["exceptions"],
        "RPT-01": [],
        "RPT-03": ["report_versions", "review_tasks"],
    }.items()
    assert {t.id: t.document_types for t in LIBRARY.all(include_disabled=True) if t.document_types} == {
        "ACC-08": [DocumentType.engagement_letter, DocumentType.minutes],
        "CSH-04": [DocumentType.confirmation],
        "DOC-04": [DocumentType.contract],
        "DOC-06": [DocumentType.confirmation],
        "GEN-08": [DocumentType.registry_extract],
    }
    assert set().union(*AGENT_FIELDS.values()) <= set(FIELD_REPLAY) <= set(STATE_HINTS)
    declared = Counter(f for t in LIBRARY.all(include_disabled=True) for f in t.outputs)
    shared = {f for f, n in declared.items() if n > 1}
    # misstatements: FIN-03 与 FIN-09 (需求 R157, R164) 都写错报新版本
    assert shared == {"exceptions", "judgments", "review_tasks", "confirmations", "documents", "misstatements"}
    assert all(FIELD_REPLAY[f] in SHAREABLE for f in shared)


def test_outputs_problems_rejected_when_nodes_are_built(tmp_path: Path) -> None:
    """R85 验收 3: 临时模板 outputs 为没有登记的状态字段 (materiality_cents) 而 owner A6: GraphNodes 构造时抛
    ValueError, 含程序与字段 (R136 起 ownership_graph 已登记, 改用只在图状态中的字段); 两条程序都声明 singleton
    字段 (评分卡) 也报出."""
    library = library_with(tmp_path, **{"X-MC": "owner: A6\noutputs: [materiality_cents]\n"})
    assert output_problems(library) == [
        "X-MC: materiality_cents 不在 A6 的写入范围内",
        "X-MC: materiality_cents 没有登记重放取回方式 (merge.FIELD_REPLAY)",
    ]
    with pytest.raises(ValueError, match="X-MC: materiality_cents"):
        GraphNodes(make_deps(FakeRegistry(), FakeTrail(), library=library), 2)
    twice = library_with(tmp_path / "twice", **{"X-SC": "owner: A2\noutputs: [acceptance_scorecard]\n"})
    assert output_problems(twice) == ["ACC-04, X-SC: 都声明 acceptance_scorecard, 而其取回方式为 singleton"]
    types = library_with(
        tmp_path / "types",
        **{
            "X-DN": "owner: A3\noutputs: [documents]\n",
            "X-DT": "owner: A3\noutputs: [review_tasks]\ndocument_types: [contract]\n",
        },
    )
    assert output_problems(types) == [
        "X-DN: 声明产出 documents 而没写 document_types",
        "X-DT: 写了 document_types 而没声明产出 documents",
    ]


def test_document_types_not_checked_when_merging() -> None:
    """合并时不查文件类型 (document_types 只管重放): CSH-04 存档按哈希去重, 取回的可能是审计师先上传的同内容文件,
    其类型不一定是 confirmation (如分类失败仍为 other); 片段照常通过, 在线行为与改造前相同."""
    csh04 = make_run("CSH-04")
    uploaded = DocumentRef(
        id="DOC-1",
        type=DocumentType.other,
        file_name="BA-01-reply.pdf",
        sha256="1" * 64,
        page_count=1,
        reliability=Reliability.internal_copy,
        status="needs_manual",
        uploaded_at=NOW,
    )
    patch: StatePatch = {"confirmations": [], "documents": [uploaded], "procedure_runs": [finished(csh04)]}
    assert reasons_of(patch, writer=AgentId.A5, run=csh04) == []


def test_undeclared_output_rejected() -> None:
    """R85 验收 4: A6 可写函证, 但 REV-02 不声明: 片段含函证即拒绝, 原因写明程序与字段."""
    rev02 = make_run("REV-02")
    patch = {"confirmations": [], "procedure_runs": [finished(rev02)]}
    assert reasons_of(patch, run=rev02) == ["REV-02 未声明产出 confirmations"]
    assert reasons_of({"procedure_runs": [finished(rev02)]}, run=rev02) == []  # 只有本次执行时不受声明限制


async def test_run_agent_checks_template_outputs() -> None:
    """节点层接线: run_agent 把本程序模板的 outputs 交给合并校验. REV-02 的 Agent 片段多带函证 (A6 可写, REV-02
    不声明) 时片段整体丢弃, 返回 A1 代写的失败片段, 原因为 merge_rejected."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    nodes = GraphNodes(deps, 2)

    def with_confirmations(ctx: Any) -> StatePatch:
        return {"confirmations": [], "procedure_runs": [finished(ctx.run)]}

    patch, writer = await nodes.run_agent(ScriptA6(deps, with_confirmations), make_ctx(deps, "REV-02"))
    assert writer is AgentId.A1 and "confirmations" not in patch
    [run] = patch["procedure_runs"]
    assert (run.status, run.failure_reason) == (RunStatus.failed, "merge_rejected: REV-02 未声明产出 confirmations")


async def test_a1_failure_patch_not_limited_by_outputs() -> None:
    """R85 验收 4: Agent 抛异常时 A1 代写的失败片段 (执行记录与 failed 任务) 不受声明限制: REV-08 不声明任务,
    失败时照样带 failed 任务, 写入者为 A1."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    nodes = GraphNodes(deps, 2)

    def broken(ctx: Any) -> StatePatch:
        raise RuntimeError("boom")

    ctx = make_ctx(deps, "REV-08")
    assert "review_tasks" not in ctx.template.outputs
    patch, writer = await nodes.run_agent(ScriptA6(deps, broken), ctx)
    assert writer is AgentId.A1
    [failed] = patch["review_tasks"]
    assert (failed.kind, failed.procedure_id) == (TaskKind.failed, "REV-08")


# ── 需求 R156: A8 的写入归属 ──


def a8_patch() -> tuple[StatePatch, ProcedureRun, EngagementState]:
    """A8 执行 FIN-03 (错报分类) 的片段: 一条判断与它的复核任务, BA-091 错报的新版本 (已分类), 本次执行成功."""
    run = make_run("FIN-03")
    classified = judgment("J-FIN-03-1-M-D-CSH02-091", "FIN-03", run.id)
    review = task(classified.id, run_id=run.id).model_copy(update={"procedure_id": "FIN-03"})
    misstatement = Misstatement(
        id="M-D-CSH02-091",
        version=2,
        judgment_id="J-CSH-02-1-BA-091",
        exception_id=None,
        review_record_id="D-CSH02-091",
        type=MisstatementType.factual,
        account="Interest income (270)",
        assertion=Assertion.Completeness,
        amount_cents=4853,
        direction=Direction.understated,
        origin="current_period",
        classification_judgment_id=classified.id,
    )
    patch: StatePatch = {
        "judgments": [classified],
        "review_tasks": [review],
        "misstatements": [misstatement],
        "procedure_runs": [finished(run)],
    }
    return patch, run, make_state(procedure_pack=[*PACK, make_procedure("FIN-03", Phase.P3_Completion)])


def test_a8_writes_judgments_tasks_and_misstatements_only() -> None:
    """R156 验收 7: A8 片段写 judgments, review_tasks, misstatements 与本次执行记录通过; A8 再写 uncorrected_schedule
    被拒; A6 写 misstatements 被拒 (人认定错报经决定, 不经 A6), 原因含 Agent 与字段."""
    patch, run, state = a8_patch()
    outputs = ["judgments", "review_tasks", "misstatements"]
    check = {"procedure_id": "FIN-03", "run_id": run.id, "state": state}
    check_patch(patch, writer=AgentId.A8, outputs=outputs, **check)  # type: ignore[arg-type]
    schedule = UncorrectedSchedule(
        id="SCHEDULE",
        items=[],
        excluded_trivial_ids=[],
        total_cents=0,
        materiality_cents=32431,
        candidate_opinion=OpinionType.unqualified,
    )
    with pytest.raises(MergeRejected, match="merge_rejected: A8 不能写字段 uncorrected_schedule"):
        check_patch(
            {**patch, "uncorrected_schedule": schedule},
            writer=AgentId.A8,
            outputs=[*outputs, "uncorrected_schedule"],
            **check,  # type: ignore[arg-type]
        )
    with pytest.raises(MergeRejected, match="merge_rejected: A6 不能写字段 misstatements"):
        check_patch(patch, writer=AgentId.A6, outputs=outputs, **check)  # type: ignore[arg-type]
