// 复核选项按 AI 结论给 (lib/review 的 reviewChoices, defaultChoice, 需求 R364, Issue #67) 与批量确认的范围 (taskTier)
// 的测试, bun test 运行. 数据是合成的: 任务与判断只填用到的字段.
import { describe, expect, test } from 'bun:test'
import type { EngagementOverview, ReviewTaskView } from '../src/client/types.gen'
import {
  buildRecord,
  CHOICE_DECISIONS,
  defaultChoice,
  draftFromMisstatement,
  emptyDraft,
  reviewChoices,
  taskTier,
} from '../src/lib/review'
import { en } from '../src/locales/en'

const DRAFT = {
  type: 'factual',
  account: 'Office equipment (710)',
  assertion: 'Existence',
  amount_cents: 75000,
  direction: 'overstated',
  account_code: '710',
}
// AI 建议的更正分录 (需求 R367, R368): 随 "同意/修改后认定" 的草稿带出并提交
const CORRECTION = [
  {
    account_code: '711',
    account_name: 'Less Accumulated Depreciation on Office Equipment',
    debit_cents: 75000,
    credit_cents: 0,
    description: 'Reverse the depreciation credited to accumulated depreciation.',
  },
]
const NORMAL = ['confirm', 'override', 'misstatement']

type Options = {
  kind?: string
  draft?: object | null
  update?: string
  allowed?: string[]
  tier?: string
  verdict?: string | null
}

function view(
  action: string,
  { kind = 'normal', draft = null, update, allowed = NORMAL, tier, verdict }: Options = {},
) {
  return {
    task: { id: 'T-1', kind, procedure_id: 'GEN-02', target_version: 2, status: 'pending' },
    target: {
      id: 'J-1',
      subject_id: 'XJ-499',
      conclusion: 'synthetic',
      proposed_action: action,
      verdict: verdict ?? null,
      misstatement_draft: draft,
    },
    allowed_decisions: allowed,
    misstatement_update_kind: update ?? null,
    tier,
  } as unknown as ReviewTaskView
}

describe('reviewChoices', () => {
  test('判为错报: 同意即认定错报, 不同意为不是错报, 另可修改后认定; 没有一项是确认', () => {
    const choices = reviewChoices(view('misstatement', { draft: DRAFT }))
    expect(choices).toEqual(['agree_misstatement', 'disagree_not_misstatement', 'amend_misstatement'])
    expect(choices?.map((choice) => CHOICE_DECISIONS[choice])).toEqual(['misstatement', 'override', 'misstatement'])
  })

  test('判为不是错报 (accept, close): 同意为确认, 不同意为认定错报; 暂时判断不了: 认定错报或不是错报', () => {
    for (const action of ['accept', 'close']) {
      expect(reviewChoices(view(action))).toEqual(['agree_clear', 'disagree_misstatement'])
    }
    const undetermined = reviewChoices(view('more_evidence'))
    expect(undetermined).toEqual(['misstatement', 'not_misstatement'])
    expect(undetermined?.map((choice) => CHOICE_DECISIONS[choice])).toEqual(['misstatement', 'override'])
  })

  test('只留后端允许的决定; 不是 normal 任务或判断带错报更新载荷 (FIN-03, FIN-09) 时照 allowed_decisions', () => {
    expect(reviewChoices(view('misstatement', { allowed: ['misstatement'] }))).toEqual([
      'agree_misstatement',
      'amend_misstatement',
    ])
    expect(reviewChoices(view('misstatement', { kind: 'dispute', allowed: ['dispute_resolution'] }))).toBeNull()
    expect(reviewChoices(view('accept', { update: 'classification' }))).toBeNull()
  })

  test('定论优先于建议动作 (需求 R368): verdict 是判断的定论, 为空时才回退到 proposed_action', () => {
    // 定论与建议动作不一致时按定论给选项 (契约里两者一一对应, 这里挡不一致的数据)
    expect(reviewChoices(view('accept', { verdict: 'misstatement', draft: DRAFT }))).toEqual([
      'agree_misstatement',
      'disagree_not_misstatement',
      'amend_misstatement',
    ])
    expect(reviewChoices(view('misstatement', { verdict: 'not_misstatement' }))).toEqual([
      'agree_clear',
      'disagree_misstatement',
    ])
    expect(reviewChoices(view('accept', { verdict: 'undetermined' }))).toEqual(['misstatement', 'not_misstatement'])
  })

  test('verdict 为空 (旧数据与人工判断) 时按 proposed_action 回退, 不崩', () => {
    expect(
      reviewChoices(view('misstatement', { verdict: null, draft: DRAFT }))?.map((item) => CHOICE_DECISIONS[item]),
    ).toEqual(['misstatement', 'override', 'misstatement'])
    expect(reviewChoices(view('accept', { verdict: null }))).toEqual(['agree_clear', 'disagree_misstatement'])
    expect(reviewChoices(view('more_evidence', { verdict: null }))).toEqual(['misstatement', 'not_misstatement'])
  })
})

describe('defaultChoice', () => {
  test('判为错报且有草稿时默认 "同意", 提交的是带 AI 草稿的认定错报', () => {
    const task = view('misstatement', { draft: DRAFT })
    const choice = defaultChoice(task, reviewChoices(task))
    expect(choice).toBe('agree_misstatement')
    if (!choice) throw new Error('没有默认选项')
    const body = buildRecord(task, CHOICE_DECISIONS[choice], draftFromMisstatement(emptyDraft(), DRAFT as never), en)
    expect(body).toMatchObject({ decision: 'misstatement', misstatement_draft: DRAFT })
  })

  test('其余不预选: 判为不是错报, 暂时判断不了, 判为错报而没有草稿', () => {
    for (const task of [view('accept'), view('more_evidence'), view('misstatement')]) {
      expect(defaultChoice(task, reviewChoices(task))).toBeNull()
    }
  })
})

describe('AI 建议的更正分录', () => {
  test('"同意/修改后认定" 的草稿带出判断的更正分录, 提交时一并给后端 (需求 R367, R368)', () => {
    const draft = { ...DRAFT, correction: CORRECTION }
    const task = view('misstatement', { draft })
    const form = draftFromMisstatement(emptyDraft(), draft as never)
    expect(form.correction).toEqual(CORRECTION)
    const body = buildRecord(task, 'misstatement', form, en)
    expect(body).toMatchObject({ decision: 'misstatement', misstatement_draft: draft })
  })

  test('没有 AI 更正分录时不提交 correction 字段 (人工填的草稿与旧数据)', () => {
    const form = draftFromMisstatement(emptyDraft(), DRAFT as never)
    expect(form.correction).toEqual([])
    const body = buildRecord(view('misstatement', { draft: DRAFT }), 'misstatement', form, en)
    expect(body).toMatchObject({ decision: 'misstatement' })
    expect((body as { misstatement_draft?: { correction?: unknown } }).misstatement_draft?.correction).toBeUndefined()
  })
})

describe('批量确认的范围', () => {
  const overview = {} as EngagementOverview
  test('后端给可批量一档时, 只有判为不是错报的留在这一档, 其余改为阅读推理链', () => {
    const tier = (action: string) => taskTier(view(action, { tier: 'batch_eligible' }), null, overview)
    expect(tier('accept')).toBe('batch_eligible')
    expect(tier('misstatement')).toBe('read_reasoning')
    expect(tier('more_evidence')).toBe('read_reasoning')
  })
})
