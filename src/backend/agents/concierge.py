"""A10：RPT-03。返回字段：report_versions、review_tasks、procedure_runs。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class ConciergeAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A10
    procedures: ClassVar[frozenset[str]] = frozenset({"RPT-03"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
