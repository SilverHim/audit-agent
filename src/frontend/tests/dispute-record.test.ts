// 仲裁表单的换算 (lib/review 的 sideProposal, attachRule, buildDisputeRecord) 的测试, bun test 运行
// (不在 tsconfig 的 src 里, 不经 tsc). 数据是合成的, 照 Numbat 争议变体的 CSH-02 判断: 原判断建议关闭,
// 独立复核建议认定错报.
import { describe, expect, test } from 'bun:test'
import type { MisstatementDraft, ReviewRecordIn, ReviewTaskView } from '../src/client/types.gen'
import {
  attachRule,
  buildDisputeRecord,
  type DisputeInput,
  draftFromMisstatement,
  emptyDraft,
  type ReviewDraft,
  sideProposal,
} from '../src/lib/review'
import { zh } from '../src/locales/zh'

const CASH: MisstatementDraft = {
  type: 'factual',
  account: 'Cash',
  assertion: 'Cutoff',
  amount_cents: 5_000_000,
  direction: 'overstated',
}

function view(a6: string, a7: string | null, a7Draft: MisstatementDraft | null = null): ReviewTaskView {
  return {
    task: {
      id: 'T-J-CSH-02-1-BA-01-v1-dispute',
      version: 1,
      kind: 'dispute',
      procedure_id: 'CSH-02',
      target_type: 'Judgment',
      target_id: 'J-CSH-02-1-BA-01',
      target_version: 1,
      created_at: '2026-01-01T00:00:00Z',
      status: 'pending',
    },
    target: {
      id: 'J-CSH-02-1-BA-01',
      subject_id: 'BA-01',
      conclusion: 'synthetic',
      proposed_action: a6,
      misstatement_draft: a6 === 'misstatement' ? CASH : null,
      critic_proposed_action: a7,
      critic_misstatement_draft: a7Draft,
      exception_ids: [],
    },
    allowed_decisions: ['dispute_resolution'],
  } as unknown as ReviewTaskView
}

const DISPUTE = view('close', 'misstatement', CASH)
const OLD = view('close', null) // collab 之前的判断: 独立复核没有建议动作

function form(patch: Partial<ReviewDraft> = {}): ReviewDraft {
  return { ...emptyDraft(), reason: '  the bank confirmation shows the cut-off error  ', ...patch }
}

function input(patch: Partial<DisputeInput>): DisputeInput {
  return { choice: 'a7_upheld', attach: false, consulted: false, consultation: '', collab: true, ...patch }
}

function body(result: ReviewRecordIn | string): ReviewRecordIn {
  if (typeof result === 'string') throw new Error(result)
  return result
}

describe('sideProposal 与 attachRule', () => {
  test('两方的建议取各自的字段', () => {
    const judgment = DISPUTE.target as never
    expect(sideProposal(judgment, 'a6')).toEqual({ action: 'close', draft: null })
    expect(sideProposal(judgment, 'a7')).toEqual({ action: 'misstatement', draft: CASH })
    expect(sideProposal(OLD.target as never, 'a7')).toEqual({ action: null, draft: null })
    expect(sideProposal(null, 'a6')).toEqual({ action: null, draft: null })
  })

  test('collab 打开: 采纳的一方建议错报为必填, 建议其他动作为不显示, 没给建议与 neither 两可', () => {
    const judgment = DISPUTE.target as never
    expect(attachRule(judgment, 'a7_upheld', true)).toBe('required')
    expect(attachRule(judgment, 'a6_upheld', true)).toBe('hidden')
    expect(attachRule(judgment, 'neither', true)).toBe('optional')
    expect(attachRule(judgment, null, true)).toBe('optional')
    expect(attachRule(OLD.target as never, 'a7_upheld', true)).toBe('optional')
    expect(attachRule(view('misstatement', 'accept').target as never, 'a6_upheld', true)).toBe('required')
    expect(attachRule(view('misstatement', 'accept').target as never, 'a7_upheld', true)).toBe('hidden')
  })

  test('collab 关闭: 一律两可 (同原来)', () => {
    for (const choice of ['a6_upheld', 'a7_upheld', 'neither'] as const) {
      expect(attachRule(DISPUTE.target as never, choice, false)).toBe('optional')
    }
  })
})

