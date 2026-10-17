"""路由共用的依赖: 取组装根注入的对象, 当前用户与项目归属校验 (C 负责)."""

from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.agents.base import AgentDeps
from backend.api.errors import api_error
from backend.contracts.models import Engagement, User
from backend.core.security import decode_access_token
from backend.orchestration.decisions import GraphDecisionSubmit
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner
from backend.services.llm_settings import LlmSettingsService

_bearer = HTTPBearer(auto_error=False)


def get_deps(request: Request) -> AgentDeps:
    deps: AgentDeps = request.app.state.deps
    return deps


def get_runner(request: Request) -> GraphRunner:
    runner: GraphRunner = request.app.state.runner
    return runner


def get_query(request: Request) -> GraphStateQuery:
    query: GraphStateQuery = request.app.state.query
    return query


def get_decisions(request: Request) -> GraphDecisionSubmit:
    decisions: GraphDecisionSubmit = request.app.state.decisions
    return decisions


def get_settings_service(request: Request) -> LlmSettingsService:
    service: LlmSettingsService = request.app.state.llm_settings
    return service


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> User:
    """校验 Bearer 令牌并返回用户. 缺失, 过期或对不上用户都是 401."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise api_error("BAD_CREDENTIALS", "缺少登录令牌")
    try:
        user_id = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise api_error("BAD_CREDENTIALS", "登录令牌无效") from None
    user = await deps.users.get(user_id)
    if user is None:
        raise api_error("BAD_CREDENTIALS", "用户不存在")
    return user


async def owned_engagement(
    eid: str,
    user: Annotated[User, Depends(current_user)],
    deps: Annotated[AgentDeps, Depends(get_deps)],
) -> Engagement:
    """项目不存在或不属于当前用户时都返回 404, 不区分这两种情况."""
    engagement = await deps.engagement_store.get(eid)
    if engagement is None or engagement.owner_id != user.id:
        raise api_error("NOT_FOUND", eid)
    return engagement
