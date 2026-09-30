"""路由共用的依赖: 取组装根注入的对象, 当前用户与项目归属校验 (C 负责)."""

from fastapi import Request

from backend.agents.base import AgentDeps
from backend.orchestration.runner import GraphRunner


def get_deps(request: Request) -> AgentDeps:
    deps: AgentDeps = request.app.state.deps
    return deps


def get_runner(request: Request) -> GraphRunner:
    runner: GraphRunner = request.app.state.runner
    return runner
