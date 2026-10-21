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
const NORMAL = ['confirm', 'override', 'misstatement']

type Options = { kind?: string; draft?: object | null; update?: string; allowed?: string[]; tier?: string }

function view(action: string, { kind = 'normal', draft = null, update, allowed = NORMAL, tier }: Options = {}) {
  return {
    task: { id: 'T-1', kind, procedure_id: 'GEN-02', target_version: 2, status: 'pending' },
    target: {
      id: 'J-1',
      subject_id: 'XJ-499',
      conclusion: 'synthetic',
      proposed_action: action,
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

describe('批量确认的范围', () => {
  const overview = {} as EngagementOverview
  test('后端给可批量一档时, 只有判为不是错报的留在这一档, 其余改为阅读推理链', () => {
    const tier = (action: string) => taskTier(view(action, { tier: 'batch_eligible' }), null, overview)
    expect(tier('accept')).toBe('batch_eligible')
    expect(tier('misstatement')).toBe('read_reasoning')
    expect(tier('more_evidence')).toBe('read_reasoning')
  })
})
