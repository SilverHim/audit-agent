"""GET /users/me."""

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.deps import current_user
from backend.contracts.models import User

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def me(user: Annotated[User, Depends(current_user)]) -> User:
    return user
