// 错报生命周期的前端计算 (lib/misstatements, 需求 R163), bun test 运行.
// 错报取 Xero 演示第 1 轮的两条 (M-D-XJ499: 710 75,000 高估; M-D-CSH02-091: 270 48.53 低估) 与 AJE-X01, AJE-X05
import { describe, expect, test } from 'bun:test'
import type { ManualTaskView, Misstatement, ReviewTaskView, TrailEntry } from '../src/client/types.gen'
import {
  accountOptions,
  correctionNoticeBody,
  emptyResponseRow,
  emptyUpdate,
  listedMisstatements,
  misstatementUpdate,
  remainingCents,
  responsesDetails,
  separateCode,
  updateBase,
  updateFromPayload,
  updateKindFor,
  verifiedEntries,
} from '../src/lib/misstatements'
import { buildTimeline } from '../src/lib/timeline'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const XJ499 = 'M-D-XJ499'
const CSH02 = 'M-D-CSH02-091'

function misstatement(patch: Partial<Misstatement> = {}): Misstatement {
  return {
    id: XJ499,
    version: 3,
    judgment_id: 'J-GEN-02-1-XJ-499',
    exception_id: null,
    review_record_id: 'D-1',
    type: 'factual',
    account: 'Office Equipment',
    assertion: 'Accuracy',
    amount_cents: 75000,
    direction: 'overstated',
    account_code: '710',
    status: 'communicated',
    ...patch,
  }
}

describe('生命周期', () => {
  test('剩余额: 验证前为金额减已更正额, 验证后取 residual_cents', () => {
    expect(remainingCents(misstatement())).toBe(75000)
    expect(
      remainingCents(misstatement({ status: 'partially_corrected', corrected_cents: 50000, residual_cents: 25000 })),
    ).toBe(25000)
  })

  test('已验证的更正凭证: 只认 corrected 与 partially_corrected 且有验证判断的; verifying 时还不算', () => {
    const entries = { adjustment_entry_nos: ['AJE-X01'], correction_notice_id: 'D-CN1' }
    expect(verifiedEntries(misstatement({ ...entries, status: 'verifying' }))).toEqual([])
    expect(verifiedEntries(misstatement({ ...entries, status: 'corrected', verified_by_judgment_id: null }))).toEqual(
      [],
    )
    const verified = misstatement({ ...entries, status: 'corrected', verified_by_judgment_id: 'J-FIN-09-1-M-D-XJ499' })
    expect(verifiedEntries(verified)).toEqual(['AJE-X01'])
  })
})

describe('更正通知', () => {
  const draft = { kind: 'adjustments_posted' as const, misstatementIds: [XJ499, CSH02], entryNos: [], reason: '' }

  test('客户已过账: 勾选的错报与凭证号, 理由可空', () => {
    expect(correctionNoticeBody({ ...draft, entryNos: ['AJE-X01', 'AJE-X05'] }, en)).toEqual({
      kind: 'adjustments_posted',
      misstatement_ids: [XJ499, CSH02],
      adjustment_entry_nos: ['AJE-X01', 'AJE-X05'],
      reason: null,
    })
    expect(correctionNoticeBody(draft, en)).toBe(en.correction.errors.entries)
    expect(correctionNoticeBody({ ...draft, misstatementIds: [] }, en)).toBe(en.correction.errors.misstatements)
  })

  test('停止等待: 理由必填 (去首尾空白), 不带凭证号', () => {
    const stop = { ...draft, kind: 'stop_waiting' as const, misstatementIds: [CSH02], entryNos: ['AJE-X05'] }
    expect(correctionNoticeBody({ ...stop, reason: '  ' }, zh)).toBe(zh.errors.reasonRequired)
    expect(correctionNoticeBody({ ...stop, reason: ' Not posted before the deadline. ' }, zh)).toEqual({
      kind: 'stop_waiting',
      misstatement_ids: [CSH02],
      adjustment_entry_nos: [],
      reason: 'Not posted before the deadline.',
    })
  })

  test('对话流: 更正通知的留痕 (后端 notice_trail) 按载荷显示种类, 错报与凭证号, 不按 "对象 决定" 拆摘要', () => {
    const trail = (n: number, payload: object) =>
      ({
        id: `TR-${n}`,
        engagement_id: 'ENG-1',
        kind: 'decision',
        actor: 'U-1',
        procedure_id: 'FIN-08',
        summary: 'not parsed',
        created_at: `2026-10-20T0${n}:00:00Z`,
        payload,
      }) as TrailEntry
    const posted = { kind: 'adjustments_posted', misstatement_ids: [CSH02, XJ499], adjustment_entry_nos: ['AJE-X01'] }
    const stopped = { kind: 'stop_waiting', misstatement_ids: [CSH02], adjustment_entry_nos: [], reason: 'Deadline.' }
    const items = buildTimeline([trail(1, { ...posted, reason: null }), trail(2, stopped)], false)
    expect(items.map(({ key, at, ...rest }) => rest)).toEqual([
      { kind: 'correction', notice: 'adjustments_posted', ids: [CSH02, XJ499], entries: ['AJE-X01'], reason: null },
      { kind: 'correction', notice: 'stop_waiting', ids: [CSH02], entries: [], reason: 'Deadline.' },
    ])
  })
})

