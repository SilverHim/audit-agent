"""A4：PLN-07、PLN-01。返回字段：analytics、materiality_candidates、audit_objectives、risk_register、exceptions、procedure_runs。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class RiskAssessorAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A4
    procedures: ClassVar[frozenset[str]] = frozenset({"PLN-07", "PLN-01"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
