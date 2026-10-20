"""意见类型的候选与可选范围 (需求 R184): A9 的候选 (FIN-04 汇总表的 candidate_opinion), FIN-01 闸门决定的
disposition 校验 (gates.fin01_check) 与 FIN-01 闸门材料 (汇总表带的候选) 都取 allowed_opinions, 不另写一份.

规格书第 10 节 P3 (五条独立路径, 金额与持续经营取较严一方), 12.5 UC-06 步骤 6. 候选可由审计师推翻 (ASA 450 第 11 段,
需求 R24 验收 3), 可选范围之外的意见类型 FIN-01 报 INVALID_VALUE. 否定意见与无法表示意见本版都不可选 (没有这两种报告
模板); 签字前提的检查结果 (CHECK-FIN-18) 与无法表示意见的分支由签字前提段 (SG-16) 加第三个参数. 纯函数, 不读 IO.
"""

from dataclasses import dataclass

from backend.contracts.models import GoingConcernOutcome, OpinionType, UncorrectedSchedule

# 金额路径可选的意见类型 (候选可推翻)
AMOUNT_OPTIONS: tuple[OpinionType, ...] = (OpinionType.unqualified, OpinionType.qualified)


@dataclass(frozen=True)
class OpinionOptions:
    """候选意见, 可选的意见类型 (按 OpinionType 的顺序) 与理由 (每条路径一句, 先金额后持续经营)."""

    candidate: OpinionType
    allowed: tuple[OpinionType, ...]
    reasons: list[str]


def allowed_opinions(
    schedule: UncorrectedSchedule,
    going_concern: GoingConcernOutcome | None,
    # 第三个参数 (CHECK-FIN-18 签字前提检查的结果, 含无法表示意见的分支) 由签字前提段 SG-16 加, 本条不加
) -> OpinionOptions:
    """按未更正错报汇总表与持续经营结论 (FIN-02; 没有结论时为 None) 给出候选与可选的意见类型.

    - 金额路径: 合计 total_cents 不低于整体重要性 materiality_cents 时候选 qualified, 否则 unqualified (需求 R23);
      可选 (unqualified, qualified), 候选可由审计师推翻 (需求 R24).
    - 持续经营 (ASA 570 第 21-23 段): None 与 no_material_uncertainty 不改金额路径; material_uncertainty_disclosed
      不改意见类型 (无保留加持续经营重大不确定性专段, 第 22 段); material_uncertainty_not_disclosed 至少保留, 只可选
      qualified (第 23 段的否定意见本版不支持); basis_inappropriate 候选 adverse (第 21 段), 没有可选的意见类型 (本版
      不支持; FIN-02 闸门已拒绝批准它, 正常走不到).
    例: Xero (合计 4853, 重要性 32431) 不论持续经营结论为空或 material_uncertainty_disclosed 都为候选 unqualified,
    可选 (unqualified, qualified)."""
    material = schedule.total_cents >= schedule.materiality_cents
    candidate = OpinionType.qualified if material else OpinionType.unqualified
    compared = "不低于" if material else "低于"
    reasons = [
        f"未更正错报合计 {schedule.total_cents} 分{compared}整体重要性 {schedule.materiality_cents} 分: "
        f"候选 {candidate.value}, 审计师可推翻 (ASA 450 para 11)"
    ]
    allowed = AMOUNT_OPTIONS
    if going_concern is GoingConcernOutcome.material_uncertainty_disclosed:
        reasons.append("持续经营存在重大不确定性且披露充分: 意见类型不变, 报告加重大不确定性专段 (ASA 570 para 22)")
    elif going_concern is GoingConcernOutcome.material_uncertainty_not_disclosed:
        candidate, allowed = OpinionType.qualified, (OpinionType.qualified,)
        reasons.append("持续经营存在重大不确定性而披露不充分: 至少保留意见, 否定意见本版不支持 (ASA 570 para 23)")
    elif going_concern is GoingConcernOutcome.basis_inappropriate:
        candidate, allowed = OpinionType.adverse, ()
        reasons.append("本版不支持持续经营假设不恰当 (否定意见): ASA 570 para 21 要求否定意见")
    return OpinionOptions(candidate=candidate, allowed=allowed, reasons=reasons)