describe('FIN-08 的登记', () => {
  const listed = [
    { id: XJ499, version: 2, account: 'Office Equipment', account_code: '710', amount_cents: 75000 },
    { id: CSH02, version: 2, account: 'Interest Income', account_code: '270', amount_cents: 4853 },
  ]
  const view = (resultModel: string | null) =>
    ({ request: { result_model: resultModel, material: { misstatements: listed } } }) as unknown as ManualTaskView

  test('结果模型为 ManagementResponses 时按材料逐条错报登记, 别的人工程序照通用表单', () => {
    expect(listedMisstatements(view('ManagementResponses'))?.map((item) => item.id)).toEqual([XJ499, CSH02])
    expect(listedMisstatements(view(null))).toBeNull()
  })

  test('每行一条回复: 依据材料中的版本, 时刻带时区, 文字去首尾空白; 同意与否照所选', () => {
    const row = {
      communicatedAt: '2026-10-19T15:00',
      communicatedTo: ' Alex Morgan, Director ',
      respondedAt: '2026-10-20T09:00',
      response: ' Agreed. ',
      agrees: true,
    }
    const result = responsesDetails(listed as never, { [XJ499]: row, [CSH02]: { ...row, agrees: false } }, en)
    if (!('details' in result)) throw new Error('应当通过')
    const [first, second] = result.details.responses as Array<Record<string, unknown>>
    expect(first).toMatchObject({
      misstatement_id: XJ499,
      base_version: 2,
      communicated_to: 'Alex Morgan, Director',
      response: 'Agreed.',
      agrees_to_correct: true,
    })
    expect(String(first?.communicated_at)).toMatch(/Z$/)
    expect(new Date(String(first?.communicated_at)).getTime()).toBe(new Date('2026-10-19T15:00').getTime())
    expect(second).toMatchObject({ misstatement_id: CSH02, agrees_to_correct: false })
  })

  test('有问题的行各给一条提示 (按错报 ID): 漏填的列出各项 (含未选是否同意), 回复早于沟通另报', () => {
    const early = {
      ...emptyResponseRow(),
      communicatedAt: '2026-10-20T09:00',
      communicatedTo: 'Alex Morgan',
      respondedAt: '2026-10-19T15:00',
      response: 'No.',
      agrees: false,
    }
    const result = responsesDetails(listed as never, { [CSH02]: early }, zh)
    expect(result).toEqual({
      errors: {
        [XJ499]: zh.fin08.errors.missing('沟通时间、沟通对象、回复时间、回复原文、是否同意更正'),
        [CSH02]: zh.fin08.errors.order,
      },
    })
  })

  test('是否同意更正缺省未选: 别的都填了也按漏填提示, 不当作同意 (ASA 450 第 9 段: 拒绝更正的一路不能被缺省值跳过)', () => {
    expect(emptyResponseRow().agrees).toBeNull()
    const filled = {
      communicatedAt: '2026-10-19T15:00',
      communicatedTo: 'Alex Morgan, Director',
      respondedAt: '2026-10-20T09:00',
      response: 'Agreed.',
      agrees: null,
    }
    const result = responsesDetails(listed as never, { [XJ499]: filled, [CSH02]: { ...filled, agrees: true } }, en)
    expect(result).toEqual({ errors: { [XJ499]: en.fin08.errors.missing(en.fin08.fields.agrees) } })
  })
})

