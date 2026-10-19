"""GET misstatements, reports, trail."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.agents.base import AgentDeps
from backend.api.deps import get_deps, get_query, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import MisstatementReport, ReportBundle
from backend.contracts.models import (
    AuditReportVersion,
    Engagement,
    GateDecision,
    GateId,
    Misstatement,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
)
from backend.orchestration.query import GraphStateQuery

router = APIRouter(prefix="/engagements/{eid}", tags=["reports"])


@router.get("/misstatements")
async def get_misstatements(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> MisstatementReport:
    """汇总表取 SCHEDULE; 重要性与意见类型取项目总览."""
    async with catch_api():
        items = await deps.output_store.latest(engagement.id, Misstatement)
        schedule = await deps.output_store.get(engagement.id, UncorrectedSchedule, "SCHEDULE")
        overview = await query.overview(engagement.id)
    return MisstatementReport(
        items=items,
        schedule=schedule,
        materiality_cents=overview.materiality_cents,
        opinion_route=overview.opinion_route,
    )


@router.get("/reports")
async def get_reports(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> ReportBundle:
    """decisions 只含 RPT-01, 含退回批注与签字."""
    async with catch_api():
        versions = await deps.output_store.latest(engagement.id, AuditReportVersion)
        decisions = await deps.output_store.latest(engagement.id, GateDecision, gate=GateId.RPT_01)
    return ReportBundle(versions=sorted(versions, key=lambda item: item.revision), decisions=decisions)


@router.get("/trail")
async def list_trail(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
    kind: TrailKind | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TrailEntry]:
    async with catch_api():
        return await deps.trail.list(engagement.id, kind, limit, offset)


@router.get("/trail/{trail_id}")
async def get_trail(
    trail_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> TrailEntry:
    async with catch_api():
        entry = await deps.trail.get(engagement.id, trail_id)
    if entry is None:
        raise api_error("NOT_FOUND", trail_id)
    return entry
