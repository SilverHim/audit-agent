// 判断的定论与 AI 建议的更正分录 (需求 R368) 的前端测试, bun test 运行: 定论回退与颜色, 金额与方向, 更正分录的
// 渲染, verdict 为空时的处理, 错报列表里的更正分录, 金额按项目币种. 数据是合成的; XJ-499 的两行更正分录取自
// Xero 演示数据集的 GEN-02 固定输出.
import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { engagementsGetEngagementQueryKey } from '../src/client/@tanstack/react-query.gen'
import type {
  CorrectionEntry,
  EngagementOverview,
  Judgment,
  Misstatement,
  MisstatementUpdate,
} from '../src/client/types.gen'
import { JudgmentCard } from '../src/components/concierge'
import { MisstatementList } from '../src/components/misstatement-lifecycle'
import { CorrectionList, ProposalVerdict, VerdictBanner, VerdictCell } from '../src/components/verdict'
import { I18nProvider } from '../src/lib/i18n'
import type { RankedTask } from '../src/lib/review'
import { MoneyProvider } from '../src/lib/use-money'
import {
  actionVerdict,
  correctionCents,
  correctionEntries,
  correctionKey,
  correctionSide,
  displayVerdict,
  effectiveVerdict,
  verdictFigure,
} from '../src/lib/verdict'

const CORRECTION: CorrectionEntry[] = [
  {
    account_code: '711',
    account_name: 'Less Accumulated Depreciation on Office Equipment',
    debit_cents: 82500,
    credit_cents: 0,
    description: 'Reverse the depreciation credited to accumulated depreciation.',
  },
  {
    account_code: '710',
    account_name: 'Office Equipment',
    debit_cents: 0,
    credit_cents: 75000,
    description: 'Reverse the office equipment debited by manual journal XJ-499.',
  },
]

const UPDATE = { kind: 'classification', type: 'factual', fraud_indicative: false } as MisstatementUpdate

function judgment(fields: Partial<Judgment> = {}): Judgment {
  return {
    id: 'J-GEN-02-1-XJ-499',
    procedure_id: 'GEN-02',
    run_id: 'RUN-GEN-02-1',
    subject_id: 'XJ-499',
    conclusion: 'XJ-499 has no basis; Office Equipment is overstated by AUD 750.00.',
    confidence: 0.85,
    confidence_parts: {},
    reasoning_chain: [],
    evidence: [],
    counter_argument: '',
    proposed_action: 'misstatement',
    autonomy_level: 'L2',
    performed_by: 'U-1',
    created_at: '2026-10-21T00:00:00Z',
    ...fields,
  } as Judgment
}

const DRAFT = {
  type: 'factual',
  account: 'Office equipment (710)',
  assertion: 'Existence',
  amount_cents: 75000,
  direction: 'overstated',
  account_code: '710',
  correction: CORRECTION,
} as const

function misstatement(fields: Partial<Misstatement> = {}): Misstatement {
  return {
    id: 'M-D-XJ499',
    judgment_id: 'J-GEN-02-1-XJ-499',
    exception_id: null,
    type: 'factual',
    account: 'Office equipment (710)',
    assertion: 'Existence',
    amount_cents: 75000,
    direction: 'overstated',
    account_code: '710',
    status: 'identified',
    ...fields,
  } as Misstatement
}

function render(children: ReactNode, currency = 'AUD'): string {
  return renderToStaticMarkup(
    <I18nProvider>
      <MoneyProvider currency={currency}>{children}</MoneyProvider>
    </I18nProvider>,
  )
}

let originalLanguage: PropertyDescriptor | undefined
beforeEach(() => {
  originalLanguage = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: 'en-AU', configurable: true })
})
afterEach(() => {
  if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
  else Reflect.deleteProperty(navigator, 'language')
})

