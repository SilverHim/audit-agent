import type {
  Assertion,
  AuditException,
  Decision,
  Direction,
  EngagementOverview,
  Judgment,
  MisstatementDraft,
  MisstatementType,
  ProcedureRun,
  ReviewRecordIn,
  ReviewTaskView,
} from '@/client/types.gen'
import { audToCents } from '@/lib/money'
import type { Messages } from '@/locales/en'

// 复核任务的公共计算: 对象, 金额, 复核档位, 决定请求体. 对话和队列共用, 保证口径一致

export type ReviewTier = 'full_review' | 'check_evidence' | 'read_reasoning' | 'batch_eligible'
export const TIER_ORDER: ReviewTier[] = ['full_review', 'check_evidence', 'read_reasoning', 'batch_eligible']

// 后端 R3 上线后 ReviewTaskView 自带 amount_cents 与 tier; 之前按同一规则在前端计算
type WithTier = ReviewTaskView & { amount_cents?: number | null; tier?: ReviewTier }

export function isJudgment(target: ReviewTaskView['target']): target is Judgment {
  return 'conclusion' in target
}

export function isRun(target: ReviewTaskView['target']): target is ProcedureRun {
  return 'run_no' in target && !('conclusion' in target)
}

export function isException(target: ReviewTaskView['target']): target is AuditException {
  return 'rule_id' in target
}

export function taskSubject(view: ReviewTaskView): string {
  const target = view.target
  if (isJudgment(target)) return target.subject_id
  if (isException(target)) return view.task.subject_id ?? target.id
  return view.task.subject_id ?? view.task.procedure_id
}

export function taskAmount(view: ReviewTaskView, exceptions: Map<string, AuditException>): number | null {
  const own = (view as WithTier).amount_cents
  if (own !== undefined) return own
  const target = view.target
  if (isJudgment(target)) {
    if (target.misstatement_draft) return target.misstatement_draft.amount_cents ?? null
    const linked = (target.exception_ids ?? [])
      .map((id) => exceptions.get(id)?.amount_cents)
      .filter((value): value is number => typeof value === 'number')
    return linked.length ? Math.max(...linked) : null
  }
  if (isException(target)) return target.amount_cents ?? null
  return null
}

export function taskTier(
  view: ReviewTaskView,
  amount: number | null,
  overview: EngagementOverview | undefined,
): ReviewTier {
  const own = (view as WithTier).tier
  if (own) return own
  const target = view.target
  if (!isJudgment(target)) return 'check_evidence'
  const performance = overview?.performance_materiality_cents
  if (performance != null && amount != null && amount > performance) return 'full_review'
  if (target.confidence < 0.7) return 'check_evidence'
  if (target.confidence <= 0.9) return 'read_reasoning'
  if (target.autonomy_level === 'L1' || view.task.kind !== 'normal') return 'read_reasoning'
  const trivial = overview?.clearly_trivial_cents
  if (amount == null || (trivial != null && amount <= trivial)) return 'batch_eligible'
  return 'read_reasoning'
}

export type RankedTask = { view: ReviewTaskView; amount: number | null; tier: ReviewTier; subject: string }

// 先按档位, 同档按金额从大到小, 再按创建时间
export function rankTasks(
  views: ReviewTaskView[],
  exceptions: Map<string, AuditException>,
  overview: EngagementOverview | undefined,
): RankedTask[] {
  return views
    .map((view) => {
      const amount = taskAmount(view, exceptions)
      return { view, amount, tier: taskTier(view, amount, overview), subject: taskSubject(view) }
    })
    .sort(
      (a, b) =>
        TIER_ORDER.indexOf(a.tier) - TIER_ORDER.indexOf(b.tier) ||
        (b.amount ?? -1) - (a.amount ?? -1) ||
        a.view.task.created_at.localeCompare(b.view.task.created_at),
    )
}

export type ReviewDraft = {
  reason: string
  conclusion: string
  account: string
  assertion: Assertion
  amount: string
  direction: Direction
  kind: MisstatementType
  params: string
}

export function emptyDraft(): ReviewDraft {
  return {
    reason: '',
    conclusion: '',
    account: '',
    assertion: 'Cutoff',
    amount: '',
    direction: 'overstated',
    kind: 'factual',
    params: '',
  }
}

// 只有人点"复制建议数据"才填入, 不预填
export function draftFromJudgment(draft: ReviewDraft, judgment: Judgment): ReviewDraft {
  const proposed = judgment.misstatement_draft
  if (!proposed) return draft
  return {
    ...draft,
    account: proposed.account,
    assertion: proposed.assertion,
    amount: (proposed.amount_cents / 100).toFixed(2),
    direction: proposed.direction,
    kind: proposed.type,
  }
}

export function reasonRequired(decision: Decision | null): boolean {
  return decision != null && decision !== 'confirm'
}

export function buildRecord(
  view: ReviewTaskView,
  decision: Decision,
  input: ReviewDraft,
  t: Messages,
): ReviewRecordIn | string {
  const reason = input.reason.trim()
  const base: ReviewRecordIn = {
    task_id: view.task.id,
    target_version: view.task.target_version,
    decision,
    reason: reason || null,
  }
  if (reasonRequired(decision) && !reason) return t.errors.reasonRequired
  if (decision === 'override') {
    if (!input.conclusion.trim()) return t.errors.conclusionRequired
    return { ...base, override_conclusion: input.conclusion.trim() }
  }
  if (decision === 'manual_result') {
    if (!input.conclusion.trim()) return t.errors.conclusionRequired
    return { ...base, manual_result: { conclusion: input.conclusion.trim(), evidence: [] } }
  }
  if (decision === 'misstatement') {
    const draft = misstatementDraft(input, t)
    return typeof draft === 'string' ? draft : { ...base, misstatement_draft: draft }
  }
  if (decision === 'rerun' && input.params.trim()) {
    try {
      const parsed: unknown = JSON.parse(input.params)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return t.errors.rerunJson
      return { ...base, rerun_params: parsed as Record<string, unknown> }
    } catch {
      return t.errors.rerunJson
    }
  }
  return base
}

// 表单里的错报字段 → 错报草稿; 科目或金额缺失时返回错误文案
export function misstatementDraft(input: ReviewDraft, t: Messages): MisstatementDraft | string {
  const cents = audToCents(input.amount)
  if (cents == null || !input.account.trim()) return t.errors.misstatementRequired
  return {
    type: input.kind,
    account: input.account.trim(),
    assertion: input.assertion,
    amount_cents: cents,
    direction: input.direction,
  }
}

export function procedureName(t: Messages, id: string, fallback?: string): string {
  return t.procedureNames[id] ?? fallback ?? id
}
