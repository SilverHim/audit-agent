"""A10 的程序执行: ConciergeAgent 按程序编号分派到各程序模块的 (执行, 解读). 返回字段见各程序模块.

新程序在本模块的 HANDLERS 登记一行 (程序编号 -> (执行, 解读)), 逻辑放在本包的独立模块 (如 RPT-03 在 report.py);
ConciergeAgent.procedures 由 HANDLERS 的键得出, 不另登记. 审计师的对话入口在 agents/concierge_chat, 不经这里.
"""

from collections.abc import Awaitable, Callable
from typing import ClassVar

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.concierge import report
from backend.contracts.models import AgentId, AuditReportVersion
from backend.contracts.state import StatePatch

Findings = AuditReportVersion  # 各程序执行的原始发现 (新程序的发现类型并入这里)
Execute = Callable[[AgentDeps, RunContext], Awaitable[Findings]]  # 执行一条程序, 返回原始发现
Interpret = Callable[[RunContext, Findings], StatePatch]  # 把原始发现转成状态片段, 须含本次执行的终态
HANDLERS: dict[str, tuple[Execute, Interpret]] = {  # 程序编号 -> (执行, 解读)
    "RPT-03": (report.draft, report.report_patch),
}


class ConciergeAgent(ProcedureAgent[Findings]):
    """A10: 按程序编号分派 (同 A6 的 handler 表), 各程序的执行与解读在本包的独立模块."""

    agent_id: ClassVar[AgentId] = AgentId.A10  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset(HANDLERS)  # 负责的程序, 即 HANDLERS 登记的

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        if procedure_id not in HANDLERS:
            raise NotImplementedError(procedure_id)
        return HANDLERS[procedure_id]

    async def execute(self, ctx: RunContext) -> Findings:
        execute, _ = self.handler(ctx.procedure.id)
        return await execute(self.deps, ctx)

    async def interpret(self, ctx: RunContext, findings: Findings) -> StatePatch:
        _, interpret = self.handler(ctx.procedure.id)
        return interpret(ctx, findings)
