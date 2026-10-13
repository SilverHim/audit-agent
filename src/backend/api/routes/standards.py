"""GET /standards/{fragment_id}."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.agents.base import AgentDeps
from backend.api.deps import current_user, get_deps
from backend.api.errors import api_error
from backend.contracts.knowledge import StandardFragment
from backend.contracts.models import User

router = APIRouter(prefix="/standards", tags=["standards"])


@router.get("/{fragment_id}")
async def get_standard(
    fragment_id: str,
    _user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> StandardFragment:
    fragment = deps.library.fragment(fragment_id)
    if fragment is None:
        raise api_error("NOT_FOUND", fragment_id)
    return fragment
