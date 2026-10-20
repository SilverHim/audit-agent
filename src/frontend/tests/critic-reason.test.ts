// 独立复核理由的显示 (lib/review 的 criticReasonText, 后端 R309) 的测试, bun test 运行. 数据是合成的: 只填用到的字段;
// 判断 ID 与原因照 Xero 演示的 J-CSH-02-1-BA-091 (未命中触发规则) 与 REV-02 的 L2 跳过.
import { describe, expect, test } from 'bun:test'
import type { CriticReasonCode, Judgment } from '../src/client/types.gen'
import { criticReasonText } from '../src/lib/review'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

function judgment(fields: Partial<Judgment>): Judgment {
  return { id: 'J-CSH-02-1-BA-091', critic_verdict: 'skipped', ...fields } as Judgment
}

const NOT_TRIGGERED = judgment({ critic_reason: '未命中独立复核的触发规则', critic_reason_code: 'not_triggered' })
const L2 = judgment({
  id: 'J-REV-02-1-SI-1',
  critic_reason: 'L2: 审计师逐条复核, 不经独立复核 (GEN-09 跳过规则)',
  critic_reason_code: 'level_l2',
})

// 后端 CriticReasonCode 的全部取值 (contracts/models.py). 文案覆盖全部取值由 en.ts 的 satisfies 经 tsc 保证, 这里再核对
// 中英两份的键
const CODES = {
  critic_off: true,
  no_review_model: true,
  level_l1: true,
  level_l2: true,
  not_triggered: true,
  no_evidence: true,
  first_round_invalid: true,
  second_round_invalid: true,
  chain_invalid: true,
  review_error: true,
} satisfies Record<CriticReasonCode, true>

describe('criticReasonText', () => {
  test('验收 1: BA-091 未命中触发规则与 L2 跳过, 英文界面显示英文, 中文界面显示中文', () => {
    expect(criticReasonText(NOT_TRIGGERED, en)).toEqual({
      text: 'No independent review trigger applied',
      detail: null,
      raw: false,
    })
    expect(criticReasonText(NOT_TRIGGERED, zh)?.text).toBe('未命中独立复核的触发规则')
    expect(criticReasonText(L2, en)?.text).toBe('L2: the auditor reviews each item, so independent review is skipped')
    expect(criticReasonText(L2, zh)?.text).toBe('L2：审计师逐条复核，不经独立复核')
  })

  test('验收 2: 旧数据只有中文原句, 照原文显示; 模型写的理由与认不出的代码也照原文', () => {
    const old = judgment({ critic_reason: '未命中独立复核的触发规则' })
    expect(criticReasonText(old, en)).toEqual({ text: '未命中独立复核的触发规则', detail: null, raw: true })
    const reviewed = judgment({
      critic_verdict: 'consistent',
      critic_reason: 'Same finding.',
      critic_reason_code: null,
    })
    expect(criticReasonText(reviewed, zh)).toEqual({ text: 'Same finding.', detail: null, raw: true })
    const unknown = judgment({ critic_reason: '新原因', critic_reason_code: 'later_code' as CriticReasonCode })
    expect(criticReasonText(unknown, en)?.text).toBe('新原因')
    expect(criticReasonText(judgment({}), en)).toBeNull()
  })

  test('没做成的带技术细节, 原样给出; 每个代码中英文都有文案且不同', () => {
    const failed = judgment({
      critic_verdict: 'not_applicable',
      critic_reason: '独立复核没有完成: 第一轮输出未通过 syntax 校验 (not json)',
      critic_reason_code: 'first_round_invalid',
      critic_reason_detail: 'syntax: not json',
    })
    expect(criticReasonText(failed, en)).toEqual({
      text: 'Could not be completed: the independent conclusion failed validation',
      detail: 'syntax: not json',
      raw: false,
    })
    for (const code of Object.keys(CODES) as CriticReasonCode[]) {
      const texts = [en, zh].map((t) => criticReasonText(judgment({ critic_reason_code: code }), t))
      expect(texts.every((r) => r !== null && !r.raw && r.text.length > 0)).toBe(true)
      expect(texts[0]?.text).not.toBe(texts[1]?.text)
    }
    expect(Object.keys(en.judgment.critic.reasonCodes).sort()).toEqual(Object.keys(CODES).sort())
    expect(Object.keys(zh.judgment.critic.reasonCodes).sort()).toEqual(Object.keys(CODES).sort())
  })
})
