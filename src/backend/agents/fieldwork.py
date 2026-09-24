"""A6：REV-08、REV-03、REV-02、CSH-05、CSH-02。返回字段：exceptions、judgments、review_tasks、confirmations、procedure_runs。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class FieldworkAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A6
    procedures: ClassVar[frozenset[str]] = frozenset({"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
