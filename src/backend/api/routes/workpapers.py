"""工作底稿的记录 (需求 R330, Issue #50): 列表, 审计师改写结论, 补充说明与签署. 规则见 services.workpapers."""

from asyncio import to_thread
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps, get_runner, owned_engagement
from backend.api.errors import catch_api
from backend.api.schemas import WorkpaperConclusionIn, WorkpaperNoteIn, WorkpaperSignoffIn
from backend.contracts.errors import DataRejected
from backend.contracts.models import Engagement, User, Workpaper
from backend.contracts.views import WorkpaperDetail, WorkpaperIndexEntry
from backend.orchestration.runner import GraphRunner
from backend.services.workpaper_downloads import XLSX, archive, disposition
from backend.services.workpaper_xlsx import filename, render
from backend.services.workpapers import WorkpaperService

router = APIRouter(prefix="/engagements/{eid}", tags=["workpapers"])


def get_workpapers(
    deps: Annotated[AgentDeps, Depends(get_deps)], runner: Annotated[GraphRunner, Depends(get_runner)]
) -> WorkpaperService:
    return WorkpaperService(deps, runner)


@router.get("/workpapers")
async def list_workpapers(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
) -> list[WorkpaperIndexEntry]:
    """项目的底稿记录 (各最新版本), 按档案顺序; 程序还没执行 (闸门还没决定) 的没有记录."""
    async with catch_api():
        return await service.list(engagement.id)


BINARY: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
}


@router.get("/workpapers/archive", response_class=Response, responses=BINARY)
async def download_archive(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
) -> Response:
    """整套最新快照的 Excel, 含索引. 条目显式 UTF-8, 索引使用相同相对文件名."""
    async with catch_api():
        snapshot = await service.ready(engagement.id)
        papers = sorted(snapshot.workpapers.values(), key=lambda p: service.deps.library.workpaper_order(p.ref))
        name = f"{engagement.id} {'审计档案' if engagement.working_language == 'zh' else 'Audit file'}.zip"
        return Response(
            await to_thread(archive, papers, engagement.working_language),
            media_type="application/zip",
            headers={"Content-Disposition": disposition(name, "workpapers.zip")},
        )


@router.get("/workpapers/{ref}")
async def get_workpaper(
    ref: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> WorkpaperDetail:
    async with catch_api():
        return await service.detail(engagement.id, ref, version)


@router.get("/workpapers/{ref}/file", response_class=Response, responses=BINARY)
async def download_workpaper(
    ref: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    async with catch_api():
        detail = await service.detail(engagement.id, ref, version)
        paper = detail.workpaper
        if paper.content is None:
            raise DataRejected("INVALID_VALUE", "这一历史版本没有记录内容快照")
        language = engagement.working_language
        return Response(
            await to_thread(render, paper, language),
            media_type=XLSX,
            headers={"Content-Disposition": disposition(filename(paper, language), "workpaper.xlsx")},
        )


@router.post("/workpapers/{ref}/conclusion")
async def conclude_workpaper(
    ref: str,
    body: WorkpaperConclusionIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
) -> Workpaper:
    """改写底稿结论, 返回新版本 (草稿照留; 已签署的改为须重新复核), 写 decision 留痕. 文字为空 422 MISSING_FIELD;
    底稿没有记录 404; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.conclude(engagement.id, ref, body.text, user.id)


@router.post("/workpapers/{ref}/notes")
async def note_workpaper(
    ref: str,
    body: WorkpaperNoteIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
) -> Workpaper:
    """补充底稿说明, 返回新版本; item 补齐该底稿可由补充说明补齐的必填项 (之后不再列在 missing). 文字为空 422
    MISSING_FIELD; item 不是可补齐的 422 INVALID_VALUE; 底稿没有记录 404; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.note(engagement.id, ref, body.text, body.item, user.id)


@router.post("/workpapers/{ref}/signoff")
async def sign_workpaper(
    ref: str,
    body: WorkpaperSignoffIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    service: Annotated[WorkpaperService, Depends(get_workpapers)],
) -> Workpaper:
    """签署底稿, 返回新版本 (status signed, 记签署人, 时间, 复核范围与意见), 写 decision 留痕. 不阻塞 FIN-01 与
    RPT-01. 复核范围不认识 422 (请求体校验); 底稿没有记录 404; 锁定 423; 忙 409."""
    async with catch_api():
        return await service.sign(engagement.id, ref, body.scope, body.opinion, user.id)