describe('定论的回退', () => {
  test('有 verdict 时以它为准', () => {
    expect(effectiveVerdict({ verdict: 'misstatement', proposed_action: 'accept' })).toBe('misstatement')
    expect(effectiveVerdict({ verdict: 'not_misstatement', proposed_action: 'misstatement' })).toBe('not_misstatement')
    expect(effectiveVerdict({ verdict: 'undetermined', proposed_action: 'close' })).toBe('undetermined')
  })

  test('verdict 为空 (旧数据与人工判断) 时按 proposed_action 回退', () => {
    expect(effectiveVerdict({ verdict: null, proposed_action: 'accept' })).toBe('not_misstatement')
    expect(effectiveVerdict({ verdict: null, proposed_action: 'close' })).toBe('not_misstatement')
    expect(effectiveVerdict({ verdict: null, proposed_action: 'misstatement' })).toBe('misstatement')
    expect(effectiveVerdict({ verdict: null, proposed_action: 'more_evidence' })).toBe('undetermined')
  })

  test('FIN-03/FIN-09 的错报更新判断没有 verdict 时不显示定论行; 其余旧数据照回退显示', () => {
    expect(displayVerdict({ verdict: null, proposed_action: 'accept', misstatement_update: UPDATE })).toBeNull()
    expect(displayVerdict({ verdict: null, proposed_action: 'accept' })).toBe('not_misstatement')
    expect(displayVerdict({ verdict: null, proposed_action: 'more_evidence', misstatement_update: null })).toBe(
      'undetermined',
    )
    expect(displayVerdict({ verdict: 'misstatement', proposed_action: 'accept', misstatement_update: UPDATE })).toBe(
      'misstatement',
    )
  })

  test('只有建议动作的争议两方也按定论的说法给', () => {
    expect(actionVerdict('accept')).toBe('not_misstatement')
    expect(actionVerdict('close')).toBe('not_misstatement')
    expect(actionVerdict('misstatement')).toBe('misstatement')
    expect(actionVerdict('more_evidence')).toBe('undetermined')
    expect(actionVerdict(null)).toBeNull()
  })

  test('是错报时的金额与方向取 AI 的错报草稿; 不是错报或没有草稿时为空', () => {
    const draft = { ...DRAFT }
    expect(
      verdictFigure({ verdict: 'misstatement', proposed_action: 'misstatement', misstatement_draft: draft }),
    ).toEqual({ amount_cents: 75000, direction: 'overstated' })
    expect(
      verdictFigure({ verdict: 'not_misstatement', proposed_action: 'accept', misstatement_draft: draft }),
    ).toBeNull()
    expect(
      verdictFigure({ verdict: 'misstatement', proposed_action: 'misstatement', misstatement_draft: null }),
    ).toBeNull()
  })
})

describe('更正分录的读取', () => {
  test('判断草稿与错报的 correction; 没带或为 null 时为空', () => {
    expect(correctionEntries({ correction: CORRECTION })).toEqual(CORRECTION)
    expect(correctionEntries({ correction: null })).toEqual([])
    expect(correctionEntries({})).toEqual([])
    expect(correctionEntries(null)).toEqual([])
  })

  test('一行只记一方: 借方非零为借方, 否则为贷方, 金额取该方', () => {
    expect(correctionSide(CORRECTION[0])).toBe('debit')
    expect(correctionCents(CORRECTION[0])).toBe(82500)
    expect(correctionSide(CORRECTION[1])).toBe('credit')
    expect(correctionCents(CORRECTION[1])).toBe(75000)
    expect(correctionKey(CORRECTION[0])).not.toBe(correctionKey(CORRECTION[1]))
  })
})

