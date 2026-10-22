// 初步分析三步结果 (推荐重要性与复筛) 的前端测试 (后端需求 R371, 页面 R372), bun test 运行: 推荐卡片与空值不渲染,
// 三组与观察组的内容、排序与空组, 退回规则的提示, 中英文案与项目币种. 数据是合成的, 取自 Xero 演示数据集 PLN-07 的
// 固定输出 (recommendation 用 revenue 1%, 观察组为 711 累计折旧 825.00 大于 710 设备原值 750.00).
import { describe, expect, test } from 'bun:test'
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type {
  AnalyticsRecommendation,
  AnalyticsResult,
  AnalyticsScreening,
  AnalyticsScreeningItem,
  EvidencePointer,
  GateView,
} from '../src/client/types.gen'
import { GateMaterial, PlanningAnalytics } from '../src/components/gate-form'
import { I18nProvider } from '../src/lib/i18n'
import { MoneyProvider } from '../src/lib/use-money'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const EID = 'ENG-TEST'

const POINTER: EvidencePointer = {
  type: 'voucher',
  source_type: 'trial_balance',
  source_id: '200',
  locator: 'balance_cents',
  page: null,
  reliability: 'internal_original',
}

const RECOMMENDATION: AnalyticsRecommendation = {
  basis: 'revenue',
  base_amount_cents: 324310000,
  percentage: 0.01,
  threshold_cents: 3243100,
  rationale: 'The company is loss-making, so revenue is the most relevant benchmark under ASA 320 A5.',
  evidence: [POINTER],
  counter_argument: 'A more conservative 0.5% would flag the same accounts without adding discrimination.',
  self_confidence: 0.86,
}

// 故意不按代码排, 页面按科目代码排
const FOCUS_ITEMS: AnalyticsScreeningItem[] = [
  {
    account_code: '710',
    account_name: 'Office Equipment',
    amount_cents: 75000,
    reason: 'Office equipment was recorded with no opening balance and should be vouched.',
    risk_or_assertion: 'Existence and valuation of property, plant and equipment',
    evidence: [POINTER],
  },
  {
    account_code: '090',
    account_name: 'Business Bank Account',
    amount_cents: 219724,
    reason: 'The bank account fell after opening balances were converted from the prior system.',
    risk_or_assertion: 'Existence and cut-off of cash at bank',
    evidence: [POINTER],
  },
  {
    account_code: '711',
    account_name: 'Less Accumulated Depreciation on Office Equipment',
    amount_cents: 82500,
    reason: 'Accumulated depreciation exceeds the cost recorded in 710.',
    risk_or_assertion: 'Valuation of property, plant and equipment',
    evidence: [],
  },
]

const SCREENING: AnalyticsScreening = {
  threshold_cents: 3243100,
  hit_account_codes: ['090', '610', '710', '711', '820'],
  focus: FOCUS_ITEMS,
  additions: [
    {
      account_code: '960',
      account_name: 'Retained Earnings',
      reason: 'An opening conversion balance with no prior-period comparative.',
      risk_or_assertion: 'Completeness and existence of opening retained earnings',
    },
  ],
  excluded: [
    {
      account_code: '820',
      account_name: 'GST',
      amount_cents: 40077,
      reason: 'The movement is explained by normal GST activity.',
      risk_or_assertion: 'Completeness of GST liabilities (monitored at year end)',
      evidence: [POINTER],
    },
  ],
  observations: [
    {
      account_codes: ['710', '711'],
      reason:
        'Accumulated depreciation (711) is AUD 825.00 while the related office equipment cost (710) is only AUD 750.00.',
      risk_or_assertion: 'Valuation of property, plant and equipment',
      evidence: [],
    },
  ],
}

function analytics(patch: Partial<AnalyticsResult> = {}): AnalyticsResult {
  return {
    id: 'AR-PLN-07-1',
    version: 1,
    ratios: {},
    exception_ids: [],
    monthly: [],
    recommendation: RECOMMENDATION,
    screening: SCREENING,
    fallback: false,
    fallback_reason: null,
    ...patch,
  }
}

function render(children: ReactNode, currency = 'AUD', language = 'en-AU'): string {
  const originalLanguage = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: language, configurable: true })
  try {
    return renderToStaticMarkup(
      <I18nProvider>
        <MoneyProvider currency={currency}>{children}</MoneyProvider>
      </I18nProvider>,
    )
  } finally {
    if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
    else Reflect.deleteProperty(navigator, 'language')
  }
}

