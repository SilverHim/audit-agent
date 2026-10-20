// PLN-01/03 闸门的重要性候选 (lib/materiality, 需求 R351), bun test 运行.
// 候选取 Xero 演示的三基准与金额 (税前亏损, 税前利润一项本期不适用); 顺序有意调过, 把不适用的一项排在最前,
// 看初值会不会取它 (材料里的实际顺序是收入, 总资产, 税前利润)
import { describe, expect, test } from 'bun:test'
import type { MaterialityCandidate, MaterialityCandidates } from '../src/client/types.gen'
import { initialChoice, selectableCandidates } from '../src/lib/materiality'

function candidate(
  basis: MaterialityCandidate['basis'],
  base: number,
  pct: [number, number],
  notApplicable: MaterialityCandidate['not_applicable'] = null,
): MaterialityCandidate {
  const [pct_low, pct_high] = pct
  return {
    basis,
    base_amount_cents: base,
    pct_low,
    pct_high,
    low_cents: Math.round(base * pct_low),
    high_cents: Math.round(base * pct_high),
    not_applicable: notApplicable,
  }
}

function materials(candidates: MaterialityCandidate[]): MaterialityCandidates {
  return { id: 'MATERIALITY', version: 1, candidates, performance_pct_range: [0.5, 0.75], clearly_trivial_pct: 0.05 }
}

const XERO = materials([
  candidate('profit_before_tax', -1322696, [0.05, 0.05], 'loss'),
  candidate('revenue', 3243100, [0.005, 0.01]),
  candidate('total_assets', 1080529, [0.01, 0.02]),
])

describe('重要性候选', () => {
  test('本期不适用的候选不进基准的选项, 其余按材料顺序', () => {
    expect(selectableCandidates(XERO).map((item) => item.basis)).toEqual(['revenue', 'total_assets'])
  })

  test('初值取第一个可选的候选, 比例取其上限, 不取排在前面的不适用候选', () => {
    expect(initialChoice(XERO)).toEqual({
      basis: 'revenue',
      pct: 0.01,
      performance_pct: 0.75,
      clearly_trivial_pct: 0.05,
    })
  })

  test('没有标记的旧材料 (R351 以前存下的): 为负的税前利润同样不进选项, 初值也不取它', () => {
    const old = materials([
      candidate('profit_before_tax', -150000000, [0.05, 0.05]),
      candidate('revenue', 1200000000, [0.005, 0.01]),
    ])
    expect(selectableCandidates(old).map((item) => item.basis)).toEqual(['revenue'])
    expect(initialChoice(old).basis).toBe('revenue')
  })

  test('都不适用时退回第一项 (提交时后端拒绝)', () => {
    const none = materials([candidate('revenue', 0, [0.005, 0.01], 'not_positive')])
    expect(selectableCandidates(none)).toEqual([])
    expect(initialChoice(none).basis).toBe('revenue')
  })
})
