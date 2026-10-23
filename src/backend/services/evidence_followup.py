"""补充证据闭环 (需求 R369, Issue #65) 的服务: 证据请求的生成, 上传关联, 无法取得与范围受限, 管理层说明登记.

只读写 Store 与留痕, 由调用方 (DecisionSubmit, DataService, API 路由) 用; 不查编排状态.
"""

from datetime import UTC, datetime

from backend.agents.base import AgentDeps
from backend.contracts.errors import DataRejected, VersionConflict
from backend.contracts.models import (
    AgentId,
    Assertion,
    AuditException,
    AuditObjective,
    DocumentRef,
    DocumentType,
    EvidenceRequest,
    Judgment,
    MisstatementDraft,
    Procedure,
    Reliability,
    ReviewRecord,
    ReviewTask,
    ScopeLimitation,
    TaskStatus,
    TrailEntry,
    TrailKind,
)
from backend.services.review_queue import evidence_description, evidence_source, judgment_amount, subject_amount


def request_of(record: ReviewRecord, task: ReviewTask, judgment: Judgment, *, now: datetime) -> EvidenceRequest:
    """要求补充证据的决定写出的请求 (需求 R369): 从判断的局限步骤与反方论证整理要什么, 关联对象为判断对象, 向谁要按
    银行/管理层/其他第三方/客户的顺序认; 审计师可在发出前用 PATCH 改."""
    assert judgment.id == task.target_id  # normal 任务的目标是判断
    return EvidenceRequest(
        id=f"ER-{record.id}",
        task_id=task.id,
        judgment_id=judgment.id,
        procedure_id=task.procedure_id,
        subject_id=judgment.subject_id,
        description=evidence_description(judgment),
        related_object_ids=[judgment.subject_id],
        requested_from=evidence_source(judgment),
        requested_by=record.reviewed_by,
        requested_at=now,
    )


def awaiting_task(task: ReviewTask, request: EvidenceRequest) -> ReviewTask:
    """等证据的任务 (需求 R369): 状态 awaiting_evidence, 不算待你处理, 不阻塞流程; 记关联请求."""
    return task.model_copy(
        update={"version": task.version + 1, "status": TaskStatus.awaiting_evidence, "evidence_request_id": request.id}
    )


def evidence_trail(eid: str, request: EvidenceRequest, action: str, actor: str, **details: object) -> TrailEntry:
    """证据请求的 data 留痕 (发出, 修改, 收到, 无法取得), 防重复键为请求 ID + action."""
    return TrailEntry(
        engagement_id=eid,
        kind=TrailKind.data,
        actor=actor,
        procedure_id=request.procedure_id,
        summary=f"补充证据请求 {request.id}: {action}",
        idempotency_key=f"{request.id}:{action}",
        payload={
            "action": action,
            "request_id": request.id,
            "judgment_id": request.judgment_id,
            "status": request.status,
            **details,
        },
    )


def scope_limitation(
    request: EvidenceRequest,
    judgment: Judgment,
    *,
    actor: str,
    now: datetime,
    reason: str | None,
    amount_cents: int | None = None,
    assertion: Assertion | None = None,
) -> ScopeLimitation:
    """无法取得证据时记的范围受限 (需求 R369): 对象, 金额, 认定取自判断的错报草稿 (没有时为空), 不自动改意见."""
    draft: MisstatementDraft | None = judgment.misstatement_draft
    return ScopeLimitation(
        id=f"SL-{request.id}",
        request_id=request.id,
        judgment_id=judgment.id,
        procedure_id=request.procedure_id,
        subject_id=request.subject_id,
        assertion=draft.assertion if draft is not None else assertion,
        amount_cents=abs(draft.amount_cents)
        if draft is not None
        else (abs(amount_cents) if amount_cents is not None else None),
        reason=reason,
        recorded_by=actor,
        recorded_at=now,
    )


async def link_uploaded_documents(
    deps: AgentDeps, eid: str, request_id: str | None, documents: list[DocumentRef], *, actor: str
) -> EvidenceRequest | None:
    """按补充证据请求上传的文件登记到请求并置 received (需求 R369): 请求记文件, 原复核任务回到待处理, 审计师带着新证据
    重新复核; 没有该请求或已不是 open 时返回 None (普通上传不受影响). 只写 Store 与留痕."""
    if not request_id:
        return None
    store = deps.output_store
    request = await store.get(eid, EvidenceRequest, request_id)
    if request is None or request.status != "open":
        return None
    task = await store.get(eid, ReviewTask, request.task_id)
    if task is None:
        return None
    now = datetime.now(UTC)
    updated = request.model_copy(
        update={
            "version": request.version + 1,
            "status": "received",
            "document_ids": list(dict.fromkeys([*request.document_ids, *[d.id for d in documents]])),
            "resolved_at": now,
            "resolved_by": actor,
        }
    )
    reopened = task.model_copy(update={"version": task.version + 1, "status": TaskStatus.pending})
    async with store.transaction(eid):
        for document in documents:
            await deps.evidence_store.update_details(
                eid,
                document.id,
                reliability=None,
                document_date=None,
                related_ids=list(
                    dict.fromkeys([*document.related_ids, request.subject_id, *request.related_object_ids])
                ),
            )
        saved = await store.put(eid, [updated, reopened], actor=AgentId.A1)
        if saved != [updated, reopened]:
            raise VersionConflict(f"补充证据请求 {request_id} 或任务 {task.id} 已有新版本")
    await deps.trail.record(evidence_trail(eid, updated, "received", actor, document_ids=[d.id for d in documents]))
    return updated


