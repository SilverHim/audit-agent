import type {
  AgentId,
  AuditObjective,
  AuditPlan,
  Engine,
  GateDecision,
  GateDecisionIn,
  PlanConfirmation,
  PlanRow,
  PlanStatus,
  PlanView,
  ProcedureStatus,
  ProcedureSummary,
} from '@/client/types.gen'
import { apiCode, apiDetail, errorText } from '@/lib/errors'
import type { Messages } from '@/locales/en'

// 审计计划页的纯函数 (后端需求 R314 的 GET /plan, 页面需求 R315): 程序表的行, 修订历史, 当前计划.
// 程序名称, 方法, 负责智能体与状态取总览的程序行, 与审计程序页同一来源

export type PlanTableRow = {
  key: string // 列表 key: 目标与程序编号, 同一计划内唯一
  objective: AuditObjective | null // 为空为通用程序 (不针对单一认定)
  procedureId: string
  name: string | null // 总览程序行的名称; 总览里没有时为空, 页面显示编号
  engine: Engine | null
  owner: AgentId | null
  status: ProcedureStatus | null
  stale: boolean // 过期: 读的数据在执行后变过
  extent: PlanRow['extent']
  sampleSize: number | null
  note: string | null // 当前计划对该程序的性质, 时间与范围说明 (AI 写的英文; ASA 300 第 9 段); 没有为空
}

// 当前计划 (后端第 0 节的取法, current_plan_id 指向它); 没有为 undefined
export function currentPlan(view: PlanView): AuditPlan | undefined {
  return view.plans.find((plan) => plan.id === view.current_plan_id)
}

// 程序表: 后端已按 "风险或认定 → 程序" 排好, 这里只接上审计目标与总览的程序行
export function planTable(view: PlanView, procedures: ProcedureSummary[]): PlanTableRow[] {
  const objectives = new Map(view.objectives.map((item) => [item.id, item]))
  const rows = new Map(procedures.map((item) => [item.id, item]))
  const notes = new Map(
    (currentPlan(view)?.procedure_notes ?? []).map((item) => [item.procedure_id, item.nature_timing_extent]),
  )
  return view.rows.map((row) => {
    const procedure = rows.get(row.procedure_id)
    return {
      key: `${row.objective_id ?? '-'}:${row.procedure_id}`,
      objective: row.objective_id ? (objectives.get(row.objective_id) ?? null) : null,
      procedureId: row.procedure_id,
      name: procedure?.name ?? null,
      engine: procedure?.engine ?? null,
      owner: procedure?.owner ?? null,
      status: procedure?.status ?? null,
      stale: procedure?.stale ?? false,
      extent: row.extent,
      sampleSize: row.sample_size ?? null,
      note: notes.get(row.procedure_id) ?? null,
    }
  })
}

export type Revision = {
  plan: AuditPlan // 该修订的最新版本
  status: PlanStatus
  decisions: GateDecision[] // 针对该修订的 PLN-15 决定, 按决定时间 (驳回理由, 信息不足, 批准)
}

// 修订历史, 新的在前: 各修订的状态与针对它的决定; 批准人与时间在计划上 (approved_by, approved_at)
export function revisionHistory(view: PlanView): Revision[] {
  return [...view.plans]
    .sort((a, b) => b.revision - a.revision)
    .map((plan) => ({
      plan,
      status: plan.status ?? 'draft',
      decisions: view.decisions.filter((decision) => decision.target_id === plan.id),
    }))
}

// 方法: rule → 规则核对, llm → AI 判断, hybrid → 规则核对 + AI 判断, manual → 人工; 总览里没有的程序为 "—"
export function methodLabel(engine: Engine | null, t: Messages): string {
  return engine ? t.planPage.methods[engine] : '—'
}

// 范围: 全量, 或抽样 n
export function extentLabel(row: Pick<PlanTableRow, 'extent' | 'sampleSize'>, t: Messages): string {
  return row.extent === 'sample' && row.sampleSize != null ? t.planPage.sample(row.sampleSize) : t.planPage.full
}

// PLN-15 批准时三项确认的初值 (需求 R206, R315): 是否需要项目质量复核与依据取 AI 建议; 不需要审计师的专家由审计师
// 自己勾选, 初值为 false
export function initialConfirmation(plan: AuditPlan): PlanConfirmation {
  return {
    eqr_required: plan.eqr_recommendation.required,
    eqr_basis: plan.eqr_recommendation.basis,
    auditor_expert_not_required: false,
  }
}

// PLN-15 批准的提交体: base (闸门, 绑定的计划与版本, 理由) 加三项确认, 依据去掉首尾空白; 依据为空时返回提示文字
export function planApprovalBody(
  base: GateDecisionIn,
  confirmation: PlanConfirmation,
  t: Messages,
): GateDecisionIn | string {
  const basis = confirmation.eqr_basis.trim()
  if (!basis) return t.planPage.confirm.basisRequired
  return { ...base, plan_confirmation: { ...confirmation, eqr_basis: basis } }
}

// PLN-15 决定被拒的文字: 缺必填项 (MISSING_FIELD, 后端的说明为字段名) 与取值不合格 (INVALID_VALUE, 如依据为空, 附后端
// 的说明) 各有文案; 版本不符 (VERSION_MISMATCH) 用闸门的说法 (呈报材料已更新), 全局文案是中性的; 其余
// (ENGAGEMENT_BUSY 等) 同 errorText
export function planDecisionErrorText(error: unknown, t: Messages): string {
  const code = apiCode(error)
  const message = apiDetail(error)?.message
  if (code === 'MISSING_FIELD') return t.planPage.errors.missing(message ?? '')
  if (code === 'INVALID_VALUE') return message ? t.planPage.errors.invalidWith(message) : t.planPage.errors.invalid
  if (code === 'VERSION_MISMATCH') return t.planPage.errors.stale
  return errorText(error, t)
}
