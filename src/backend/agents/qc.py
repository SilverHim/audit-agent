"""A9: FIN-04. 返回字段: uncorrected_schedule, procedure_runs."""

from datetime import UTC, datetime
from typing import ClassVar

from backend.agents.base import ProcedureAgent, RunContext, finish_run
from backend.contracts.models import (
    AgentId,
    MisstatementStatus,
    ProposedAction,
    ReviewRecord,
    ReviewStatus,
    SuggestionLevel,
    UncorrectedItem,
    UncorrectedSchedule,
    UnrecognisedSuggestion,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.services.judgment_engine import judgment_key
from backend.services.opinion import allowed_opinions
from backend.services.staleness import current_results

# FIN-04 能编入汇总表的错报状态: 未更正, 部分更正 (累计剩余额), 已更正 (只列 ID); 其余的出现即执行失败 (需求 R166).
# 按枚举成员的同一性判断, 与编表时的分支一致: 检查点读回退回 model_construct 时状态是字符串, 也整次失败, 不按全额累计
SCHEDULABLE = (MisstatementStatus.uncorrected, MisstatementStatus.partially_corrected, MisstatementStatus.corrected)


def suggestion_level(amount: int, materiality: int, performance: int | None, trivial: int) -> SuggestionLevel:
    """AI 建议错报金额 (绝对值) 的档位: 不低于整体重要性为 material (同 services.opinion 的金额路径口径), 超过执行
    重要性 (未定为空时跳过) 为 above_performance, 超过明显微小临界值为 above_trivial, 其余 trivial. 纯函数."""
    if amount >= materiality:
        return "material"
    if performance is not None and amount > performance:
        return "above_performance"
    return "above_trivial" if amount > trivial else "trivial"


def unrecognised_suggestions(
    state: EngagementState, total: int, materiality: int, performance: int | None, trivial: int
) -> list[UnrecognisedSuggestion]:
    """AI 建议为错报但未认定的判断 (需求 R363), 按程序与对象排序: 当前结果中的 AI 判断 (不是人工判断 -H{n}), 建议动作
    为 misstatement 且带草稿, 已复核 (confirmed, 或 overridden: 审计师不同意), 而它与取代它的人工判断都没有当前结果的
    错报 (撤回作废的不算). 复核人与时间取处理它的任务上最近一条没撤回的决定 (没有决定为空). 金额档位见
    suggestion_level; would_qualify 为未更正合计 total 加上草稿金额后不低于整体重要性. 纯函数."""
    current = current_results(state)
    recognised = {m.judgment_id for m in current.misstatements if m.judgment_id is not None}
    withdrawn = {w.record_id for w in state["review_withdrawals"]}
    tasks = {t.id: t for t in state["review_tasks"]}
    latest: dict[str, ReviewRecord] = {}  # 判断 ID → 处理它的任务上最近一条没撤回的决定
    for record in sorted(state["review_records"], key=lambda r: r.decided_at):
        task = tasks.get(record.task_id)
        if task is not None and record.id not in withdrawn:
            latest[task.target_id] = record
    rows = []
    for judgment in current.judgments:
        draft = judgment.misstatement_draft
        if (
            judgment.proposed_action is not ProposedAction.misstatement
            or draft is None
            or judgment_key(judgment) != f"{judgment.procedure_id}:{judgment.subject_id}"  # 人工判断另带 :H{n}
            or judgment.review_status not in (ReviewStatus.confirmed, ReviewStatus.overridden)
            or {judgment.id, judgment.superseded_by} & recognised
        ):
            continue
        decided = latest.get(judgment.id)
        amount = abs(draft.amount_cents)
        rows.append(
            UnrecognisedSuggestion(
                judgment_id=judgment.id,
                procedure_id=judgment.procedure_id,
                subject_id=judgment.subject_id,
                task_id=decided.task_id if decided is not None else None,
                record_id=decided.id if decided is not None else None,
                decision=decided.decision if decided is not None else None,
                reviewed_by=decided.reviewed_by if decided is not None else None,
                decided_at=decided.decided_at if decided is not None else None,
                account=draft.account,
                account_code=draft.account_code,
                assertion=draft.assertion,
                amount_cents=draft.amount_cents,
                direction=draft.direction,
                level=suggestion_level(amount, materiality, performance, trivial),
                would_qualify=total + amount >= materiality,
            )
        )
    return sorted(rows, key=lambda r: (r.procedure_id, r.subject_id))


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
        同一函数, 需求 R184), 持续经营结论传 None, 即金额路径: 不低于整体重要性 materiality_cents 时为保留.
        两个门槛取本次执行的参数快照. 状态已有汇总表 (FIN-01 驳回后重做) 时出下一版.
        unrecognised 列 AI 建议为错报但未认定的判断 (需求 R363, unrecognised_suggestions; 档位另取执行重要性), 不计入
        合计与候选意见.
        例: 一条 14200000 高估的未更正错报, 重要性 12000000, 明显微小 600000 → items 一行, total 14200000, qualified.
        """
        params = ctx.run.params_snapshot
        materiality, trivial = params["materiality_cents"], params["clearly_trivial_cents"]
        performance = params.get("performance_materiality_cents")  # 需求 R363 之前的参数快照没有
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
        # A9 只走金额路径 (FIN-04 早于 gate_fin 的 FIN-02 步, 拿不到已批准的结论), 综合候选由 FIN-01 闸门材料调本函数
        # 给出 (GC-09, 需求 R187 验收 4), 签字前提的参数由 SG-16 加
        options = allowed_opinions(total_cents=total, materiality_cents=materiality, going_concern=None)
        return UncorrectedSchedule(
            id="SCHEDULE",
            version=previous.version + 1 if previous else 1,
            items=items,
            excluded_trivial_ids=excluded,
            corrected_ids=corrected,
            total_cents=total,
            materiality_cents=materiality,
            candidate_opinion=options.candidate,
            unrecognised=unrecognised_suggestions(ctx.state, total, materiality, performance, trivial),
        )

    async def interpret(self, ctx: RunContext, findings: UncorrectedSchedule) -> StatePatch:
        return {"uncorrected_schedule": findings, "procedure_runs": [finish_run(ctx.run, datetime.now(UTC))]}
