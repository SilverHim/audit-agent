"""A6: REV-08, REV-03, REV-02, CSH-05, CSH-02.

返回字段: exceptions, judgments, review_tasks, confirmations, procedure_runs.
"""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class FieldworkAgent(ProcedureAgent[None]):
    """A6: 执行收入与货币资金的实质性程序, 产出例外与判断."""

    agent_id: ClassVar[AgentId] = AgentId.A6  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError(ctx.procedure.id)

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
