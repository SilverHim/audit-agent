"""A9: FIN-04. 返回字段: uncorrected_schedule, procedure_runs."""

from datetime import UTC, datetime
from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import (
    AgentId,
    MisstatementStatus,
    OpinionType,
    UncorrectedItem,
    UncorrectedSchedule,
)
from backend.contracts.state import StatePatch
from backend.services.opinion import allowed_opinions
from backend.services.staleness import current_results

# FIN-04 能编入汇总表的错报状态: 未更正, 部分更正 (累计剩余额), 已更正 (只列 ID); 其余的出现即执行失败 (需求 R166).
# 按枚举成员的同一性判断, 与编表时的分支一致: 检查点读回退回 model_construct 时状态是字符串, 也整次失败, 不按全额累计
SCHEDULABLE = (MisstatementStatus.uncorrected, MisstatementStatus.partially_corrected, MisstatementStatus.corrected)


class QCAgent(ProcedureAgent[UncorrectedSchedule]):
    """A9: 执行 FIN-04, 累计未更正错报并给出候选意见."""

    agent_id: ClassVar[AgentId] = AgentId.A9  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({"FIN-04"})  # 负责的程序

    async def execute(self, ctx: RunContext) -> UncorrectedSchedule:
        """按状态中当前结果的错报 (过期重跑取代的执行引出的不计, services.staleness.current_results) 编未更正错报汇总表
        SCHEDULE, 规则见 FIN-04.yaml (需求 R166, 规格书 12.3 子状态机注).

        按错报状态累计: uncorrected 取错报金额, partially_corrected 取剩余额 (residual_cents); 累计额高于明显微小门槛
        (clearly_trivial_cents) 的计入 items (带方向与 origin), 等于或低于的只列 ID 于 excluded_trivial_ids;
        corrected 的只列 ID 于 corrected_ids. 还有其他状态的错报 (identified, communicated, verifying 未结清; 本版不写的
        evaluated, closed 同样不能累计) 时抛 ValueError, 不出汇总表: A1 记执行失败并建 failed 任务, 转人工 (兜底, 正常
        拓扑下错报都已结清). 功能 misstatement 关闭时错报一律为 uncorrected (旧口径), 结果同 R23.
        total_cents 为 items 金额之和, 不按高估低估抵销; 候选意见取 services.opinion.allowed_opinions (与 FIN-01 的校验
        同一函数; 持续经营结论本条传 None, 即金额路径: 不低于整体重要性 materiality_cents 时为保留, 需求 R184).
        两个门槛取本次执行的参数快照. 状态已有汇总表 (FIN-01 驳回后重做) 时出下一版.
        例: 一条 14200000 高估的未更正错报, 重要性 12000000, 明显微小 600000 → items 一行, total 14200000, qualified.
        """
        params = ctx.run.params_snapshot
        materiality, trivial = params["materiality_cents"], params["clearly_trivial_cents"]
        misstatements = current_results(ctx.state).misstatements
        unsettled = [f"{m.id} ({m.status})" for m in misstatements if not any(m.status is s for s in SCHEDULABLE)]
        if unsettled:
            raise ValueError(f"FIN-04: 还有不能累计的错报 (未结清或本版不写的状态): {', '.join(unsettled)}")
        items: list[UncorrectedItem] = []
        excluded: list[str] = []
        corrected: list[str] = []
        for misstatement in misstatements:
            if misstatement.status is MisstatementStatus.corrected:
                corrected.append(misstatement.id)
                continue
            amount = (
                misstatement.residual_cents
                if misstatement.status is MisstatementStatus.partially_corrected
                else misstatement.amount_cents
            )
            assert amount is not None  # partially_corrected 的剩余额由 Misstatement 的模型校验保证
            if amount > trivial:
                items.append(
                    UncorrectedItem(
                        misstatement_id=misstatement.id,
                        amount_cents=amount,
                        direction=misstatement.direction,
                        origin=misstatement.origin or "current_period",
                    )
                )
            else:
                excluded.append(misstatement.id)
        total = sum(item.amount_cents for item in items)
        previous = ctx.state["uncorrected_schedule"]
        schedule = UncorrectedSchedule(
            id="SCHEDULE",
            version=previous.version + 1 if previous else 1,
            items=items,
            excluded_trivial_ids=excluded,
            corrected_ids=corrected,
            total_cents=total,
            materiality_cents=materiality,
            candidate_opinion=OpinionType.unqualified,  # 占位: 候选由下一行按合计与重要性取
        )
        # 持续经营结论本条传 None (需求 R184); 接上已批准的 FIN-02 结论由 GC-10 做
        return schedule.model_copy(update={"candidate_opinion": allowed_opinions(schedule, None).candidate})

    async def interpret(self, ctx: RunContext, findings: UncorrectedSchedule) -> StatePatch:
        return {"uncorrected_schedule": findings, "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}
