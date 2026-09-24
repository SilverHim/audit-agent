"""A2：ACC-04。返回字段：acceptance_scorecard、exceptions、procedure_runs。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class ClientIntelAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A2
    procedures: ClassVar[frozenset[str]] = frozenset({"ACC-04"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
