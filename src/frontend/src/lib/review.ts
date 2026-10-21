import type {
  Assertion,
  AuditException,
  Decision,
  Direction,
  DisputeResolution,
  EngagementOverview,
  Judgment,
  MisstatementDraft,
  MisstatementType,
  ProcedureRun,
  ProposedAction,
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

// 批量确认只用于 AI 判为不是错报的判断 (需求 R364): 其余落在可批量一档的 (后端给的或这里算的) 改为阅读推理链
export function taskTier(
  view: ReviewTaskView,
  amount: number | null,
  overview: EngagementOverview | undefined,
): ReviewTier {
  const tier = (view as WithTier).tier ?? computedTier(view, amount, overview)
  return tier === 'batch_eligible' && aiVerdict(view) !== 'clear' ? 'read_reasoning' : tier
}

function computedTier(
  view: ReviewTaskView,
  amount: number | null,
  overview: EngagementOverview | undefined,
): ReviewTier {
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

// 人工程序的任务 (manual, 后端需求 R94) 由人在人工程序页执行并记录结果, 不按复核决定处理: 复核队列不列, 对话页与
// 待决页单列为指向人工程序页的入口 (需求 R303). 分成 (复核任务, 人工任务), 各自保持原顺序
export function splitManual(views: ReviewTaskView[]): [ReviewTaskView[], ReviewTaskView[]] {
  return [views.filter((view) => view.task.kind !== 'manual'), views.filter((view) => view.task.kind === 'manual')]
}

// 先按档位, 同档按金额从大到小, 再按创建时间. 项目开了智能体协作 (功能 collab, 后端 R151) 时待决的争议任务单列
// 在最前, 争议之间按创建时间, 不按置信度或金额; 其余顺序不变
export function rankTasks(
  views: ReviewTaskView[],
  exceptions: Map<string, AuditException>,
  overview: EngagementOverview | undefined,
): RankedTask[] {
  const disputesFirst = overview?.features?.includes('collab') ?? false
  const dispute = (item: RankedTask) =>
    disputesFirst && item.view.task.kind === 'dispute' && item.view.task.status === 'pending'
  const created = (a: RankedTask, b: RankedTask) => a.view.task.created_at.localeCompare(b.view.task.created_at)
  return views
    .map((view) => {
      const amount = taskAmount(view, exceptions)
      return { view, amount, tier: taskTier(view, amount, overview), subject: taskSubject(view) }
    })
    .sort((a, b) => {
      const first = Number(dispute(b)) - Number(dispute(a))
      if (first || (dispute(a) && dispute(b))) return first || created(a, b)
      return (
        TIER_ORDER.indexOf(a.tier) - TIER_ORDER.indexOf(b.tier) || (b.amount ?? -1) - (a.amount ?? -1) || created(a, b)
      )
    })
}

// AI 对判断对象的结论 (需求 R364, Issue #67): 判为错报 (proposed_action misstatement), 不是错报 (accept, close), 暂时
// 判断不了 (more_evidence). 复核卡片按它给选项; 不是判断, 不是 normal 任务, 或判断带错报更新载荷 (FIN-03 分类, FIN-09
// 验证, 对象是已有的错报) 时为 null, 卡片照后端的 allowed_decisions 逐项给
export type AiVerdict = 'misstatement' | 'clear' | 'undetermined'

export function aiVerdict(view: ReviewTaskView): AiVerdict | null {
  const target = view.target
  if (!isJudgment(target) || view.task.kind !== 'normal' || view.misstatement_update_kind) return null
  if (target.proposed_action === 'misstatement') return 'misstatement'
  return target.proposed_action === 'more_evidence' ? 'undetermined' : 'clear'
}

export type ReviewChoice =
  | 'agree_misstatement'
  | 'disagree_not_misstatement'
  | 'amend_misstatement'
  | 'agree_clear'
  | 'disagree_misstatement'
  | 'misstatement'
  | 'not_misstatement'

// 各选项提交的决定: 认定错报的带错报草稿; 与 AI 不同而 "不是错报" 的为推翻 (须写结论)
export const CHOICE_DECISIONS: Record<ReviewChoice, Decision> = {
  agree_misstatement: 'misstatement',
  disagree_not_misstatement: 'override',
  amend_misstatement: 'misstatement',
  agree_clear: 'confirm',
  disagree_misstatement: 'misstatement',
  misstatement: 'misstatement',
  not_misstatement: 'override',
}

// 按 AI 结论给的选项; 暂时判断不了时的 "要求补充证据" 待 R369
const CHOICES: Record<AiVerdict, ReviewChoice[]> = {
  misstatement: ['agree_misstatement', 'disagree_not_misstatement', 'amend_misstatement'],
  clear: ['agree_clear', 'disagree_misstatement'],
  undetermined: ['misstatement', 'not_misstatement'],
}

// 预填 AI 草稿的选项 (agree 为默认选中的那项)
export const PREFILLED: ReadonlySet<ReviewChoice> = new Set(['agree_misstatement', 'amend_misstatement'])

// 复核卡片的选项: 按 AI 结论, 只留后端允许的决定; aiVerdict 为 null 时为 null
export function reviewChoices(view: ReviewTaskView): ReviewChoice[] | null {
  const verdict = aiVerdict(view)
  if (!verdict) return null
  return CHOICES[verdict].filter((choice) => view.allowed_decisions.includes(CHOICE_DECISIONS[choice]))
}

// 默认选中的选项: AI 判为错报且有草稿时为 "同意, 认定为错报"; 其余不预选
export function defaultChoice(view: ReviewTaskView, choices: ReviewChoice[] | null): ReviewChoice | null {
  const draft = isJudgment(view.target) ? view.target.misstatement_draft : null
  return draft && choices?.includes('agree_misstatement') ? 'agree_misstatement' : null
}

export type ReviewDraft = {
  reason: string
  conclusion: string
  account: string
  accountCode: string // 科目代码 (从科目表选), 空串为不填 (需求 R163)
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
    accountCode: '',
    assertion: 'Cutoff',
    amount: '',
    direction: 'overstated',
    kind: 'factual',
    params: '',
  }
}

// 错报草稿填进表单, 科目代码一并带入 (需求 R163; 草稿没有时清空). 预填的只有: AI 判为错报时 "同意" 与 "修改后认定"
// 两项按判断的草稿 (需求 R364), 功能 collab 打开时仲裁采纳的一方建议错报 (见 attachRule); 其余由人点 "复制建议数据"
export function draftFromMisstatement(draft: ReviewDraft, proposed: MisstatementDraft): ReviewDraft {
  return {
    ...draft,
    account: proposed.account,
    accountCode: proposed.account_code ?? '',
    assertion: proposed.assertion,
    amount: (proposed.amount_cents / 100).toFixed(2),
    direction: proposed.direction,
    kind: proposed.type,
  }
}

// 独立复核的理由怎么显示 (后端 R309): 跳过或没做成的带 critic_reason_code, 按界面语言取文案, 没做成的另带技术细节
// (critic_reason_detail: 校验级别与错误, 异常信息, 原样显示). 没有代码的照 critic_reason 原文 (raw 为真): 一致或不一致
// 时理由由复核模型写; R309 之前存下的判断只有中文原句. 认不出的代码 (前端比后端旧) 也照原文
export type CriticReasonText = { text: string; detail: string | null; raw: boolean }

export function criticReasonText(judgment: Judgment, t: Messages): CriticReasonText | null {
  const code = judgment.critic_reason_code
  const coded = code ? t.judgment.critic.reasonCodes[code] : undefined
  if (coded) return { text: coded, detail: judgment.critic_reason_detail ?? null, raw: false }
  if (!judgment.critic_reason) return null
  return { text: judgment.critic_reason, detail: null, raw: true }
}

// 争议任务两方的建议 (后端 R150): a6 为原判断的建议动作与错报草稿, a7 为独立复核的 (critic_ 字段; 功能 collab
// 之前的判断没有, 为 null)
export type DisputeSide = 'a6' | 'a7'
export type SideProposal = { action: ProposedAction | null; draft: MisstatementDraft | null }

export function sideProposal(judgment: Judgment | null, side: DisputeSide): SideProposal {
  if (!judgment) return { action: null, draft: null }
  if (side === 'a6') return { action: judgment.proposed_action, draft: judgment.misstatement_draft ?? null }
  return { action: judgment.critic_proposed_action ?? null, draft: judgment.critic_misstatement_draft ?? null }
}

// 仲裁时 "同时登记错报" 这一项只按后端给的规则 (ReviewTaskView.draft_rules, 与后端校验 check_dispute_draft
// 同一函数): required 自动勾选, 不能取消; hidden 不显示, 不带草稿; optional 与没给规则 (如 collab 关闭) 两可
export type AttachRule = 'required' | 'hidden' | 'optional'

export function attachRule(view: ReviewTaskView, choice: DisputeResolution | null): AttachRule {
  return (choice ? view.draft_rules?.[choice] : undefined) ?? 'optional'
}

export type DisputeInput = {
  choice: DisputeResolution
  attach: boolean // 审计师勾了 "同时登记错报" (attachRule 为 optional 时才看)
  consulted: boolean
  consultation: string
  collab: boolean // 项目开了 collab: 才有咨询两项
}

// 仲裁的请求体; 缺必填时返回错误文案 (理由选填, 需求 R360). 错报草稿按 attachRule; collab 关闭时不带咨询字段
export function buildDisputeRecord(
  view: ReviewTaskView,
  input: ReviewDraft,
  dispute: DisputeInput,
  t: Messages,
): ReviewRecordIn | string {
  const reason = input.reason.trim()
  const conclusion = input.conclusion.trim()
  if (dispute.choice === 'neither' && !conclusion) return t.errors.conclusionRequired
  const rule = attachRule(view, dispute.choice)
  const attached = rule === 'required' || (rule === 'optional' && dispute.attach) ? misstatementDraft(input, t) : null
  if (typeof attached === 'string') return attached
  const consultation = dispute.consultation.trim()
  if (dispute.collab && dispute.consulted && !consultation) return t.errors.consultationRequired
  const body: ReviewRecordIn = {
    task_id: view.task.id,
    target_version: view.task.target_version,
    decision: 'dispute_resolution',
    reason,
    dispute_resolution: dispute.choice,
    neither_action: dispute.choice === 'neither' ? 'human_conclusion' : null,
    override_conclusion: dispute.choice === 'neither' ? conclusion : null,
    misstatement_draft: attached,
  }
  if (!dispute.collab) return body
  return { ...body, consulted: dispute.consulted, consultation_record: dispute.consulted ? consultation : null }
}

// 复核决定的请求体; 缺结论或错报字段, 重跑参数写不对时返回错误文案. 理由选填 (需求 R360), 空也照发空串
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
    reason,
  }
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

// 表单里的错报字段 → 错报草稿; 科目或金额缺失时返回错误文案. 科目代码可空 (空为不填; 后端查它在本项目科目表中)
export function misstatementDraft(input: ReviewDraft, t: Messages): MisstatementDraft | string {
  const cents = audToCents(input.amount)
  if (cents == null || !input.account.trim()) return t.errors.misstatementRequired
  return {
    type: input.kind,
    account: input.account.trim(),
    assertion: input.assertion,
    amount_cents: cents,
    direction: input.direction,
    account_code: input.accountCode || null,
  }
}

export function procedureName(t: Messages, id: string, fallback?: string): string {
  return t.procedureNames[id] ?? fallback ?? id
}

// 人工程序的说明 (需求 R357): 界面语言的 manualInstructions, 没有时为后端给的 manual.instructions
export function manualInstructions(t: Messages, procedureId: string, fallback?: string): string {
  return t.manualInstructions[procedureId] ?? fallback ?? ''
}
