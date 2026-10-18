"""POST, GET /engagements; GET /engagements/{eid}; POST /engagements/{eid}/resume; GET client-profile."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps, get_query, get_runner, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import ClientProfileView, EngagementCreate, EngagementSummary
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditException,
    ClientProfile,
    Engagement,
    GateId,
    User,
)
from backend.contracts.views import EngagementOverview
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner

router = APIRouter(prefix="/engagements", tags=["engagements"])


@router.post("", status_code=201)
async def create_engagement(
    body: EngagementCreate,
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    runner: Annotated[GraphRunner, Depends(get_runner)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> EngagementOverview:
    """客户取数据集画像, 建项目后启动流程. 返回时后台已在跑, running 为真. 数据集不存在 404; 画像不合格或补造文件
    与快照不匹配 422, 不建项目."""
    try:
        profile = deps.datasets.client_profile(body.dataset)
    except FileNotFoundError as exc:
        raise api_error("NOT_FOUND", body.dataset) from exc
    except ValueError as exc:  # 含 SupplementMismatch 与画像校验失败 (ValidationError)
        raise api_error("INVALID_VALUE", str(exc)) from exc
    try:
        engagement = await deps.engagement_store.create(
            owner_id=user.id,
            period_start=body.period_start,
            period_end=body.period_end,
            dataset=body.dataset,
            profile=profile,
        )
    except ValueError as exc:
        raise api_error("INVALID_VALUE", str(exc)) from exc
    async with catch_api():
        await runner.start(engagement.id)
        return await query.overview(engagement.id)


@router.get("")
async def list_engagements(
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> list[EngagementSummary]:
    """只列出当前用户的项目. 闸门与待办数取总览, 与详情页一致."""
    owned = await deps.engagement_store.list(user.id)
    summaries: list[EngagementSummary] = []
    async with catch_api():
        for engagement in owned:
            overview = await query.overview(engagement.id)
            summaries.append(
                EngagementSummary(
                    id=engagement.id,
                    client_name=overview.client_name,
                    period_start=engagement.period_start,
                    period_end=engagement.period_end,
                    phase=overview.engagement.phase,
                    current_gate=overview.current_gate,
                    pending_tasks=overview.pending_tasks,
                    updated_at=overview.engagement.updated_at,
                )
            )
    return summaries


@router.get("/{eid}")
async def get_engagement(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> EngagementOverview:
    async with catch_api():
        return await query.overview(engagement.id)


@router.post("/{eid}/resume", status_code=202, response_model=None)
async def resume_engagement(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    runner: Annotated[GraphRunner, Depends(get_runner)],
) -> Response:
    """run_error 非空时重试. 项目忙返回 409 ENGAGEMENT_BUSY."""
    async with catch_api():
        await runner.resume(engagement.id)
    return Response(status_code=202)


@router.get("/{eid}/client-profile")
async def get_client_profile(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> ClientProfileView:
    """客户画像的最新版与第 1 版起的历史, 版本最高的承接评分卡及其例外.

    editable 与 PUT /client-profile 的可用条件一致: 停在 ACC-01, 未锁定, 后台没在运行, 没有待恢复的错误.
    """
    eid, client_id = engagement.id, engagement.client_id
    async with catch_api():
        profile = await deps.engagement_store.client_profile(client_id)
        if profile is None:
            raise api_error("NOT_FOUND", client_id)
        history: list[ClientProfile] = []
        for version in range(1, profile.version + 1):
            earlier = await deps.engagement_store.client_profile(client_id, version)
            if earlier is not None:
                history.append(earlier)
        scorecards = await deps.output_store.latest(eid, AcceptanceScorecard)
        scorecard = max(scorecards, key=lambda item: item.version, default=None)
        exceptions: list[AuditException] = []
        for exception_id in scorecard.exception_ids if scorecard else []:
            exception = await deps.output_store.get(eid, AuditException, exception_id)
            if exception is not None:
                exceptions.append(exception)
        overview = await query.overview(eid)
    editable = (
        overview.current_gate is GateId.ACC_01
        and not overview.engagement.locked
        and not overview.running
        and overview.run_error is None
    )
    return ClientProfileView(
        profile=profile,
        history=history,
        engagement_profile_version=overview.engagement.client_profile_version,
        scorecard=scorecard,
        exceptions=exceptions,
        editable=editable,
    )
