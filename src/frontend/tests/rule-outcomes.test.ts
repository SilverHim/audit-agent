// 程序详情页的 "未执行的规则" (lib/rule-outcomes, 需求 R300) 的测试, bun test 运行. 执行记录是合成的: 只填用到的字段.
import { describe, expect, test } from 'bun:test'
import type { RuleOutcome } from '../src/client/types.gen'
import { notRunRules, outcomeStatuses, outcomeTone } from '../src/lib/rule-outcomes'

const R02: RuleOutcome = { rule_id: 'R02', status: 'skipped', reason: '数据集声明录入时间不可靠' }
const R04: RuleOutcome = { rule_id: 'R04', status: 'not_executable', reason: '前置检查未通过' }
const R07: RuleOutcome = { rule_id: 'R07', status: 'not_executable', reason: 'approval limit not declared' }

describe('notRunRules (验收 1)', () => {
  test('按记录的顺序列出没有执行的规则', () => {
    expect(notRunRules({ rule_outcomes: [R04, R02, R07] })).toEqual([R04, R02, R07])
  })

  test('全部执行了, 或旧记录没有这一项, 为空 (不显示这一块)', () => {
    expect(notRunRules({ rule_outcomes: [] })).toEqual([])
    expect(notRunRules({})).toEqual([])
  })
})

describe('两种状态 (验收 2)', () => {
  test('样式不同: not_executable 为提醒色, skipped 为中性色', () => {
    expect(outcomeTone('not_executable')).toBe('warn')
    expect(outcomeTone('skipped')).toBe('neutral')
  })

  test('只说明出现过的状态, 本该执行的在前, 不重复', () => {
    expect(outcomeStatuses([R02, R04, R07])).toEqual(['not_executable', 'skipped'])
    expect(outcomeStatuses([R02])).toEqual(['skipped'])
    expect(outcomeStatuses([R07, R04])).toEqual(['not_executable'])
    expect(outcomeStatuses([])).toEqual([])
  })
})