describe('定论行', () => {
  test('是错报: 红色大字, 同一行给金额与方向 (需求 R368 第 1, 2 条)', () => {
    const html = render(<VerdictBanner judgment={judgment({ verdict: 'misstatement', misstatement_draft: DRAFT })} />)
    expect(html).toContain('data-verdict="misstatement"')
    expect(html).toContain('text-risk')
    expect(html).toContain('>Misstatement<')
    expect(html).toContain('AUD 750.00')
    expect(html).toContain('Overstated')
    expect(html).toContain('>Verdict<')
  })

  test('不是错报绿色, 暂时判断不了黄色; 两者都不显示金额', () => {
    const clear = render(
      <VerdictBanner judgment={judgment({ verdict: 'not_misstatement', proposed_action: 'accept' })} />,
    )
    expect(clear).toContain('data-verdict="not_misstatement"')
    expect(clear).toContain('text-ok')
    expect(clear).toContain('Not a misstatement')
    expect(clear).not.toContain('AUD')

    const undetermined = render(
      <VerdictBanner judgment={judgment({ verdict: 'undetermined', proposed_action: 'more_evidence' })} />,
    )
    expect(undetermined).toContain('data-verdict="undetermined"')
    expect(undetermined).toContain('text-warn')
    expect(undetermined).toContain('Cannot tell yet')
    expect(undetermined).not.toContain('AUD')
  })

  test('verdict 为空时按 proposed_action 回退显示, 不崩', () => {
    const html = render(
      <VerdictBanner
        judgment={judgment({ verdict: null, proposed_action: 'misstatement', misstatement_draft: DRAFT })}
      />,
    )
    expect(html).toContain('data-verdict="misstatement"')
    expect(html).toContain('AUD 750.00')
    expect(html).toContain('Overstated')
  })

  test('FIN-03/FIN-09 的错报更新判断没有 verdict 时不显示定论行', () => {
    const html = render(
      <VerdictBanner judgment={judgment({ verdict: null, proposed_action: 'accept', misstatement_update: UPDATE })} />,
    )
    expect(html).toBe('')
  })

  test('金额按项目记账币种格式化 (需求 R366)', () => {
    const html = render(
      <VerdictBanner judgment={judgment({ verdict: 'misstatement', misstatement_draft: DRAFT })} />,
      'NZD',
    )
    expect(html).toContain('NZD 750.00')
    expect(html).not.toContain('AUD')
  })

  test('中文界面用中文的定论, 方向与更正分录 (需求 R368 第 6 条)', () => {
    const original = Object.getOwnPropertyDescriptor(navigator, 'language')
    Object.defineProperty(navigator, 'language', { value: 'zh-CN', configurable: true })
    try {
      const banner = render(
        <VerdictBanner judgment={judgment({ verdict: 'misstatement', misstatement_draft: DRAFT })} />,
      )
      for (const text of ['定论', '是错报', '高估', 'AUD 750.00']) expect(banner).toContain(text)
      expect(render(<CorrectionList entries={CORRECTION} />)).toContain('AI 建议的更正分录')
      const clear = render(
        <VerdictBanner judgment={judgment({ verdict: 'not_misstatement', proposed_action: 'accept' })} />,
      )
      expect(clear).toContain('不是错报')
      const undetermined = render(
        <VerdictBanner judgment={judgment({ verdict: 'undetermined', proposed_action: 'more_evidence' })} />,
      )
      expect(undetermined).toContain('暂时判断不了')
      expect(render(<CorrectionList entries={CORRECTION} />)).toContain('借方')
    } finally {
      if (original) Object.defineProperty(navigator, 'language', original)
      else Reflect.deleteProperty(navigator, 'language')
    }
  })
})

describe('列表里的定论一格', () => {
  test('不是判断时为破折号; 是错报的同一格给金额与方向', () => {
    expect(render(<VerdictCell judgment={null} />)).toBe('<span>—</span>')
    const html = render(<VerdictCell judgment={judgment({ verdict: 'misstatement', misstatement_draft: DRAFT })} />)
    expect(html).toContain('data-verdict="misstatement"')
    expect(html).toContain('Misstatement')
    expect(html).toContain('AUD 750.00')
    expect(html).toContain('Overstated')
  })

  test('不是错报与没有 verdict 的旧数据照常出一格', () => {
    expect(
      render(<VerdictCell judgment={judgment({ verdict: 'not_misstatement', proposed_action: 'close' })} />),
    ).toContain('Not a misstatement')
    expect(render(<VerdictCell judgment={judgment({ verdict: null, proposed_action: 'more_evidence' })} />)).toContain(
      'Cannot tell yet',
    )
  })
})

describe('两方的建议', () => {
  test('按定论的说法显示, 没有建议动作时说明', () => {
    expect(render(<ProposalVerdict action="more_evidence" />)).toContain('Cannot tell yet')
    expect(render(<ProposalVerdict action={null} />)).toContain('No proposal')
  })
})

