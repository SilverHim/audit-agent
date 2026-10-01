"""A4: PLN-07, PLN-01.

返回字段: analytics, materiality_candidates, audit_objectives, risk_register, exceptions, procedure_runs.
"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class RiskAssessorAgent(ProcedureAgent[None]):
    """A4: 执行 PLN-07, PLN-01, 做初步分析并算重要性候选."""

    agent_id: ClassVar[AgentId] = AgentId.A4  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"PLN-07", "PLN-01"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError(ctx.procedure.id)

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
