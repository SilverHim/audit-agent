// 审计计划页的纯函数 (lib/plan, 需求 R315) 与时间线认出 PLN-15 决定 (lib/timeline), bun test 运行.
// 程序表的行序由后端 GET /plan 定 (tests/test_query.py 守), 这里只看前端怎样接上总览的程序行与标签
import { describe, expect, test } from 'bun:test'
import type { AuditPlan, GateDecision, PlanView, ProcedureSummary, TrailEntry } from '../src/client/types.gen'
import {
  currentPlan,
  extentLabel,
  methodLabel,
  planDecisionErrorText,
  planTable,
  revisionHistory,
} from '../src/lib/plan'
import { buildTimeline } from '../src/lib/timeline'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

function plan(revision: number, status: AuditPlan['status']): AuditPlan {
  return { id: `PLAN-${revision}`, revision, status } as AuditPlan
}

function decision(id: string, target: string, verdict: GateDecision['decision'], at: string): GateDecision {
  return { id, gate: 'PLN-15', target_id: target, target_version: 1, decision: verdict, decided_at: at } as GateDecision
}

const VIEW: PlanView = {
  current_plan_id: 'PLAN-2',
  plans: [plan(1, 'superseded'), plan(2, 'approved')],
  decisions: [
    decision('D-1', 'PLAN-1', 'reject', '2026-10-19T01:00:00Z'),
    decision('D-2', 'PLAN-2', 'approve', '2026-10-19T02:00:00Z'),
  ],
  objectives: [{ id: 'OBJ-REV-OCC', account: 'Revenue', assertion: 'Occurrence', risk_level: 'significant' }],
  risks: [],
  rows: [
    { objective_id: 'OBJ-REV-OCC', procedure_id: 'REV-08', extent: 'full' },
    { objective_id: null, procedure_id: 'GEN-05', extent: 'sample', sample_size: 25 },
  ],
} as PlanView

const REV08 = { id: 'REV-08', name: '凭证三单匹配', engine: 'rule', owner: 'A6', status: 'executing', stale: true }

describe('审计计划页', () => {
  test('程序表接上审计目标与总览的程序行; 总览里没有的程序方法, 负责智能体与状态为空', () => {
    const rows = planTable(VIEW, [REV08 as ProcedureSummary])
    expect(
      rows.map((row) => [row.key, row.objective?.id ?? null, row.engine, row.owner, row.status, row.stale]),
    ).toEqual([
      ['OBJ-REV-OCC:REV-08', 'OBJ-REV-OCC', 'rule', 'A6', 'executing', true],
      ['-:GEN-05', null, null, null, null, false],
    ])
    expect(rows.map((row) => extentLabel(row, zh))).toEqual(['全量', '抽样 25'])
    expect(rows.map((row) => extentLabel(row, en))).toEqual(['Full population', 'Sample of 25'])
  })

  test('方法的标签: 规则核对, AI 判断, 两者兼有, 人工; 没有为 —', () => {
    const engines = ['rule', 'llm', 'hybrid', 'manual', null] as const
    expect(engines.map((engine) => methodLabel(engine, zh))).toEqual([
      '规则核对',
      'AI 判断',
      '规则核对 + AI 判断',
      '人工',
      '—',
    ])
  })

  test('当前计划取 current_plan_id; 修订历史新的在前, 各带针对它的决定', () => {
    expect(currentPlan(VIEW)?.id).toBe('PLAN-2')
    expect(currentPlan({ ...VIEW, current_plan_id: null })).toBeUndefined()
    expect(revisionHistory(VIEW).map((r) => [r.plan.id, r.status, r.decisions.map((d) => d.id)])).toEqual([
      ['PLAN-2', 'approved', ['D-2']],
      ['PLAN-1', 'superseded', ['D-1']],
    ])
  })
})

describe('时间线', () => {
  test('PLN-15 的批准与退回识别为闸门决定', () => {
    const entries = ['approve', 'reject'].map(
      (verdict, n) =>
        ({
          id: `TR-${n}`,
          engagement_id: 'ENG-1',
          kind: 'decision',
          actor: 'U-1',
          summary: `PLN-15 ${verdict}`,
          created_at: `2026-10-19T0${n}:00:00Z`,
          payload: { reason: verdict === 'reject' ? 'Add timing' : null },
        }) as TrailEntry,
    )
    expect(
      buildTimeline(entries, false).map((item) => (item.kind === 'decision' ? [item.gate, item.verdict] : [])),
    ).toEqual([
      ['PLN-15', 'approve'],
      ['PLN-15', 'reject'],
    ])
  })
})

describe('计划审批被拒的文字', () => {
  const rejected = (code: string, message?: string) => ({ detail: { code, message } })
  test('缺必填项与取值不合格各有文案 (附后端的说明); 版本不符同通用错误码文案; 不是后端错误体照原文', () => {
    expect(planDecisionErrorText(rejected('MISSING_FIELD', 'plan_confirmation'), zh)).toBe(
      '决定缺少必填项：plan_confirmation。',
    )
    expect(planDecisionErrorText(rejected('MISSING_FIELD', 'reason'), en)).toBe(
      'The decision is missing a required field: reason.',
    )
    expect(planDecisionErrorText(rejected('INVALID_VALUE', 'eqr_basis 不能为空'), zh)).toBe(
      '决定内容不合要求：eqr_basis 不能为空',
    )
    expect(planDecisionErrorText(rejected('INVALID_VALUE'), en)).toBe(en.planPage.errors.invalid)
    expect(planDecisionErrorText(rejected('VERSION_MISMATCH', 'stale'), zh)).toBe('呈报材料已更新，请刷新后重新决定。')
    expect(planDecisionErrorText(new Error('network down'), en)).toBe('network down')
  })
})
