import type {
  AnalyticsRecommendation,
  MaterialityCandidate,
  MaterialityCandidates,
  MaterialityChoice,
} from '@/client/types.gen'

// PLN-01/03 闸门的重要性候选 (需求 R351): 本期不适用的候选 (not_applicable 有值, 基准金额不为正) 照常列出并写明
// 原因, 不进基准的选项, 表单初值也不取它; 后端仍按金额拒绝以它为基准的决定 (INVALID_VALUE)

// 候选本期不适用的原因, 适用时为 null: 取材料的标记; 没有标记而基准金额不为正的 (R351 以前存下的材料) 按
// not_positive, 同闸门口径. 候选列表显示原因还是区间, 基准下拉选不选, 都按它
export function inapplicable(item: MaterialityCandidate): NonNullable<MaterialityCandidate['not_applicable']> | null {
  return item.not_applicable ?? (item.base_amount_cents > 0 ? null : 'not_positive')
}

// 可选为基准的候选: 本期适用的, 顺序同材料
export function selectableCandidates(candidates: MaterialityCandidates): MaterialityCandidate[] {
  return candidates.candidates.filter((item) => !inapplicable(item))
}

// 表单初值: 第一个可选的候选, 比例取其上限, 执行重要性取范围上限; 没有可选的候选时退回第一项 (提交时被拒)
export function initialChoice(
  candidates: MaterialityCandidates,
  recommendation?: AnalyticsRecommendation | null,
): MaterialityChoice {
  const recommended =
    recommendation &&
    selectableCandidates(candidates).find(
      (c) =>
        c.basis === recommendation.basis &&
        recommendation.percentage >= c.pct_low &&
        recommendation.percentage <= c.pct_high,
    )
  const usable = recommended || selectableCandidates(candidates)[0] || candidates.candidates[0]
  const range = candidates.performance_pct_range ?? [0.5, 0.75]
  return {
    basis: usable?.basis ?? 'revenue',
    pct: recommended && recommendation ? recommendation.percentage : (usable?.pct_high ?? 0.01),
    performance_pct: range[1],
    clearly_trivial_pct: candidates.clearly_trivial_pct ?? 0.05,
  }
}