async def unavailable(deps: AgentDeps, eid: str, request_id: str, *, actor: str, reason: str | None) -> EvidenceRequest:
    """审计师标补充证据无法取得 (需求 R369): 请求置 unavailable, 原复核任务回到待处理, 记一条范围受限.
    只写 Store 与留痕."""
    store = deps.output_store
    request = await store.get(eid, EvidenceRequest, request_id)
    if request is None:
        raise LookupError(request_id)
    if request.status != "open":
        raise VersionConflict(f"{request_id} 已不是待补充的请求")
    task = await store.get(eid, ReviewTask, request.task_id)
    judgment = await store.get(eid, Judgment, request.judgment_id)
    if task is None or judgment is None:
        raise LookupError(request_id)
    now = datetime.now(UTC)
    updated = request.model_copy(
        update={
            "version": request.version + 1,
            "status": "unavailable",
            "unavailable_reason": reason,
            "resolved_at": now,
            "resolved_by": actor,
        }
    )
    exceptions = {x.id: x for x in await store.latest(eid, AuditException)}
    amount = judgment_amount(judgment, exceptions, await subject_amount(deps.financial_store, eid, judgment))
    procedure = await store.get(eid, Procedure, request.procedure_id)
    objectives = await store.latest(eid, AuditObjective)
    assertion = next((o.assertion for o in objectives if procedure and o.id in procedure.objective_ids), None)
    if assertion is None:
        assertion = next(iter(deps.library.load(request.procedure_id).assertions), None)
    limitation = scope_limitation(
        updated, judgment, actor=actor, now=now, reason=reason, amount_cents=amount, assertion=assertion
    )
    reopened = task.model_copy(update={"version": task.version + 1, "status": TaskStatus.pending})
    async with store.transaction(eid):
        saved = await store.put(eid, [updated, limitation, reopened], actor=AgentId.A1)
        if saved != [updated, limitation, reopened]:
            raise VersionConflict(f"{request_id} 或任务 {task.id} 已有新版本")
    await deps.trail.record(evidence_trail(eid, updated, "unavailable", actor, reason=reason), independent=False)
    return updated


async def register_representation(
    deps: AgentDeps,
    eid: str,
    *,
    person: str,
    stated_on: str,
    content: str,
    actor: str,
    attachment_ids: list[str] | None = None,
    related_object_ids: list[str] | None = None,
) -> DocumentRef:
    """登记管理层说明为证据 (需求 R369): 写成一份文本文件登记入证据库, 可靠性为管理层说明 (低于外部证据), 判断引用时
    看得出是管理层说明. 判断可以用 document 指针引用它."""
    if not all(v.strip() for v in (person, stated_on, content)):
        raise DataRejected("MISSING_FIELD", "person, stated_on, content")
    attachments = attachment_ids or []
    known = {d.id for d in await deps.financial_store.documents(eid)}
    if any(i not in known for i in attachments):
        raise DataRejected("INVALID_VALUE", "附件须为本项目已登记的文件")
    text = (
        f"Management representation\nPerson: {person}\nStated on: {stated_on}\n"
        f"Attachments: {', '.join(attachments)}\n\n{content}\n"
    ).encode()
    document = await deps.evidence_store.save(
        eid,
        f"management-representation-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.txt",
        text,
        type=DocumentType.internal_record,
        reliability=Reliability.management_representation,
        media_type="text/plain",
        uploaded_by=actor,
        status="classified",
    )
    if related_object_ids:
        document = await deps.evidence_store.update_details(
            eid, document.id, reliability=None, document_date=None, related_ids=related_object_ids
        )
    await deps.trail.record(
        TrailEntry(
            engagement_id=eid,
            kind=TrailKind.data,
            actor=actor,
            summary=f"登记管理层说明 {document.id}",
            payload={
                "action": "management_representation",
                "document_id": document.id,
                "person": person,
                "attachment_ids": attachments,
                "related_object_ids": related_object_ids or [],
            },
        ),
        independent=False,
    )
    return document if isinstance(document, DocumentRef) else DocumentRef.model_validate(document)
