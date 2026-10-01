"""A3: DOC-04, DOC-06. 返回字段: documents, confirmations, review_tasks, procedure_runs; 合同经 save_contract 写入."""

from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.models import AgentId
from backend.contracts.state import StatePatch


class IngestAgent(ProcedureAgent[None]):
    """A3: 执行 DOC-04, DOC-06, 从合同与回函 PDF 抽取要素."""

    agent_id: ClassVar[AgentId] = AgentId.A3  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"DOC-04", "DOC-06"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError(ctx.procedure.id)

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError(ctx.procedure.id)