describe('推荐重要性卡片', () => {
  test('基准, 基准金额, 比例, 门槛, 置信度与理由; 金额按币种', () => {
    const html = render(<PlanningAnalytics eid={EID} analytics={analytics()} />)
    for (const text of [
      'Recommended materiality (AI)',
      'Revenue',
      'AUD 3,243,100.00',
      '1.00%',
      'AUD 32,431.00',
      '86.00%',
      en.planning.analysis.recommendation.rationale,
      RECOMMENDATION.rationale,
      en.planning.analysis.recommendation.counterArgument,
      RECOMMENDATION.counter_argument,
      en.planning.analysis.recommendation.hint,
    ]) {
      expect(html).toContain(text)
    }
    expect(html).toContain('data-testid="analytics-recommendation"')
    // 依据的证据指针也列出
    expect(html).toContain('200 · balance')
  })

  test('recommendation 为空时不显示推荐卡片; 没有复筛与退回时为整块空', () => {
    const noRecommendation = render(<PlanningAnalytics eid={EID} analytics={analytics({ recommendation: null })} />)
    expect(noRecommendation).not.toContain('data-testid="analytics-recommendation"')
    expect(noRecommendation).toContain('data-testid="analytics-screening"')

    const empty = render(<PlanningAnalytics eid={EID} analytics={analytics({ recommendation: null, screening: {} })} />)
    expect(empty).toBe('')
  })

  test('新增的基准取值有中英文案', () => {
    const grossProfit = { ...RECOMMENDATION, basis: 'gross_profit' as const }
    expect(render(<PlanningAnalytics eid={EID} analytics={analytics({ recommendation: grossProfit })} />)).toContain(
      'Gross profit',
    )
    expect(
      render(<PlanningAnalytics eid={EID} analytics={analytics({ recommendation: grossProfit })} />, 'AUD', 'zh-CN'),
    ).toContain('毛利')
    expect(zh.bases.total_expenses).toBe('总费用')
    expect(zh.bases.net_assets).toBe('净资产')
    expect(en.bases.total_expenses).toBe('Total expenses')
  })
})

describe('三步复筛', () => {
  test('门槛与命中科目数, 重点关注与已排除按科目代码排, 显示金额/理由/风险或认定', () => {
    const html = render(<PlanningAnalytics eid={EID} analytics={analytics()} />)
    expect(html).toContain('Screening result')
    expect(html).toContain(`Threshold AUD 32,431.00`)
    expect(html).toContain('5 accounts over the threshold')
    expect(html).toContain('data-testid="analytics-focus"')
    expect(html).toContain('data-testid="analytics-excluded"')
    // 给出的顺序是 710, 090, 711, 页面按代码排成 090, 710, 711
    const focus = html.slice(
      html.indexOf('data-testid="analytics-focus"'),
      html.indexOf('data-testid="analytics-additions"'),
    )
    expect(focus.indexOf('090')).toBeLessThan(focus.indexOf('710'))
    expect(focus.indexOf('710')).toBeLessThan(focus.indexOf('711'))
    expect(focus).toContain('Business Bank Account')
    expect(focus).toContain('AUD 2,197.24')
    expect(focus).toContain('The bank account fell after opening balances were converted')
    expect(focus).toContain('Risk or assertion: Existence and cut-off of cash at bank')
  })

  test('大模型补充的科目与对已命中科目的补充观察各自成组; 观察组显示相关科目与名称', () => {
    const html = render(<PlanningAnalytics eid={EID} analytics={analytics()} />)
    expect(html).toContain('data-testid="analytics-additions"')
    expect(html).toContain('Added by the model (not flagged by the rules)')
    expect(html).toContain('960')
    expect(html).toContain('Retained Earnings')
    expect(html).toContain('data-testid="analytics-observations"')
    expect(html).toContain('Supplementary observations')
    expect(html).toContain('Accumulated depreciation (711) is AUD 825.00')
    // 观察组只有科目代码, 名称从重点/排除两组补全
    expect(html).toContain('Less Accumulated Depreciation on Office Equipment')
    expect(html).toContain('Valuation of property, plant and equipment')
  })

  test('空组不渲染 (统一约定: 不显示"无"): 只有重点时其余三组都没有标题', () => {
    const onlyFocus: AnalyticsScreening = {
      threshold_cents: 1000,
      hit_account_codes: ['090'],
      focus: [FOCUS_ITEMS[0]],
    }
    const html = render(<PlanningAnalytics eid={EID} analytics={analytics({ screening: onlyFocus })} />)
    expect(html).toContain('data-testid="analytics-focus"')
    expect(html).not.toContain('data-testid="analytics-additions"')
    expect(html).not.toContain('data-testid="analytics-observations"')
    expect(html).not.toContain('data-testid="analytics-excluded"')
  })

  test('中文界面: 小标题与结论文案', () => {
    const html = render(<PlanningAnalytics eid={EID} analytics={analytics()} />, 'AUD', 'zh-CN')
    for (const text of [
      '推荐重要性（AI）',
      '复筛结果',
      '重点关注',
      '大模型补充',
      '补充观察',
      '已排除',
      '命中 5 个科目',
    ]) {
      expect(html).toContain(text)
    }
    expect(html).toContain('门槛 AUD 32,431.00')
  })
})

