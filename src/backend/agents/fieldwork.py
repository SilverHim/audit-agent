"""A6: REV-08, REV-03, REV-02, CSH-05, CSH-02.

返回字段: exceptions, judgments, review_tasks, confirmations, procedure_runs.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import AgentId
from backend.contracts.services import RuleResult
from backend.contracts.state import StatePatch

Execute = Callable[[RunContext], Awaitable[RuleResult]]  # 执行一条程序, 返回原始发现
Interpret = Callable[[RunContext, RuleResult], StatePatch]  # 把原始发现转成状态片段


class FieldworkAgent(ProcedureAgent[RuleResult]):
    """A6: 执行收入与货币资金的实质性程序, 产出例外与判断.

    按程序编号分派到各自的执行与解读; 判断类程序 (REV-03, REV-02, CSH-02) 尚未登记, 执行时报未实现.
    """

    agent_id: ClassVar[AgentId] = AgentId.A6  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"REV-08", "REV-03", "REV-02", "CSH-05", "CSH-02"})  # 负责的程序

    def handler(self, procedure_id: str) -> tuple[Execute, Interpret]:
        """程序编号对应的 (执行, 解读). 没有登记的程序抛 NotImplementedError."""
        handlers: dict[str, tuple[Execute, Interpret]] = {
            "REV-08": (self.run_rules, self.rule_exceptions),
            "CSH-05": (self.run_rules, self.rule_exceptions),
        }
        if procedure_id not in handlers:
            raise NotImplementedError(procedure_id)
        return handlers[procedure_id]

    async def execute(self, ctx: RunContext) -> RuleResult:
        execute, _ = self.handler(ctx.procedure.id)
        return await execute(ctx)

    async def interpret(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        _, interpret = self.handler(ctx.procedure.id)
        return interpret(ctx, findings)

    async def run_rules(self, ctx: RunContext) -> RuleResult:
        """执行程序 YAML 中的规则, 参数取本次执行的参数快照. RuleError 原样上抛, 由 A1 按执行失败处理."""
        return await self.deps.rules.run(ctx.engagement.id, ctx.template, ctx.run, ctx.run.params_snapshot)

    def rule_exceptions(self, ctx: RunContext, findings: RuleResult) -> StatePatch:
        """纯规则程序 (REV-08, CSH-05): 例外原样返回, 不作判断, 不建复核任务; 由之后的判断程序归因."""
        return {"exceptions": list(findings.exceptions), "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}
