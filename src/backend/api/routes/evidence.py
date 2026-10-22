"""文件与证据: 上传与分类 (需求 R14), 文件登记, 合同与回函 (含修改与版本), ASIC 摘录的抽取结果 (需求 R290), 原件,
财务明细."""

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse

from backend.agents.base import AgentDeps
from backend.agents.ingest import extracted_from
from backend.api.deps import (
    current_user,
    get_data_service,
    get_decisions,
    get_deps,
    owned_engagement,
)
from backend.api.errors import api_error, catch_api
from backend.api.schemas import (
    ConfirmationView,
    DocumentDetailsIn,
    DocumentTypeIn,
    EvidenceRequestIn,
    EvidenceUnavailableIn,
    ManagementRepresentationIn,
    RegistryExtractView,
)
from backend.contracts.data import ContractVersion, ExtractionEdit, ExtractionRecord
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Confirmation,
    Contract,
    DocumentRef,
    Engagement,
    EvidenceRequest,
    ScopeLimitation,
    SourceType,
    User,
)
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.services import data_import, evidence_followup
from backend.services.data_service import DataService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/engagements/{eid}", tags=["evidence"])
INLINE = {"application/pdf", "image/png", "image/jpeg"}  # 浏览器能直接显示的; 其余 (Word, Excel, CSV) 以附件下载


class EvidenceFile(FileResponse):
    """OpenAPI 标成二进制, 生成的客户端按二进制读取; 实际的 Content-Type 取文件登记的媒体类型."""

    media_type = "application/octet-stream"


@router.get("/documents")
async def list_documents(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    related_id: Annotated[str | None, Query(description="只列关联对象含此 ID 的文件 (需求 R90)")] = None,
) -> list[DocumentRef]:
    """已登记的文件, 按文件名排序; 给 related_id 时只列关联对象含它的."""
    async with catch_api():
        return await deps.financial_store.documents(engagement.id, related_id=related_id)


