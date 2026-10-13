"""POST /auth/login."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from backend.agents.base import AgentDeps
from backend.api.deps import get_deps
from backend.api.errors import api_error
from backend.api.schemas import Token
from backend.core.security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> Token:
    """表单字段 username 为审计师邮箱."""
    found = await deps.users.by_email(form.username)
    if found is None or not verify_password(form.password, found[1]):
        raise api_error("BAD_CREDENTIALS", "邮箱或密码不正确")
    return Token(access_token=create_access_token(found[0].id))
