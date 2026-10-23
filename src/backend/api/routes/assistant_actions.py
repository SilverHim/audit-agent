"""用户可核对、授权或取消的助理操作方案. 每个接口均校验当前项目归属."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.deps import current_user, owned_engagement
from backend.api.errors import catch_api
from backend.contracts.assistant_actions import AssistantExecuteIn, AssistantPlan, AssistantPlanIn, AssistantReceipt
from backend.contracts.models import Engagement, User
from backend.services.assistant_actions import AssistantActions

router = APIRouter(prefix="/engagements/{eid}/assistant-actions", tags=["assistant_actions"])


def get_actions(request: Request) -> AssistantActions:
    actions: AssistantActions = request.app.state.assistant_actions
    return actions


@router.get("")
async def list_assistant_plans(
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    actions: Annotated[AssistantActions, Depends(get_actions)],
) -> list[AssistantPlan]:
    return await actions.plans(engagement.id)


@router.post("")
async def propose_assistant_action(
    body: AssistantPlanIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    actions: Annotated[AssistantActions, Depends(get_actions)],
) -> AssistantPlan:
    async with catch_api():
        return await actions.propose(engagement.id, user.id, body, "工作台生成的操作方案")


@router.post("/{plan_id}/execute")
async def execute_assistant_action(
    plan_id: str,
    body: AssistantExecuteIn,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    actions: Annotated[AssistantActions, Depends(get_actions)],
) -> AssistantReceipt:
    async with catch_api():
        return await actions.execute(engagement.id, plan_id, user.id, body.reason)


@router.post("/{plan_id}/cancel", status_code=204)
async def cancel_assistant_action(
    plan_id: str,
    engagement: Annotated[Engagement, Depends(owned_engagement)],
    user: Annotated[User, Depends(current_user)],
    actions: Annotated[AssistantActions, Depends(get_actions)],
) -> None:
    async with catch_api():
        await actions.cancel(engagement.id, plan_id, user.id)
