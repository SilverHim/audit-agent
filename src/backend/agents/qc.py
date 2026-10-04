"""A9: FIN-04. 返回字段: uncorrected_schedule, procedure_runs."""

from datetime import UTC, datetime
from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import AgentId, OpinionType, UncorrectedItem, UncorrectedSchedule
from backend.contracts.state import StatePatch


class QCAgent(ProcedureAgent[UncorrectedSchedule]):
    """A9: 执行 FIN-04, 累计未更正错报并给出候选意见."""

    agent_id: ClassVar[AgentId] = AgentId.A9  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"FIN-04"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> UncorrectedSchedule:
        """按状态中的错报 (本版全部未更正) 编未更正错报汇总表 SCHEDULE, 规则见 FIN-04.yaml.

        高于明显微小门槛 (clearly_trivial_cents) 的错报计入 items, 等于或低于的只列 ID 于 excluded_trivial_ids;
        total_cents 为 items 金额之和, 不按高估低估抵销; 不低于整体重要性 (materiality_cents) 时候选意见为保留.
        两个门槛取本次执行的参数快照. 状态已有汇总表 (FIN-01 驳回后重做) 时出下一版.
        例: 一条 14200000 高估的错报, 重要性 12000000, 明显微小 600000 → items 一行, total 14200000, qualified.
        """
        params = ctx.run.params_snapshot
        materiality, trivial = params["materiality_cents"], params["clearly_trivial_cents"]
        items: list[UncorrectedItem] = []
        excluded: list[str] = []
        for misstatement in ctx.state["misstatements"]:
            if misstatement.amount_cents > trivial:
                items.append(
                    UncorrectedItem(
                        misstatement_id=misstatement.id,
                        amount_cents=misstatement.amount_cents,
                        direction=misstatement.direction,
                    )
                )
            else:
                excluded.append(misstatement.id)
        total = sum(item.amount_cents for item in items)
        previous = ctx.state["uncorrected_schedule"]
        return UncorrectedSchedule(
            id="SCHEDULE",
            version=previous.version + 1 if previous else 1,
            items=items,
            excluded_trivial_ids=excluded,
            total_cents=total,
            materiality_cents=materiality,
            candidate_opinion=OpinionType.qualified if total >= materiality else OpinionType.unqualified,
        )

    async def interpret(self, ctx: RunContext, findings: UncorrectedSchedule) -> StatePatch:
        return {"uncorrected_schedule": findings, "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}