describe('AI 建议的更正分录', () => {
  test('每行科目代码与名称, 借方或贷方金额, 摘要; 金额按项目币种', () => {
    const html = render(<CorrectionList entries={CORRECTION} />, 'USD')
    expect(html).toContain('AI-suggested correcting entry')
    expect(html).toContain('711')
    expect(html).toContain('Less Accumulated Depreciation on Office Equipment')
    expect(html).toContain('Debit')
    expect(html).toContain('USD 825.00')
    expect(html).toContain('Reverse the depreciation credited to accumulated depreciation.')
    expect(html).toContain('710')
    expect(html).toContain('Office Equipment')
    expect(html).toContain('Credit')
    expect(html).toContain('USD 750.00')
    expect(html).not.toContain('AUD')
  })

  test('没有分录时整块不渲染 (旧数据与人工判断没填)', () => {
    expect(render(<CorrectionList entries={[]} />)).toBe('')
    expect(render(<CorrectionList entries={correctionEntries({})} />)).toBe('')
  })

  test('错报列表 (错报页与 FIN-01 材料) 显示 Misstatement.correction (需求 R368 第 4 条)', () => {
    const html = render(<MisstatementList items={[misstatement({ correction: CORRECTION })]} />)
    expect(html).toContain('AI-suggested correcting entry')
    expect(html).toContain('AUD 825.00')
    expect(html).toContain('AUD 750.00')
    expect(html).toContain('Reverse the office equipment debited by manual journal XJ-499.')
    const bare = render(<MisstatementList items={[misstatement()]} />)
    expect(bare).not.toContain('AI-suggested correcting entry')
  })
})

// 判断卡 (对话里与表单视图共用) 的整体排布: 定论在最上面, 下面是理由与更正分录
const EID = 'ENG-TEST'

function renderJudgmentCard(target: Judgment): string {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(engagementsGetEngagementQueryKey({ path: { eid: EID } }), {
    engagement: { id: EID, currency: 'AUD' },
    features: [],
    run_languages: {},
  } as unknown as EngagementOverview)
  const ranked = {
    view: {
      task: {
        id: 'T-1',
        procedure_id: 'GEN-02',
        target_version: 1,
        status: 'pending',
        kind: 'normal',
        target_type: 'Judgment',
        run_id: 'RUN-GEN-02-1',
      },
      target,
      allowed_decisions: ['confirm', 'override', 'misstatement'],
      amount_cents: 75000,
      tier: 'read_reasoning',
      procedure_name: 'Journal entry exception attribution',
    },
    amount: 75000,
    tier: 'read_reasoning',
    subject: 'XJ-499',
  } as unknown as RankedTask
  try {
    return renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <I18nProvider>
          <MoneyProvider currency="AUD">
            <JudgmentCard eid={EID} ranked={ranked} />
          </MoneyProvider>
        </I18nProvider>
      </QueryClientProvider>,
    )
  } finally {
    client.clear()
  }
}

describe('判断卡', () => {
  test('定论在最上面, 叙述文字标为理由, 更正分录列在下面 (需求 R368)', () => {
    const html = renderJudgmentCard(
      judgment({ verdict: 'misstatement', misstatement_draft: { ...DRAFT, correction: CORRECTION } }),
    )
    expect(html).toContain('data-verdict="misstatement"')
    expect(html).toContain('AUD 750.00')
    expect(html).toContain('Overstated')
    expect(html).toContain('AI-suggested correcting entry')
    // 定论行在理由之前, 更正分录在理由之前
    expect(html.indexOf('data-verdict="misstatement"')).toBeLessThan(html.indexOf('>Reason<'))
    expect(html.indexOf('AI-suggested correcting entry')).toBeLessThan(html.indexOf('>Reason<'))
    expect(html).toContain('XJ-499 has no basis')
  })

  test('verdict 为空时按建议动作回退显示, 不带更新载荷 (旧数据的判断卡照常显示)', () => {
    const html = renderJudgmentCard(
      judgment({ verdict: null, proposed_action: 'more_evidence', misstatement_draft: null }),
    )
    expect(html).toContain('data-verdict="undetermined"')
    expect(html).toContain('Cannot tell yet')
    expect(html).not.toContain('data-testid="correction-entries"')
  })
})
