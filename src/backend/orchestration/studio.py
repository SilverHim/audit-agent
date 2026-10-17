"""供 `uv run langgraph dev` 在 LangGraph Studio 中调试主图 (仅限合成或公开数据). 检查点由 dev server 提供."""

from backend.core.config import get_settings
from backend.main import build_deps
from backend.orchestration.graph import compile_graph
from backend.orchestration.nodes import GraphNodes

_settings = get_settings()
graph = compile_graph(GraphNodes(build_deps(_settings), _settings.PARALLEL_CAP))
