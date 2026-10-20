// 审计计划页的纯函数 (lib/plan, 需求 R315), 决定卡片的 key (lib/engagement) 与时间线认出 PLN-15 决定 (lib/timeline),
// bun test 运行.
// 程序表的行序由后端 GET /plan 定 (tests/test_query.py 守), 这里只看前端怎样接上总览的程序行与标签
import { describe, expect, test } from 'bun:test'
import type {
  AuditPlan,
  GateDecision,
  GateDecisionIn,
  PlanView,
  ProcedureSummary,
  TrailEntry,
} from '../src/client/types.gen'
import { decisionCardKey } from '../src/lib/engagement'
import { errorText } from '../src/lib/errors'
import {
  currentPlan,
  extentLabel,
  initialConfirmation,
  methodLabel,
  planApprovalBody,
  planDecisionErrorText,
  planTable,
  revisionHistory,
} from '../src/lib/plan'
import { buildTimeline } from '../src/lib/timeline'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

// 程序的性质, 时间与范围说明: 当前计划 PLAN-2 只有 REV-08 的, 已取代的 PLAN-1 有 GEN-05 的 (不该取到)
function plan(revision: number, status: AuditPlan['status'], noted: string): AuditPlan {
  const procedure_notes = [{ procedure_id: noted, nature_timing_extent: `PLAN-${revision} note on ${noted}` }]
  return { id: `PLAN-${revision}`, revision, status, procedure_notes } as AuditPlan
}

function decision(id: string, target: string, verdict: GateDecision['decision'], at: string): GateDecision {
  return { id, gate: 'PLN-15', target_id: target, target_version: 1, decision: verdict, decided_at: at } as GateDecision
}

const VIEW: PlanView = {
  current_plan_id: 'PLAN-2',
  plans: [plan(1, 'superseded', 'GEN-05'), plan(2, 'approved', 'REV-08')],
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
  test('程序表接上审计目标与总览的程序行; 总览里没有的程序方法, 负责智能体与状态为空; 说明只取当前计划的', () => {
    const rows = planTable(VIEW, [REV08 as ProcedureSummary])
    expect(
      rows.map((row) => [row.key, row.objective?.id ?? null, row.engine, row.owner, row.status, row.stale, row.note]),
    ).toEqual([
      ['OBJ-REV-OCC:REV-08', 'OBJ-REV-OCC', 'rule', 'A6', 'executing', true, 'PLAN-2 note on REV-08'],
      ['-:GEN-05', null, null, null, null, false, null],
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

describe('计划审批的提交体', () => {
  const base: GateDecisionIn = { gate: 'PLN-15', target_id: 'PLAN-2', target_version: 1, decision: 'approve' }
  // AI 建议需要项目质量复核: 与写死的 false 分得开 (Xero 固定输出的建议是不需要)
  const listed = { eqr_recommendation: { required: true, basis: 'Listed entity (ASQM 1 para 34(f))' } } as AuditPlan

  test('三项确认的初值: 是否需要项目质量复核与依据取 AI 建议, 不需要审计师的专家为否', () => {
    expect(initialConfirmation(listed)).toEqual({
      eqr_required: true,
      eqr_basis: 'Listed entity (ASQM 1 para 34(f))',
      auditor_expert_not_required: false,
    })
  })

  test('提交体带三项确认, 依据去掉首尾空白; 依据为空白时返回提示', () => {
    const confirmation = {
      ...initialConfirmation(listed),
      eqr_basis: '  Listed entity \n',
      auditor_expert_not_required: true,
    }
    expect(planApprovalBody(base, confirmation, zh)).toEqual({
      ...base,
      plan_confirmation: { eqr_required: true, eqr_basis: 'Listed entity', auditor_expert_not_required: true },
    })
    expect(planApprovalBody(base, { ...confirmation, eqr_basis: ' \n ' }, zh)).toBe('请填写是否需要项目质量复核的依据')
    expect(planApprovalBody(base, { ...confirmation, eqr_basis: '' }, en)).toBe(en.planPage.confirm.basisRequired)
  })

  test('决定卡片的 key 含呈报对象: 退回后的 PLAN-2 v1 与 PLAN-1 v1 不同, 同一对象换版本也不同', () => {
    const keys = [
      { gate: 'PLN-15', target_id: 'PLAN-1', target_version: 1 },
      { gate: 'PLN-15', target_id: 'PLAN-2', target_version: 1 },
      { gate: 'PLN-15', target_id: 'PLAN-2', target_version: 2 },
    ].map((view) => decisionCardKey(view as Parameters<typeof decisionCardKey>[0]))
    expect(new Set(keys).size).toBe(3)
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
  test('缺必填项与取值不合格各有文案 (附后端的说明); 版本不符用闸门的说法 (全局文案是中性的); 不是后端错误体照原文', () => {
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
    expect(errorText(rejected('VERSION_MISMATCH', 'stale'), zh)).toBe('数据已被更新，请刷新后重试。')
    expect(planDecisionErrorText(rejected('VERSION_MISMATCH'), en)).not.toBe(
      errorText(rejected('VERSION_MISMATCH'), en),
    )
    expect(planDecisionErrorText(new Error('network down'), en)).toBe('network down')
  })
})
