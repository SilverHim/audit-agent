"""GET, POST /engagements/{eid}/deferrals: 稍后处理的标记与读取. 只写 deferral 留痕, 不改任务与编排状态."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps, get_query, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import DeferralIn, DeferralView, IdBody
from backend.contracts.models import Engagement, GateDecision, ReviewTask, TaskStatus, TrailEntry, TrailKind, User
from backend.orchestration.query import GraphStateQuery
from backend.services.deferrals import active_deferrals, deferred_at, is_active, latest_entries, target_of
from backend.services.trail_pages import all_of_kind

router = APIRouter(prefix="/engagements/{eid}", tags=["deferrals"])


@router.get("/deferrals")
async def list_deferrals(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> list[DeferralView]:
    """当前有效的标记, 按标记时间升序. 任务已决定, 闸门已作出决定, 或补交请求已收到的, 自动不在其中."""
    eid = engagement.id
    async with catch_api():
        entries = await all_of_kind(deps.trail, eid, TrailKind.deferral)
        tasks = {t.id: t for t in await deps.output_store.latest(eid, ReviewTask)}
        decisions = await deps.output_store.latest(eid, GateDecision)
        gate = (await query.overview(eid)).current_gate
        requests = {r.id: r for r in await deps.data.requests(eid)}
    views = []
    for entry in active_deferrals(entries, tasks, gate, decisions, requests):
        target_type, target_id = target_of(entry)
        views.append(
            DeferralView.model_validate(
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "deferred_by": entry.actor,
                    "deferred_at": deferred_at(entry),
                }
            )
        )
    return views


@router.post("/deferrals", status_code=202)
async def set_deferral(
    body: DeferralIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> IdBody:
    """标记 (deferred 为真) 或取消. 目标当前状态已与请求一致时不写新留痕, 返回最近一条的 ID.

    项目已锁定 423 ENGAGEMENT_LOCKED; 任务不存在 404 NOT_FOUND, 已决定 409 TASK_NOT_PENDING;
    闸门不是当前停留的 409 NOT_AT_GATE; 补交请求不存在 404 NOT_FOUND, 不是待补交 409 REQUEST_NOT_OPEN;
    取消从未标记过的目标 404 NOT_FOUND.
    """
    eid = engagement.id
    if engagement.locked:
        raise api_error("ENGAGEMENT_LOCKED", f"{eid} 已锁定")
    async with catch_api():
        tasks = {t.id: t for t in await deps.output_store.latest(eid, ReviewTask)}
        decisions = await deps.output_store.latest(eid, GateDecision)
        gate = (await query.overview(eid)).current_gate
        requests = {r.id: r for r in await deps.data.requests(eid)}
        if body.target_type == "review_task":
            task = tasks.get(body.target_id)
            if task is None:
                raise api_error("NOT_FOUND", body.target_id)
            if task.status is not TaskStatus.pending:
                raise api_error("TASK_NOT_PENDING", f"{body.target_id} 已决定")
        elif body.target_type == "data_request":
            request = requests.get(body.target_id)
            if request is None:
                raise api_error("NOT_FOUND", body.target_id)
            if request.status != "open":
                raise api_error("REQUEST_NOT_OPEN", f"{body.target_id} 不是待补交的请求")
        elif gate is None or gate.value != body.target_id:
            raise api_error("NOT_AT_GATE", f"当前不在 {body.target_id} 闸门", status=409)
        latest = latest_entries(await all_of_kind(deps.trail, eid, TrailKind.deferral)).get(
            (body.target_type, body.target_id)
        )
        active = latest is not None and is_active(latest, tasks, gate, decisions, requests)
        if active == body.deferred:
            if latest is None:
                raise api_error("NOT_FOUND", f"{body.target_id} 没有稍后处理标记")
            assert latest.id is not None  # 读回的留痕都有 ID
            return IdBody(id=latest.id)
        action = "稍后处理" if body.deferred else "取消稍后处理"
        trail_id = await deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.deferral,
                actor=user.id,
                summary=f"{action} {body.target_id}",
                payload={"target_type": body.target_type, "target_id": body.target_id, "deferred": body.deferred},
                created_at=datetime.now(UTC),
            )
        )
    return IdBody(id=trail_id)
