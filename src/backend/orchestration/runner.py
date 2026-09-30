"""EngagementRunner (A 负责, M2 实现): 项目锁, 后台推进, recover 规则见 开发接口文档.md 第 7 节."""

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from backend.contracts.models import Phase, Procedure
from backend.contracts.state import EngagementState
from backend.services.procedure_library import ProcedureLibrary


def fixed_procedures(library: ProcedureLibrary) -> list[Procedure]:
    """固定集: P2 以外的程序在项目启动时实例化; P2 程序由 assemble_pack 按风险加入."""
    return [
        Procedure(id=t.id, phase=t.phase, template_commit=library.commit, autonomy_level=t.autonomy_level)
        for t in library.all()
        if t.phase is not Phase.P2_Executing
    ]


class GraphRunner:
    """EngagementRunner 的实现: 在后台推进图, 管项目锁与重启恢复."""

    def __init__(self, app: CompiledStateGraph[EngagementState]) -> None:
        self.app = app  # 编译好的主图

    @staticmethod
    def config(eid: str) -> RunnableConfig:
        return {"configurable": {"thread_id": eid}}

    async def start(self, eid: str) -> None:
        raise NotImplementedError

    async def resume(self, eid: str) -> None:
        raise NotImplementedError

    async def recover(self) -> None:
        raise NotImplementedError

    def is_running(self, eid: str) -> bool:
        raise NotImplementedError
