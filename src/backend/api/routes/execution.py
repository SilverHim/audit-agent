"""执行事件流: 以留痕为持久化来源, 游标重连时补齐漏收的全部事件."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from backend.agents.base import AgentDeps
from backend.api.deps import get_deps, owned_engagement
from backend.api.errors import api_error
from backend.contracts.models import Engagement, TrailEntry, TrailKind
from backend.contracts.ports import AuditTrail

router = APIRouter(prefix="/engagements/{eid}", tags=["execution"])
KINDS = (TrailKind.execution, TrailKind.run, TrailKind.assistant_action)
PAGE = 100


async def events_since(trail: AuditTrail, eid: str, cursor: str | None) -> list[TrailEntry]:
    """首次取最近一页; 重连向前翻到游标, 不因一次产生超过一页的事件而丢失."""
    pages: list[list[TrailEntry]] = []
    before = None
    while True:
        page = await trail.list(eid, KINDS, PAGE, newest=True, before=before)
        ids = [e.id for e in page]
        if cursor is not None and cursor in ids:
            pages.append(page[ids.index(cursor) + 1 :])
            break
        pages.append(page)
        if cursor is None or len(page) < PAGE:
            break
        before = page[0].id
    return [entry for page in reversed(pages) for entry in page]


@router.get("/execution/stream", response_class=StreamingResponse)
async def stream_execution(
    request: Request,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    after: str | None = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """SSE execution 事件含 TrailEntry, id 为留痕 ID; 首次回放最近 100 条, 每 10 秒发送心跳."""
    cursor = after or last_event_id
    if cursor:
        entry = await deps.trail.get(engagement.id, cursor)
        if entry is None or entry.kind not in KINDS:
            raise api_error("NOT_FOUND", "执行事件游标不存在")

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            for entry in await events_since(deps.trail, engagement.id, cursor):
                cursor = entry.id
                payload = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {entry.id}\nevent: execution\ndata: {payload}\n\n"
            idle += 1
            if idle % 5 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
