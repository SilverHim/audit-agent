"""意见类型的候选与可选范围 (需求 R184): A9 的候选 (FIN-04 汇总表的 candidate_opinion), FIN-01 闸门决定的
disposition 校验 (gates.fin01_check) 与 FIN-01 闸门材料的综合候选都取 allowed_opinions, 不另写一份.

A9 只走金额路径 (FIN-04 早于 gate_fin 的 FIN-02 步, 拿不到已批准的结论), 一直传 None; FIN-01 闸门材料调本函数给出
金额与持续经营的综合候选由 GC-09 (需求 R187) 做, FIN-01 校验接上已批准的 FIN-02 结论由 GC-10 (需求 R188) 做.
规格书第 10 节 P3 (五条独立路径, 金额与持续经营取较严一方), 12.5 UC-06 步骤 6. 候选可由审计师推翻 (ASA 450 第 11 段,
需求 R24 验收 3), 可选范围之外的意见类型 FIN-01 报 INVALID_VALUE. 否定意见与无法表示意见本版都不可选 (没有这两种报告
模板); 签字前提的检查结果 (CHECK-FIN-18) 与无法表示意见的分支由签字前提段 (SG-16) 另加关键字参数 check_fin18.
纯函数, 不读 IO.
"""

from dataclasses import dataclass
from typing import assert_never

from backend.contracts.models import GoingConcernOutcome, OpinionType

# 金额路径可选的意见类型 (候选可推翻)
AMOUNT_OPTIONS: tuple[OpinionType, ...] = (OpinionType.unqualified, OpinionType.qualified)


@dataclass(frozen=True)
class OpinionOptions:
    """候选意见, 可选的意见类型 (按 OpinionType 的顺序) 与理由 (每条路径一句, 先金额后持续经营).

    不变式: allowed 非空时 candidate 在 allowed 中 (候选总能选); allowed 为空 (本版不支持的情形) 时 candidate 是准则
    要求而本版给不出的意见类型."""

    candidate: OpinionType
    allowed: tuple[OpinionType, ...]
    reasons: list[str]


def allowed_opinions(
    *,
    total_cents: int,
    materiality_cents: int,
    going_concern: GoingConcernOutcome | None,
    # 签字前提检查 CHECK-FIN-18 的结果 (含无法表示意见的分支) 由签字前提段 SG-16 另加关键字参数 check_fin18, 本条不加
) -> OpinionOptions:
    """按未更正错报合计 total_cents, 整体重要性 materiality_cents 与持续经营结论 (FIN-02; 没有结论时为 None) 给出候选
    与可选的意见类型.

    - 金额路径: 合计不低于整体重要性时候选 qualified, 否则 unqualified (需求 R23); 可选 (unqualified, qualified), 候选
      可由审计师推翻 (需求 R24). 理由写成金额路径的结论.
    - 持续经营 (ASA 570 第 21-23 段): None 与 no_material_uncertainty 不改金额路径; material_uncertainty_disclosed
      不改意见类型 (无保留加持续经营重大不确定性专段, 第 22 段); material_uncertainty_not_disclosed 至少保留, 只可选
      qualified (第 23 段的否定意见本版不支持); basis_inappropriate 候选 adverse (第 21 段), 没有可选的意见类型 (本版
      不支持; GC-10 起 FIN-02 闸门拒绝批准它, 正常走不到). 持续经营较严时另写一句较严的结果, 与金额路径那句不矛盾.
    例: Xero (合计 4853, 重要性 32431) 不论持续经营结论为空或 material_uncertainty_disclosed 都为候选 unqualified,
    可选 (unqualified, qualified)."""
    material = total_cents >= materiality_cents
    amount = OpinionType.qualified if material else OpinionType.unqualified
    compared = "不低于" if material else "低于"
    reasons = [
        f"金额路径: 未更正错报合计 {total_cents} 分{compared}整体重要性 {materiality_cents} 分, 金额路径候选 "
        f"{amount.value} (可由审计师推翻, ASA 450 para 11)"
    ]
    candidate, allowed = amount, AMOUNT_OPTIONS
    match going_concern:
        case None | GoingConcernOutcome.no_material_uncertainty:
            pass
        case GoingConcernOutcome.material_uncertainty_disclosed:
            reasons.append("持续经营存在重大不确定性且披露充分: 意见类型不变, 报告加重大不确定性专段 (ASA 570 para 22)")
        case GoingConcernOutcome.material_uncertainty_not_disclosed:
            candidate, allowed = OpinionType.qualified, (OpinionType.qualified,)
            changed = "仍为" if amount is OpinionType.qualified else "改为"
            reasons.append(
                "持续经营存在重大不确定性而披露不充分: 至少保留意见, 否定意见本版不支持 (ASA 570 para 23); "
                f"较严, 候选{changed} qualified, 不可选 unqualified"
            )
        case GoingConcernOutcome.basis_inappropriate:
            candidate, allowed = OpinionType.adverse, ()
            reasons.append(
                "本版不支持持续经营假设不恰当 (否定意见): ASA 570 para 21 要求否定意见; 较严, 候选改为 adverse, "
                "没有可选的意见类型"
            )
        case _:
            assert_never(going_concern)
    return OpinionOptions(candidate=candidate, allowed=allowed, reasons=reasons)
