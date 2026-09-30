"""A9: FIN-04. 返回字段: uncorrected_schedule, procedure_runs."""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class QCAgent(ProcedureAgent):
    """A9: 执行 FIN-04, 累计未更正错报并给出候选意见."""

    agent_id: ClassVar[AgentId] = AgentId.A9  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"FIN-04"})  # 负责的程序

    async def run(self, ctx: RunContext) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
