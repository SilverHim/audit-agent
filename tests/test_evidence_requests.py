"""需求 R369 (Issue #65): 补充证据闭环的服务部分.

复核时要求补充证据 → 由判断整理证据请求, 任务转等证据 (不阻塞); 请求在发给客户/银行前可改; 按请求上传文件后请求置
已收到, 任务回待处理; 标无法取得时记审计范围受限; 管理层说明可登记为证据 (可靠性低于外部证据). 重判由
GraphDecisionSubmit 之外的编排路径另行处理, 本文件只测服务与请求整理.
"""

from datetime import UTC, datetime

from backend.contracts.models import (
    Decision,
    DocumentType,
    EvidenceRequest,
    Judgment,
    ProposedAction,
    ReasoningStep,
    Reliability,
    ReviewRecord,
    ReviewTask,
    ScopeLimitation,
    StepKind,
    TaskKind,
    TaskStatus,
    TrailKind,
    Verdict,
)
from backend.services.evidence_followup import (
    awaiting_task,
    link_uploaded_documents,
    register_representation,
    request_of,
    scope_limitation,
    unavailable,
)
from backend.services.review_queue import evidence_source
from tests.fakes import FakeRegistry, FakeTrail, make_deps

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def judgment(*, subject: str = "XJ-397", limitation: str | None = None) -> Judgment:
    steps = [
        ReasoningStep(kind=StepKind.fact, text="The conversion entry debits two bank accounts."),
    ]
    if limitation is not None:
        steps.append(ReasoningStep(kind=StepKind.limitation, text=limitation))
    return Judgment(
        id=f"J-GEN-02-1-{subject}",
        version=1,
        procedure_id="GEN-02",
        run_id="RUN-GEN-02-1",
        subject_id=subject,
        conclusion="The conversion balance cannot be agreed to the bank statements.",
        confidence=0.6,
        confidence_parts={"llm": 0.6},
        reasoning_chain=steps,
        evidence=[],
        counter_argument="Bank statements showing the opening balances would support the entry.",
        proposed_action=ProposedAction.more_evidence,
        verdict=Verdict.undetermined,
        autonomy_level="L2",
        performed_by="U-1",
        created_at=NOW,
    )


def task() -> ReviewTask:
    return ReviewTask(
        id="T-J-GEN-02-1-XJ-397-v1",
        version=1,
        kind=TaskKind.normal,
        procedure_id="GEN-02",
        target_type="Judgment",
        target_id="J-GEN-02-1-XJ-397",
        target_version=1,
        run_id="RUN-GEN-02-1",
        subject_id="XJ-397",
        created_at=NOW,
    )


def record() -> ReviewRecord:
    return ReviewRecord(
        id="D-MORE-EVIDENCE-1",
        task_id="T-J-GEN-02-1-XJ-397-v1",
        target_version=1,
        decision=Decision.more_evidence,
        reviewed_by="U-1",
        decided_at=NOW,
    )


def request(j: Judgment) -> EvidenceRequest:
    return request_of(record(), task(), j, now=NOW)


def test_request_takes_the_missing_evidence_from_the_limitation_step() -> None:
    """从判断的局限步骤取要什么, 认出银行, 关联对象为判断对象, 请求 ID 由决定 ID 定 (验收 2)."""
    limitation = "The bank statement for the period to 16 July 2026, or the Xero migration balance detail, is missing."
    j = judgment(limitation=limitation)
    request_ = request(j)
    assert request_.description == limitation
    assert request_.requested_from == "bank"
    assert request_.related_object_ids == ["XJ-397"]
    assert (request_.task_id, request_.judgment_id, request_.subject_id) == (
        "T-J-GEN-02-1-XJ-397-v1",
        "J-GEN-02-1-XJ-397",
        "XJ-397",
    )
    assert request_.id == "ER-D-MORE-EVIDENCE-1" and request_.status == "open"


def test_request_falls_back_to_the_counter_argument_and_client_default() -> None:
    """没有局限步骤时取反方论证; 认不出银行/管理层/第三方时向客户要."""
    j = judgment().model_copy(
        update={
            "conclusion": "The conversion balance cannot be agreed to the records.",
            "counter_argument": "A signed side letter would show the balance is supported.",
        }
    )
    request_ = request(j)
    assert request_.description == j.counter_argument
    assert request_.requested_from == "client"
    assert evidence_source(j) == "client"