@router.get("/contracts")
async def list_contracts(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[Contract]:
    async with catch_api():
        return await deps.financial_store.contracts(engagement.id)


@router.post("/documents")
async def upload_documents(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
    files: Annotated[list[UploadFile], File(description="PDF, DOCX, XLSX, PNG, JPG 等, 单个不超过 50 MB")],
    request_id: Annotated[str | None, Form()] = None,
) -> list[DocumentRef]:
    """上传文件, 返回各文件的登记 (同一文件重复上传返回已有的); 新文件随即由 A3 在后台分类与抽取.
    类型不支持 422 INVALID_VALUE; 单个超过 50 MB 413 FILE_TOO_LARGE; 锁定 423; 忙 409."""
    contents = []
    for upload in files:
        data = await upload.read(data_import.MAX_BYTES + 1)
        if len(data) > data_import.MAX_BYTES:
            raise api_error("FILE_TOO_LARGE", f"{upload.filename} 超过 50 MB")
        contents.append((upload.filename or "upload", data))
    async with catch_api():
        documents = await service.upload_documents(engagement.id, contents, actor=user.id, request_id=request_id)
    if request_id is not None:
        await _rejudge_after_evidence(decisions, engagement.id, request_id, user.id)
    return documents


@router.put("/documents/{document_id}/type", status_code=202)
async def set_document_type(
    document_id: str,
    body: DocumentTypeIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DocumentRef:
    """人工指定文件类型, 返回改后的登记 (classified); 合同, 回函与 ASIC 摘录随即在后台抽取. 理由选填 (Issue
    #61). 文件不存在 404; 文件还在智能导入的确认单里 (暂存), 或确认时没勾选而未导入 422 INVALID_VALUE; 锁定 423;
    忙 409."""
    async with catch_api():
        return await service.set_document_type(engagement.id, document_id, body.type, body.reason, user.id)


@router.put("/documents/{document_id}/details")
async def set_document_details(
    document_id: str,
    body: DocumentDetailsIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DocumentRef:
    """补登记文件的可靠性, 文件日期与关联对象 (只给要改的), 返回改后的登记; 留版本与留痕, 数据版本 +1. 理由选填
    (Issue #61). 文件不存在 404; 三项都没给 422 MISSING_FIELD; 暂存或未导入的文件, 没有要改的, 关联对象有空串或
    重复 422 INVALID_VALUE; 锁定 423; 忙 409. 同一修改的重试返回当前登记."""
    async with catch_api():
        [updated] = await service.set_document_details(engagement.id, {document_id: body}, user.id)
        return updated


@router.get("/documents/{document_id}/history")
async def document_history(
    document_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[ExtractionRecord]:
    """文件登记的各版 (补登记), 按版本升序; 没补登记过的以当前登记作唯一一版. 文件不存在 404."""
    async with catch_api():
        return await service.document_history(engagement.id, document_id)


@router.get("/evidence/{document_id}", response_class=EvidenceFile)
async def get_evidence(
    document_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> EvidenceFile:
    """原件, Content-Type 为登记的媒体类型. PDF 与图片在浏览器里显示 (PDF 前端带令牌下载后用 #page=N 跳页),
    Word, Excel, CSV 以附件下载."""
    document = next((d for d in await deps.financial_store.documents(engagement.id) if d.id == document_id), None)
    try:
        if document is None:
            raise FileNotFoundError(document_id)
        path = await deps.evidence_store.path(engagement.id, document_id)
    except (FileNotFoundError, ValueError) as exc:
        raise api_error("NOT_FOUND", document_id) from exc
    disposition = "inline" if document.media_type in INLINE else "attachment"
    return EvidenceFile(
        path, media_type=document.media_type, filename=document.file_name, content_disposition_type=disposition
    )


@router.put("/contracts/{contract_id}")
async def edit_contract(
    contract_id: str,
    body: ExtractionEdit,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> Contract:
    """修改合同抽取结果, 返回新版本. field 为 counterparty, counterparty_abn, amount_cents, sign_date,
    terms[n].text, terms[n].term_type (n 从 0 起). 理由选填 (Issue #61). 合同不存在 404; 字段或值不合法, 与当前
    相同 422 INVALID_VALUE; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.edit_contract(engagement.id, contract_id, body, user.id)


@router.get("/contracts/{contract_id}/history")
async def contract_history(
    contract_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[ContractVersion]:
    """合同的各版本, 按版本升序. 合同不存在 404."""
    async with catch_api():
        return await service.contract_history(engagement.id, contract_id)


@router.get("/registry-extracts")
async def list_registry_extracts(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[RegistryExtractView]:
    """审计师上传的各份 ASIC 摘录的当前抽取结果 (需求 R290, R306: 渠道 auditor_upload), 按文件 ID; 现在登记为
    registry_extract 而还没有抽取结果的不列."""
    async with catch_api():
        return [RegistryExtractView.of(r) for r in await service.registry_extracts(engagement.id)]


@router.put("/registry-extracts/{document_id}")
async def edit_registry_extract(
    document_id: str,
    body: ExtractionEdit,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> RegistryExtractView:
    """修改 ASIC 摘录的抽取结果, 返回新的一版. field 为 name, acn, abn, status, type, registered_date,
    registered_address, officeholders[n].name, officeholders[n].role, officeholders[n].appointed (n 从 0 起).
    理由选填 (Issue #61). 文件不是审计师上传的 ASIC 摘录或还没有抽取结果 404; 字段或值不合法, 与当前相同 422
    INVALID_VALUE; 锁定 423; 忙 409."""
    async with catch_api():
        return RegistryExtractView.of(await service.edit_registry_extract(engagement.id, document_id, body, user.id))


@router.get("/registry-extracts/{document_id}/history")
async def registry_extract_history(
    document_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> list[RegistryExtractView]:
    """ASIC 摘录抽取结果的各版, 按版本升序. 文件不是审计师上传的 ASIC 摘录或还没有抽取结果 404."""
    async with catch_api():
        return [RegistryExtractView.of(r) for r in await service.registry_history(engagement.id, document_id)]


@router.get("/records/{source_type}/{source_id}")
async def get_record(
    source_type: SourceType,
    source_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> dict[str, Any]:
    """一行财务明细. 金额字段仍是分."""
    async with catch_api():
        row = await deps.financial_store.record(engagement.id, source_type, source_id)
    if row is None:
        raise api_error("NOT_FOUND", source_id)
    return row


@router.get("/confirmations")
async def list_confirmations(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[ConfirmationView]:
    """各函证的最新版本, 另带 reply_extracted (当前回函经 DOC-06 成功抽取过)."""
    async with catch_api():
        found = await deps.output_store.latest(engagement.id, Confirmation)
    return [ConfirmationView(**c.model_dump(), reply_extracted=extracted_from(c, c.reply_document_id)) for c in found]


@router.put("/confirmations/{confirmation_id}")
async def edit_confirmation(
    confirmation_id: str,
    body: ExtractionEdit,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> Confirmation:
    """修改回函抽取结果, 返回函证新版本. field 为 confirming_party, balance_date, confirmed_balance_cents.
    只在图停在闸门或等复核时可改, 否则 409 NOT_EDITABLE. 理由选填 (Issue #61). 函证不存在 404; 字段或值不合法,
    与当前相同 422 INVALID_VALUE; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.edit_confirmation(engagement.id, confirmation_id, body, user.id)


# ── 补充证据闭环 (需求 R369, Issue #65) ──


@router.get("/evidence-requests")
async def list_evidence_requests(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[EvidenceRequest]:
    """补充证据请求, 按发出顺序; 任务与请求的状态供前端显示 (等待证据, 已收到, 无法取得)."""
    return await deps.output_store.latest(engagement.id, EvidenceRequest)


@router.get("/scope-limitations")
async def list_scope_limitations(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[ScopeLimitation]:
    """审计范围受限的记录 (需求 R369), 按记录顺序."""
    return await deps.output_store.latest(engagement.id, ScopeLimitation)


@router.patch("/evidence-requests/{request_id}")
async def edit_evidence_request(
    request_id: str,
    body: EvidenceRequestIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> EvidenceRequest:
    """审计师在发出前修改补充证据请求 (要什么, 向谁要, 关联对象); 只改给了的项. 请求不存在 404; 已收到或无法取得 409;
    锁定 423; 忙 409."""
    async with catch_api():
        return await decisions.edit_evidence_request(
            engagement.id,
            request_id,
            description=body.description,
            requested_from=body.requested_from,
            related_object_ids=body.related_object_ids,
            actor=user.id,
        )


@router.post("/evidence-requests/{request_id}/unavailable")
async def evidence_unavailable(
    request_id: str,
    body: EvidenceUnavailableIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> EvidenceRequest:
    """标补充证据无法取得 (需求 R369): 原复核任务回待处理, 另记一条审计范围受限. 理由选填. 请求不存在 404; 已结 409;
    锁定 423; 忙 409."""
    async with catch_api():
        return await decisions.mark_evidence_unavailable(engagement.id, request_id, body.reason, actor=user.id)


@router.post("/management-representations")
async def register_management_representation(
    body: ManagementRepresentationIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> DocumentRef:
    """登记管理层的书面或口头说明为证据 (需求 R369): 说明人, 时间与内容, 存成证据文件, 可靠性为管理层说明 (低于外部
    证据); 判断可以引用它. 锁定 423."""
    async with catch_api():
        if engagement.locked:
            raise api_error("ENGAGEMENT_LOCKED", engagement.id)
        return await evidence_followup.register_representation(
            deps, engagement.id, person=body.person, stated_on=body.stated_on, content=body.content, actor=user.id
        )


async def _rejudge_after_evidence(decisions: GraphDecisionSubmit, eid: str, request_id: str, actor: str) -> None:
    """补充证据上传后自动重判该对象 (需求 R369): 上传刚结束, A3 可能仍持项目锁处理文件, 稍等再试; 图不在可重判位置
    (如已过执行阶段) 时保留请求为已收到、任务回待处理, 由审计师带新证据复核, 不影响上传本身."""
    for _ in range(40):
        try:
            await decisions.rejudge_after_evidence(eid, request_id, actor=actor)
            return
        except DecisionRejected as error:
            if error.code != "ENGAGEMENT_BUSY":
                if error.code != "NOT_RERUNNABLE":
                    logger.info("补充证据请求 %s 未自动重判: %s", request_id, error)
                return
            await asyncio.sleep(0.5)
        except Exception:
            logger.exception("补充证据请求 %s 自动重判失败", request_id)
            return
    logger.info("补充证据请求 %s 等待项目空闲超时, 由审计师带新证据复核", request_id)
