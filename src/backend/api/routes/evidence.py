"""文件与证据: 上传与分类 (需求 R14), 文件登记, 合同与回函 (含修改与版本), 原件, 财务明细."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_data_service, get_deps, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import DocumentTypeIn
from backend.contracts.data import ContractVersion, ExtractionEdit
from backend.contracts.models import Confirmation, Contract, DocumentRef, Engagement, SourceType, User
from backend.services import data_import
from backend.services.data_service import DataService

router = APIRouter(prefix="/engagements/{eid}", tags=["evidence"])
INLINE = {"application/pdf", "image/png", "image/jpeg"}  # 浏览器能直接显示的; 其余 (Word, Excel, CSV) 以附件下载


class EvidenceFile(FileResponse):
    """OpenAPI 标成二进制, 生成的客户端按二进制读取; 实际的 Content-Type 取文件登记的媒体类型."""

    media_type = "application/octet-stream"


@router.get("/documents")
async def list_documents(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> list[DocumentRef]:
    async with catch_api():
        return await deps.financial_store.documents(engagement.id)


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
        return await service.upload_documents(engagement.id, contents, actor=user.id, request_id=request_id)


@router.put("/documents/{document_id}/type", status_code=202)
async def set_document_type(
    document_id: str,
    body: DocumentTypeIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> DocumentRef:
    """人工指定文件类型, 返回改后的登记 (classified); 合同与回函随即在后台抽取. 文件不存在 404; 理由为空
    422 MISSING_FIELD; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.set_document_type(engagement.id, document_id, body.type, body.reason, user.id)


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
    terms[n].text, terms[n].term_type (n 从 0 起). 合同不存在 404; 理由为空 422 MISSING_FIELD; 字段或值不合法,
    与当前相同 422 INVALID_VALUE; 锁定 423; 忙 409."""
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
) -> list[Confirmation]:
    async with catch_api():
        return await deps.output_store.latest(engagement.id, Confirmation)


@router.put("/confirmations/{confirmation_id}")
async def edit_confirmation(
    confirmation_id: str,
    body: ExtractionEdit,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[DataService, Depends(get_data_service)],
) -> Confirmation:
    """修改回函抽取结果, 返回函证新版本. field 为 confirming_party, balance_date, confirmed_balance_cents.
    只在图停在闸门或等复核时可改, 否则 409 NOT_EDITABLE. 函证不存在 404; 理由为空 422 MISSING_FIELD;
    字段或值不合法, 与当前相同 422 INVALID_VALUE; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.edit_confirmation(engagement.id, confirmation_id, body, user.id)
