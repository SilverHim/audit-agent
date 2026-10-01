"""A10: RPT-03. 返回字段: report_versions, review_tasks, procedure_runs."""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class ConciergeAgent(ProcedureAgent[None]):
    """A10: 执行 RPT-03, 生成审计报告初稿并按批注修订."""

    agent_id: ClassVar[AgentId] = AgentId.A10  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"RPT-03"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError(ctx.procedure.id)

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
