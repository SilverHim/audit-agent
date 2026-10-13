"""GET documents, contracts, evidence PDF, financial records, confirmations."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from backend.agents.base import AgentDeps
from backend.api.deps import get_deps, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.contracts.models import Confirmation, Contract, DocumentRef, Engagement, SourceType

router = APIRouter(prefix="/engagements/{eid}", tags=["evidence"])


class PdfFile(FileResponse):
    """OpenAPI 标成 application/pdf, 生成的客户端按二进制读取."""

    media_type = "application/pdf"


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


@router.get("/evidence/{document_id}", response_class=PdfFile)
async def get_evidence(
    document_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> PdfFile:
    """原件 PDF. 前端带令牌下载后用 #page=N 跳页."""
    try:
        path = await deps.evidence_store.path(engagement.id, document_id)
    except (FileNotFoundError, ValueError) as exc:
        raise api_error("NOT_FOUND", document_id) from exc
    return PdfFile(path, filename=path.name)


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
