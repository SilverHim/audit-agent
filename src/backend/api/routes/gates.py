"""GET /engagements/{eid}/gate; POST gate-decisions; PUT client-profile."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Response

from backend.api.deps import current_user, get_decisions, get_query, owned_engagement
from backend.api.errors import api_error, catch_api
from backend.api.schemas import ClientProfileIn, GateDecisionIn, IdBody
from backend.contracts.models import ClientProfile, Engagement, GateDecision, User
from backend.contracts.views import GateView
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery

router = APIRouter(prefix="/engagements/{eid}", tags=["gates"])


@router.get("/gate")
async def get_gate(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    query: Annotated[GraphStateQuery, Depends(get_query)],
) -> GateView:
    """不在闸门时 404 NOT_AT_GATE. 停在闸门但材料缺失时 404 NOT_FOUND."""
    async with catch_api():
        view = await query.gate(engagement.id)
    if view is None:
        raise api_error("NOT_AT_GATE", "当前不在闸门", status=404)
    return view


@router.post("/gate-decisions", status_code=202)
async def submit_gate_decision(
    body: GateDecisionIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> IdBody:
    decision = GateDecision.model_validate(
        {**body.model_dump(), "id": f"D-{uuid4()}", "decided_by": user.id, "decided_at": datetime.now(UTC)}
    )
    async with catch_api():
        await decisions.submit(engagement.id, decision)
    return IdBody(id=decision.id)


@router.put("/client-profile", status_code=202, response_model=None)
async def update_client_profile(
    body: ClientProfileIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    decisions: Annotated[GraphDecisionSubmit, Depends(get_decisions)],
) -> Response:
    """只在 ACC-01 可用. 保存后流程重跑 ACC-04 并重新呈报."""
    profile = ClientProfile.model_validate({**body.model_dump(), "id": engagement.client_id})
    async with catch_api():
        await decisions.update_client_profile(engagement.id, profile, updated_by=user.id)
    return Response(status_code=202)