def test_awaiting_task_does_not_count_as_pending() -> None:
    """任务转等证据 (验收 3): 状态 awaiting_evidence, 不占待决, 关联请求."""
    updated = awaiting_task(task(), request(judgment(limitation="Missing bank statement.")))
    assert (updated.status, updated.version, updated.evidence_request_id) == (
        TaskStatus.awaiting_evidence,
        2,
        "ER-D-MORE-EVIDENCE-1",
    )


def test_scope_limitation_without_draft_preserves_optional_amount() -> None:
    """范围受限取判断的错报草稿 (对象, 金额, 认定), 草稿为空时金额与认定为空."""
    j = judgment(limitation="Missing bank statement.")
    limitation = scope_limitation(request(j), j, actor="U-1", now=NOW, reason="Bank refused")
    assert (limitation.subject_id, limitation.assertion, limitation.amount_cents) == ("XJ-397", None, None)
    assert (limitation.id, limitation.reason) == ("SL-ER-D-MORE-EVIDENCE-1", "Bank refused")


async def test_uploaded_evidence_links_to_the_request_and_reopens_the_task() -> None:
    """按请求上传文件 (验收 4 的数据部分): 请求置 received 并记文件, 任务回待处理; 留痕可查."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    eid = "ENG-2026-001"
    j, t = judgment(limitation="Missing bank statement."), task()
    request_ = request(j)
    await deps.output_store.put(eid, [j, t, request_], actor="U-1")
    document = await deps.evidence_store.save(
        eid,
        "statement.pdf",
        b"%PDF-1.4 synthetic",
        type=DocumentType.bank_statement,
        reliability=Reliability.external_copy,
    )
    updated = await link_uploaded_documents(deps, eid, request_.id, [document], actor="U-1")
    assert updated is not None and updated.status == "received" and updated.document_ids == [document.id]
    stored_task = await deps.output_store.get(eid, ReviewTask, t.id)
    assert stored_task is not None and stored_task.status is TaskStatus.pending
    linked = await deps.financial_store.documents(eid, related_id="XJ-397")
    assert [d.id for d in linked] == [document.id]
    assert any(e.kind is TrailKind.data and e.payload["action"] == "received" for e in deps.trail.entries)


async def test_unavailable_records_a_scope_limitation_and_reopens_the_task() -> None:
    """无法取得 (验收 5): 请求置 unavailable, 另记范围受限, 原任务回待处理."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    eid = "ENG-2026-001"
    j, t = judgment(limitation="Missing bank statement."), task()
    request_ = request(j)
    await deps.output_store.put(eid, [j, t, request_], actor="U-1")
    updated = await unavailable(deps, eid, request_.id, actor="U-1", reason="Bank refused to provide it.")
    assert (updated.status, updated.unavailable_reason) == ("unavailable", "Bank refused to provide it.")
    stored_task = await deps.output_store.get(eid, ReviewTask, t.id)
    assert stored_task is not None and stored_task.status is TaskStatus.pending
    [limitation] = await deps.output_store.latest(eid, ScopeLimitation)
    assert limitation.request_id == request_.id and limitation.subject_id == j.subject_id
    assert limitation.assertion is not None and limitation.reason == "Bank refused to provide it."
    assert limitation.recorded_by == "U-1"


async def test_management_representation_is_registered_as_evidence() -> None:
    """管理层说明登记为证据 (验收 6): 存成证据文件, 可靠性为管理层说明 (低于外部证据)."""
    deps = make_deps(FakeRegistry(), FakeTrail())
    document = await register_representation(
        deps,
        "ENG-2026-001",
        person="Alex Morgan",
        stated_on="2026-10-22",
        content="The 840 balance includes a shareholder loan.",
        actor="U-1",
    )
    assert document.reliability is Reliability.management_representation
    assert deps.evidence_store.saves and deps.evidence_store.save_args[-1][3] is Reliability.management_representation
    assert any(e.payload["action"] == "management_representation" for e in deps.trail.entries)


def test_more_evidence_is_a_normal_task_decision() -> None:
    """要求补充证据对普通复核任务可选 (验收 1), 且不是可重跑决定."""
    from backend.services.review_queue import ALLOWED, RERUNNABLE

    assert Decision.more_evidence in ALLOWED[TaskKind.normal]
    assert {Decision.confirm, Decision.override, Decision.misstatement} <= ALLOWED[TaskKind.normal]
    assert TaskKind.normal not in RERUNNABLE
