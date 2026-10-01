"""A5: CSH-04. 返回字段: confirmations, documents, procedure_runs."""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class EvidenceAgent(ProcedureAgent[None]):
    """A5: 执行 CSH-04, 向银行发询证函并存档回函."""

    agent_id: ClassVar[AgentId] = AgentId.A5  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"CSH-04"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError(ctx.procedure.id)

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