describe('复核表单的错报更新载荷', () => {
  const carried = { misstatement_id: XJ499, base_version: 1, kind: 'classification' as const, type: 'factual' as const }

  // updateDecisions: 后端给的须带载荷的决定 (ReviewTaskView.update_decisions)
  function task(
    kind: string,
    target: object,
    updateDecisions: string[] = [],
    updateKind: string | null = 'classification',
  ): ReviewTaskView {
    return {
      task: { id: 'T-1', kind, subject_id: XJ499, procedure_id: 'FIN-03' },
      target,
      misstatement_update_kind: updateKind,
      update_decisions: updateDecisions,
    } as unknown as ReviewTaskView
  }
  const judgment = { conclusion: 'c', subject_id: XJ499, misstatement_update: carried }

  test('须带载荷的决定只看后端给的 update_decisions, 类别取 misstatement_update_kind; 不在其中或没有类别时不带', () => {
    const normal = task('normal', judgment, ['override'])
    expect(updateKindFor(normal, 'override')).toBe('classification')
    expect(updateKindFor(normal, 'confirm')).toBeNull()
    expect(updateKindFor(normal, null)).toBeNull()
    const exception = task('unattributed', { rule_id: 'V02' }, ['override', 'misstatement'], 'verification')
    expect(updateKindFor(exception, 'misstatement')).toBe('verification')
    expect(updateKindFor(task('normal', judgment, [], 'classification'), 'override')).toBeNull()
    expect(updateKindFor(task('normal', judgment, ['override'], null), 'override')).toBeNull()
  })

  test('依据: 判断带的载荷的错报与版本; 失败任务取对象在错报列表中的当前版本, 没取到为 null', () => {
    expect(updateBase(task('normal', judgment), [])).toEqual({ misstatement_id: XJ499, base_version: 1 })
    const failed = task('failed', { run_no: 1 })
    expect(updateBase(failed, [misstatement({ version: 2 })])).toEqual({ misstatement_id: XJ499, base_version: 2 })
    expect(updateBase(failed, [])).toBeNull()
  })

  test('分类: 类型与舞弊迹象, 有舞弊迹象须写理由; AI 的载荷可作起点', () => {
    const base = { misstatement_id: XJ499, base_version: 1 }
    const draft = { ...updateFromPayload(carried), type: 'judgmental' as const, fraud: true }
    expect(misstatementUpdate('classification', base, draft, en)).toBe(en.updateForm.errors.fraudReason)
    expect(
      misstatementUpdate('classification', base, { ...draft, fraudReason: ' sole director posted it ' }, en),
    ).toEqual({
      ...base,
      kind: 'classification',
      type: 'judgmental',
      fraud_indicative: true,
      fraud_reason: 'sole director posted it',
    })
    expect(misstatementUpdate('classification', null, emptyUpdate(), en)).toBe(en.updateForm.errors.base)
  })

  test('验证: 状态, 凭证号 (逗号或空白分隔), 金额按 AUD 转分; 是否须重新沟通恒为 null (后端按状态推出); 金额写不对报错', () => {
    const base = { misstatement_id: CSH02, base_version: 4 }
    const draft = {
      ...emptyUpdate(),
      status: 'partially_corrected' as const,
      entryNos: 'AJE-X05, AJE-X06 ',
      corrected: '40',
      residual: '8.53',
    }
    expect(misstatementUpdate('verification', base, draft, en)).toEqual({
      ...base,
      kind: 'verification',
      status: 'partially_corrected',
      adjustment_entry_nos: ['AJE-X05', 'AJE-X06'],
      corrected_cents: 4000,
      residual_cents: 853,
      needs_recommunication: null,
    })
    expect(misstatementUpdate('verification', base, { ...draft, residual: 'n/a' }, en)).toBe(
      en.updateForm.errors.amount,
    )
  })

  test('验证: 没写的可选字段为 null (表示不改), 凭证号为空不发 []', () => {
    const base = { misstatement_id: CSH02, base_version: 4 }
    expect(
      misstatementUpdate('verification', base, { ...emptyUpdate(), status: 'communicated', entryNos: ' ' }, en),
    ).toEqual({
      ...base,
      kind: 'verification',
      status: 'communicated',
      adjustment_entry_nos: null,
      corrected_cents: null,
      residual_cents: null,
      needs_recommunication: null,
    })
  })
})

describe('科目代码', () => {
  test.each([
    ['Office equipment (710)', '710', null],
    ['Office Equipment', '710', '710'],
    ['Office equipment (7100)', '710', '710'],
    ['Office Equipment', null, null],
  ])('另列与否: %p 代码 %p → %p', (account, code, expected) => {
    expect(separateCode({ account, account_code: code })).toBe(expected)
  })

  test('选项: 科目表的行按代码排 (数字序); 当前值不在表里时也列上, 不悄悄清空', () => {
    const rows = [
      { code: '820', name: 'GST' },
      { code: '710', name: 'Office Equipment' },
      { code: 'X', name: 7 },
      { name: 'no code' },
    ]
    expect(accountOptions(rows, '')).toEqual([
      { code: '710', name: 'Office Equipment' },
      { code: '820', name: 'GST' },
      { code: 'X', name: null },
    ])
    expect(accountOptions(rows, '1100').map((item) => item.code)).toEqual(['710', '820', '1100', 'X'])
  })
})