describe('buildDisputeRecord', () => {
  test('采纳建议错报的一方: 不看勾选, 带上 (预填后可改的) 草稿', () => {
    const prefilled = draftFromMisstatement(form(), CASH)
    expect(prefilled.amount).toBe('50000.00')
    const record = body(buildDisputeRecord(DISPUTE, prefilled, input({ attach: false }), zh))
    expect(record).toMatchObject({
      decision: 'dispute_resolution',
      dispute_resolution: 'a7_upheld',
      reason: 'the bank confirmation shows the cut-off error',
      neither_action: null,
      override_conclusion: null,
      misstatement_draft: CASH,
      consulted: false,
      consultation_record: null,
    })
    const edited = body(buildDisputeRecord(DISPUTE, { ...prefilled, amount: '48000' }, input({}), zh))
    expect(edited.misstatement_draft?.amount_cents).toBe(4_800_000)
    expect(buildDisputeRecord(DISPUTE, form(), input({}), zh)).toBe(zh.errors.misstatementRequired)
  })

  test('采纳不建议错报的一方: 勾过也不带草稿', () => {
    const filled = draftFromMisstatement(form(), CASH)
    const record = body(buildDisputeRecord(DISPUTE, filled, input({ choice: 'a6_upheld', attach: true }), zh))
    expect(record.misstatement_draft).toBeNull()
  })

  test('neither 与没给建议的一方按勾选', () => {
    const filled = draftFromMisstatement(form({ conclusion: ' auditor view ' }), CASH)
    const neither = input({ choice: 'neither' })
    expect(body(buildDisputeRecord(DISPUTE, filled, neither, zh))).toMatchObject({
      neither_action: 'human_conclusion',
      override_conclusion: 'auditor view',
      misstatement_draft: null,
    })
    expect(body(buildDisputeRecord(DISPUTE, filled, { ...neither, attach: true }, zh)).misstatement_draft).toEqual(CASH)
    expect(buildDisputeRecord(DISPUTE, form(), neither, zh)).toBe(zh.errors.conclusionRequired)
    expect(body(buildDisputeRecord(OLD, filled, input({}), zh)).misstatement_draft).toBeNull()
    expect(body(buildDisputeRecord(OLD, filled, input({ attach: true }), zh)).misstatement_draft).toEqual(CASH)
  })

  test('系统外咨询: 勾选后咨询记录必填, 随决定提交', () => {
    const filled = draftFromMisstatement(form(), CASH)
    expect(buildDisputeRecord(DISPUTE, filled, input({ consulted: true, consultation: '  ' }), zh)).toBe(
      zh.errors.consultationRequired,
    )
    const record = body(
      buildDisputeRecord(DISPUTE, filled, input({ consulted: true, consultation: ' 质量复核合伙人: 同意调整 ' }), zh),
    )
    expect(record).toMatchObject({ consulted: true, consultation_record: '质量复核合伙人: 同意调整' })
    // 没勾选时不带记录
    const unchecked = body(buildDisputeRecord(DISPUTE, filled, input({ consultation: '写了又取消' }), zh))
    expect(unchecked).toMatchObject({ consulted: false, consultation_record: null })
  })

  test('collab 关闭: 不带咨询字段, 草稿只看勾选 (同原来)', () => {
    const filled = draftFromMisstatement(form(), CASH)
    const off = input({ collab: false, consulted: true, consultation: 'ignored' })
    const record = body(buildDisputeRecord(DISPUTE, filled, off, zh))
    expect('consulted' in record || 'consultation_record' in record).toBe(false)
    expect(record.misstatement_draft).toBeNull()
    expect(body(buildDisputeRecord(DISPUTE, filled, { ...off, choice: 'a6_upheld', attach: true }, zh))).toMatchObject({
      misstatement_draft: CASH,
    })
  })

  test('理由必填', () => {
    expect(buildDisputeRecord(DISPUTE, form({ reason: ' ' }), input({}), zh)).toBe(zh.errors.reasonRequired)
  })
})
