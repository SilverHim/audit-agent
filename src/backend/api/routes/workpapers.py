"""工作底稿的记录 (需求 R330, Issue #50): 列表, 审计师改写结论, 补充说明与签署. 规则见 services.workpapers."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps, get_runner, owned_engagement
from backend.api.errors import catch_api
from backend.api.schemas import WorkpaperConclusionIn, WorkpaperNoteIn, WorkpaperSignoffIn
from backend.contracts.models import Engagement, User, Workpaper
from backend.orchestration.runner import GraphRunner
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
) -> list[Workpaper]:
    """项目的底稿记录 (各最新版本), 按档案顺序; 程序还没执行 (闸门还没决定) 的没有记录."""
    async with catch_api():
        return await service.list(engagement.id)


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
