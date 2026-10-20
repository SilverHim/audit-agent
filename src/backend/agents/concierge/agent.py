"""A10 的程序执行: ConciergeAgent 按程序编号分派到各程序模块的 (执行, 解读). 返回字段见各程序模块.

新程序在本模块的 HANDLERS 登记一行 (程序编号 -> (执行, 解读)), 逻辑放在本包的独立模块 (如 RPT-03 在 report.py);
ConciergeAgent.procedures 由 HANDLERS 的键得出, 不另登记. 人工程序 (需求 R93) 不在 HANDLERS: registry.build_agents 把
它们映射到 A10 并填 owners, 派发见 manual.py, 新增人工程序不改这里. 审计师的对话入口在 agents/concierge_chat, 不经这里.
"""

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from backend.agents.base import AgentDeps, ManualHandler, ProcedureAgent, RunContext
from backend.agents.concierge import manual, report
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch

# 各程序的原始发现类型不同 (RPT-03 为报告草稿, 人工程序为派发材料), 分派表按程序配对, 不统一类型
Execute = Callable[[AgentDeps, RunContext], Awaitable[Any]]  # 执行一条程序, 返回原始发现
Interpret = Callable[[RunContext, Any], StatePatch]  # 把原始发现转成状态片段, 须含本次执行的终态
HANDLERS: dict[str, tuple[Execute, Interpret]] = {  # 程序编号 -> (执行, 解读)
    "RPT-03": (report.draft, report.report_patch),
}


class ConciergeAgent(ProcedureAgent[Any]):
    """A10: 按程序编号分派 (同 A6 的 handler 表), 各程序的执行与解读在本包的独立模块; 人工程序由它派发 (manual.py)."""

    agent_id: ClassVar[AgentId] = AgentId.A10  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset(HANDLERS)  # 负责的程序, 即 HANDLERS 登记的

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        # 人工程序 → 模板 owner 的 ManualHandler, 由 registry.build_agents 填 (registry.manual_owners); 派发时取材料
        self.owners: dict[str, ManualHandler] = {}

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读): 人工程序为派发 (manual.dispatcher), 其余查 HANDLERS. 都没有的抛
        NotImplementedError."""
        if procedure_id in self.owners:
            prepare, dispatch = manual.dispatcher(self.owners[procedure_id], self.deps)
            return prepare, dispatch
        if procedure_id not in HANDLERS:
            raise NotImplementedError(procedure_id)
        return HANDLERS[procedure_id]

    async def execute(self, ctx: RunContext) -> Any:
        execute, _ = self.handler(ctx.procedure.id)
        return await execute(self.deps, ctx)

    async def interpret(self, ctx: RunContext, findings: Any) -> StatePatch:
        _, interpret = self.handler(ctx.procedure.id)
        return interpret(ctx, findings)
