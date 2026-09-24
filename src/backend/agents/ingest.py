"""A3：DOC-04、DOC-06。返回字段：documents、confirmations、review_tasks、procedure_runs；合同经 save_contract 写入。"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class IngestAgent(ProcedureAgent):
    agent_id: ClassVar[AgentId] = AgentId.A3
    procedures: ClassVar[frozenset[str]] = frozenset({"DOC-04", "DOC-06"})

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
