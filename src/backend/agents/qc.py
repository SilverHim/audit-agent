"""A9：FIN-04。返回字段：uncorrected_schedule、procedure_runs。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class QCAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A9
    procedures: ClassVar[frozenset[str]] = frozenset({"FIN-04"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