describe('退回规则的提示', () => {
  test('fallback 为真: 写明退回本期收入 × 0.5% 的规则结果与原因, 不显示推荐卡片与复筛', () => {
    const html = render(
      <PlanningAnalytics
        eid={EID}
        analytics={analytics({
          recommendation: null,
          screening: {},
          fallback: true,
          fallback_reason: 'PLN-07 basis: the model output did not pass validation after retries',
        })}
      />,
    )
    for (const text of [
      en.planning.analysis.fallback.title,
      'Reason: PLN-07 basis: the model output did not pass validation after retries',
    ]) {
      expect(html).toContain(text)
    }
    expect(html).toContain('data-testid="analytics-fallback"')
    expect(html).not.toContain('data-testid="analytics-recommendation"')
    expect(html).not.toContain('data-testid="analytics-screening"')
  })

  test('fallback 没有原因时只显示退回提示', () => {
    const html = render(
      <PlanningAnalytics eid={EID} analytics={analytics({ recommendation: null, screening: {}, fallback: true })} />,
    )
    expect(html).toContain(en.planning.analysis.fallback.title)
    expect(html).not.toContain('Reason:')
  })

  test('中文的退回提示', () => {
    const html = render(
      <PlanningAnalytics
        eid={EID}
        analytics={analytics({
          recommendation: null,
          screening: {},
          fallback: true,
          fallback_reason: '大模型输出不合格',
        })}
      />,
      'AUD',
      'zh-CN',
    )
    expect(html).toContain(zh.planning.analysis.fallback.title)
    expect(html).toContain('原因：大模型输出不合格')
  })
})

describe('项目币种', () => {
  test('推荐金额, 门槛与复筛金额都按项目记账币种', () => {
    const html = render(<PlanningAnalytics eid={EID} analytics={analytics()} />, 'NZD')
    expect(html).toContain('NZD 3,243,100.00')
    expect(html).toContain('NZD 32,431.00')
    expect(html).toContain('NZD 2,197.24')
    // 金额都用项目币种; 模型写的理由原文里出现的 AUD 不换算, 只核对格式化出来的金额
    expect(html).not.toContain('AUD 3,243,100.00')
    expect(html).not.toContain('AUD 32,431.00')
  })
})

describe('闸门材料', () => {
  function gateView(patch: Partial<AnalyticsResult> = {}): GateView {
    return {
      gate: 'PLN-01/03',
      target_id: 'PLN-01/03',
      target_version: 1,
      material: {
        kind: 'PLN-01/03',
        analytics: analytics(patch),
        analytics_exceptions: [],
        candidates: { id: 'MC-1', candidates: [] },
        objectives: [],
        risks: [],
        uncovered_objective_ids: [],
      },
    } as unknown as GateView
  }

  test('PLN-01/03 闸门材料在现有初步分析面板之后显示推荐与复筛', () => {
    const html = render(<GateMaterial eid={EID} view={gateView()} />)
    expect(html).toContain('Preliminary analysis')
    expect(html).toContain('data-testid="planning-analytics"')
    expect(html).toContain('data-testid="analytics-recommendation"')
    expect(html).toContain('data-testid="analytics-focus"')
    expect(html).toContain('data-testid="analytics-observations"')
  })

  test('闸门材料退回规则时显示提示, 旧数据 (没有新字段) 时整块不显示', () => {
    const fallback = render(
      <GateMaterial
        eid={EID}
        view={gateView({ recommendation: null, screening: {}, fallback: true, fallback_reason: 'model unavailable' })}
      />,
    )
    expect(fallback).toContain('data-testid="analytics-fallback"')
    const legacy = render(<GateMaterial eid={EID} view={gateView({ recommendation: null, screening: {} })} />)
    expect(legacy).not.toContain('data-testid="planning-analytics"')
  })
})
