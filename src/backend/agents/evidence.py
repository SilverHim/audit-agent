"""A5：CSH-04。返回字段：confirmations、documents、procedure_runs。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class EvidenceAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A5
    procedures: ClassVar[frozenset[str]] = frozenset({"CSH-04"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
