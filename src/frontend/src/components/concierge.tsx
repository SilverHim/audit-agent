import { useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from 'cn'
import { MessageCircle } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import { toast } from 'sonner'
import { reviewsSubmitReviewRecordMutation } from '@/client/@tanstack/react-query.gen'
import type {
  Assertion,
  Decision,
  Direction,
  Judgment,
  MisstatementType,
  ReasoningStep,
  ReviewRecordIn,
  ReviewTaskView,
} from '@/client/types.gen'
import { Code, SectionLabel, StatusPill } from '@/components/chrome'
import { PointerList } from '@/components/evidence-link'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import {
  buildRecord,
  draftFromJudgment,
  emptyDraft,
  isException,
  isJudgment,
  isRun,
  procedureName,
  type RankedTask,
  type ReviewDraft,
  reasonRequired,
} from '@/lib/review'

// 对话流的组成部件: 消息, 判断卡片, 人工决定卡片

export function ConciergeAvatar() {
  return (
    <span className="flex size-[30px] shrink-0 items-center justify-center rounded-[9px] bg-primary">
      <MessageCircle className="size-4 text-white" aria-hidden="true" />
    </span>
  )
}

export function ConciergeMessage({ children }: { children: ReactNode }) {
  const { t } = useI18n()
  return (
    <div className="flex items-start gap-3">
      <ConciergeAvatar />
      <div className="flex min-w-0 flex-1 flex-col gap-2.5">
        <div className="font-semibold text-[13px]">{t.workspace.title}</div>
        {children}
      </div>
    </div>
  )
}

export function ConciergeText({ children }: { children: ReactNode }) {
  return <p className="m-0 whitespace-pre-wrap text-[15px] leading-[1.7]">{children}</p>
}

export function UserBubble({ title, children }: { title?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex justify-end">
      <div className="flex max-w-[600px] flex-col gap-1 rounded-[14px_14px_4px_14px] border border-input bg-card px-3.5 py-3">
        {title ? <span className="font-semibold text-[12px] text-ok tracking-wide">{title}</span> : null}
        {children}
      </div>
    </div>
  )
}

export function QuestionBubble({ children }: { children: ReactNode }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[560px] whitespace-pre-wrap rounded-[14px_14px_4px_14px] bg-[#e8ebf1] px-3.5 py-2.5 text-[15px]">
        {children}
      </div>
    </div>
  )
}

export function EventLine({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-3 text-[12.5px] text-muted-foreground">
      <span className="h-px min-w-6 flex-1 bg-border" />
      <span className="text-center">{children}</span>
      <span className="h-px min-w-6 flex-1 bg-border" />
    </div>
  )
}

const STEP_TONES: Record<string, string> = {
  fact: 'bg-muted text-secondary-foreground',
  standard: 'bg-standard-soft text-standard',
  inference: 'bg-accent text-accent-foreground',
  assumption: 'bg-warn-soft text-warn',
  limitation: 'bg-risk-soft text-risk',
}

function ReasoningChain({ eid, steps }: { eid: string; steps: ReasoningStep[] }) {
  const { t } = useI18n()
  return (
    <ol className="m-0 flex list-none flex-col p-0">
      {steps.map((step, index) => (
        <li key={`${step.kind}:${step.text}`} className="flex gap-3">
          <div className="flex shrink-0 flex-col items-center">
            <span className="flex size-6 items-center justify-center rounded-full border border-input bg-card font-mono text-[11.5px] text-secondary-foreground">
              {index + 1}
            </span>
            {index < steps.length - 1 ? <span className="min-h-3 w-px flex-1 bg-[#dce0e6]" /> : null}
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-1.5 pb-3.5">
            <span
              className={cn(
                'self-start rounded-[5px] px-1.5 py-px font-semibold text-[11px] tracking-wide',
                STEP_TONES[step.kind] ?? STEP_TONES.fact,
              )}
            >
              {t.stepKinds[step.kind]}
            </span>
            <span className="text-[14px] leading-[1.65]" lang="en">
              {step.text}
            </span>
            {step.from_steps?.length ? (
              <span className="text-[12.5px] text-muted-foreground">
                {t.judgment.fromSteps(step.from_steps.join(', '))}
              </span>
            ) : null}
            <PointerList eid={eid} pointers={step.evidence} />
          </div>
        </li>
      ))}
    </ol>
  )
}

// 后端 R9 上线后判断带独立复核字段; 之前不显示这一栏
type Critic = {
  critic_verdict?: string
  critic_reason_type?: string | null
  critic_reason?: string | null
  critic_conclusion?: string | null
}

function CriticPanel({ judgment }: { judgment: Judgment }) {
  const { t } = useI18n()
  const critic = judgment as Judgment & Critic
  if (!critic.critic_verdict) return null
  const verdict = critic.critic_verdict
  const tone = verdict === 'consistent' ? 'ok' : verdict === 'inconsistent' ? 'risk' : 'neutral'
  return (
    <div className="flex flex-1 basis-[240px] flex-col gap-1.5 border-line-soft p-4 sm:border-l">
      <SectionLabel>{t.judgment.critic.title}</SectionLabel>
      <StatusPill tone={tone} className="self-start">
        {t.judgment.critic.verdicts[verdict] ?? verdict}
      </StatusPill>
      {critic.critic_reason_type ? (
        <span className="text-[13px] text-secondary-foreground">
          {t.judgment.critic.reasonTypes[critic.critic_reason_type] ?? critic.critic_reason_type}
        </span>
      ) : null}
      {critic.critic_reason ? (
        <p className="m-0 text-[14px] text-secondary-foreground" lang="en">
          {critic.critic_reason}
        </p>
      ) : null}
      {critic.critic_conclusion ? (
        <div className="flex flex-col gap-1 pt-1">
          <span className="text-[12px] text-muted-foreground">{t.judgment.critic.ownConclusion}</span>
          <p className="m-0 text-[14px]" lang="en">
            {critic.critic_conclusion}
          </p>
        </div>
      ) : null}
    </div>
  )
}

export function JudgmentCard({ eid, ranked }: { eid: string; ranked: RankedTask }) {
  const { t } = useI18n()
  const { view, amount, tier, subject } = ranked
  const target = view.target
  const proc = procedureName(t, view.task.procedure_id, view.procedure_name)
  const header = (
    <div className="flex flex-wrap items-center gap-2.5 border-line-soft border-b px-4 py-3.5">
      <Code className="rounded-md bg-muted px-1.5 py-0.5 font-semibold text-[12px] text-secondary-foreground">
        {view.task.procedure_id}
      </Code>
      <span className="font-semibold">{proc}</span>
      <span className="flex-1" />
      <StatusPill tone={tier === 'full_review' ? 'risk' : tier === 'check_evidence' ? 'warn' : 'accent'}>
        {t.tiers[tier]}
        {tier === 'full_review' && amount != null ? ` · ${t.tierRules.full_review}` : ''}
      </StatusPill>
    </div>
  )

  if (!isJudgment(target)) {
    return (
      <article className="overflow-hidden rounded-[14px] border bg-card">
        {header}
        <div className="flex flex-col gap-2 p-4 text-[14px]">
          {isRun(target) ? (
            <>
              <span className="font-semibold">{t.judgment.runFailed(target.run_no)}</span>
              <span lang="en">{target.failure_reason || t.judgment.noReason}</span>
            </>
          ) : isException(target) ? (
            <>
              <span className="font-semibold">
                {t.judgment.exception} <Code>{target.id}</Code> · {formatAud(target.amount_cents)}
              </span>
              <PointerList eid={eid} pointers={target.evidence} />
            </>
          ) : null}
        </div>
      </article>
    )
  }

  const parts = Object.entries(target.confidence_parts)
    .map(([key, value]) => `${t.judgment.parts[key] ?? key} ${value.toFixed(2)}`)
    .join(' · ')
  return (
    <article className="overflow-hidden rounded-[14px] border bg-card">
      {header}
      <div className="flex flex-wrap gap-5 border-line-soft border-b px-4 py-4">
        <div className="flex flex-1 basis-[200px] flex-col gap-0.5">
          <span className="text-[12px] text-muted-foreground">{t.judgment.subject}</span>
          <Code className="font-semibold">{subject}</Code>
        </div>
        <div className="flex basis-[170px] flex-col gap-0.5">
          <span className="text-[12px] text-muted-foreground">{t.judgment.amount}</span>
          <Code className="font-semibold text-[16px]">{formatAud(amount)}</Code>
        </div>
        <div className="flex basis-[140px] flex-col gap-0.5">
          <span className="text-[12px] text-muted-foreground">{t.judgment.proposed}</span>
          <span className={cn('font-semibold', target.proposed_action === 'misstatement' && 'text-risk')}>
            {t.proposed[target.proposed_action]}
          </span>
        </div>
        <div className="flex flex-1 basis-[200px] flex-col gap-1.5">
          <span className="text-[12px] text-muted-foreground">{t.judgment.confidence}</span>
          <div className="flex items-center gap-2.5">
            <span className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
              <span className="bg-primary" style={{ width: `${Math.round(target.confidence * 100)}%` }} />
            </span>
            <Code className="font-semibold">{target.confidence.toFixed(2)}</Code>
          </div>
          <Code className="text-[11.5px] text-muted-foreground">{parts}</Code>
        </div>
      </div>
      <div className="flex flex-col gap-1.5 border-line-soft border-b px-4 py-4">
        <SectionLabel>{t.judgment.conclusion}</SectionLabel>
        <p className="m-0 text-[15px] leading-[1.65]" lang="en">
          {target.conclusion}
        </p>
      </div>
      <div className="flex flex-col gap-2.5 border-line-soft border-b px-4 py-4">
        <SectionLabel>{t.judgment.reasoning(target.reasoning_chain.length)}</SectionLabel>
        <ReasoningChain eid={eid} steps={target.reasoning_chain} />
      </div>
      <div className="flex flex-wrap">
        <div className="flex flex-1 basis-[300px] flex-col gap-1.5 bg-counter p-4">
          <SectionLabel className="text-warn">{t.judgment.counter}</SectionLabel>
          <p className="m-0 text-[14px] leading-[1.6]" lang="en">
            {target.counter_argument}
          </p>
        </div>
        <CriticPanel judgment={target} />
      </div>
    </article>
  )
}

export function OptionCard({
  name,
  value,
  checked,
  onSelect,
  label,
  hint,
  disabled,
}: {
  name: string
  value: string
  checked: boolean
  onSelect: () => void
  label: string
  hint?: string
  disabled?: boolean
}) {
  const id = `${name}-${value}`
  return (
    <label
      htmlFor={id}
      className={cn(
        'flex min-h-11 flex-1 basis-[180px] cursor-pointer items-start gap-2.5 rounded-[10px] border-[1.5px] px-3.5 py-3',
        checked ? 'border-primary bg-accent' : 'border-[#dce0e6] bg-card',
        disabled && 'cursor-not-allowed opacity-60',
      )}
    >
      <input
        id={id}
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
        className="mt-1 size-4 accent-primary"
      />
      <span className="flex flex-col gap-0.5">
        <span className="font-semibold">{label}</span>
        {hint ? <span className="text-[12.5px] text-[#4a5262]">{hint}</span> : null}
      </span>
    </label>
  )
}

export function DecisionFrame({
  title,
  note,
  children,
  footer,
}: {
  title: ReactNode
  note?: ReactNode
  children: ReactNode
  footer: ReactNode
}) {
  const { t } = useI18n()
  return (
    <section className="flex flex-col gap-3.5 rounded-[14px] border-[1.5px] border-primary bg-card px-5 py-4.5 shadow-xs sm:ml-[42px]">
      <div className="flex flex-col gap-1">
        <span className="font-semibold text-[12px] text-accent-foreground tracking-wide">{t.card.decision}</span>
        <h2 className="m-0 font-semibold text-[17px] tracking-tight">{title}</h2>
        {note ? <span className="text-[13px] text-muted-foreground">{note}</span> : null}
      </div>
      {children}
      <div className="flex flex-wrap items-center justify-between gap-2.5">{footer}</div>
    </section>
  )
}

export function ReasonField({
  id,
  value,
  onChange,
  required,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  required: boolean
}) {
  const { t } = useI18n()
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="font-semibold text-[13px]">
        {t.card.reason}{' '}
        <span className="font-normal text-muted-foreground">
          {required ? t.card.reasonRequired : t.card.reasonOptional}
        </span>
      </label>
      <Textarea
        id={id}
        rows={3}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="text-[14px]"
      />
    </div>
  )
}

const ASSERTIONS: Assertion[] = ['Occurrence', 'Completeness', 'Accuracy', 'Cutoff', 'Existence', 'Valuation', 'Rights']
const MISSTATEMENT_TYPES: MisstatementType[] = ['factual', 'judgmental', 'projected']
const DIRECTIONS: Direction[] = ['overstated', 'understated']
const SELECT = 'h-10 rounded-lg border border-input bg-card px-2.5 text-[14px]'

type DisputeChoice = 'a6_upheld' | 'a7_upheld' | 'neither'

export function ReviewDecisionCard({
  eid,
  ranked,
  disabled,
  deferred,
  onDefer,
  deferring,
}: {
  eid: string
  ranked: RankedTask
  disabled: boolean
  deferred: boolean
  onDefer: () => void
  deferring: boolean
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const view: ReviewTaskView = ranked.view
  const judgment = isJudgment(view.target) ? view.target : null
  const [decision, setDecision] = useState<Decision | null>(null)
  const [draft, setDraft] = useState<ReviewDraft>(emptyDraft)
  const [dispute, setDispute] = useState<DisputeChoice | null>(null)
  const set = (patch: Partial<ReviewDraft>) => setDraft((current) => ({ ...current, ...patch }))
  const isDispute = (decision as string | null) === 'dispute_resolution'

  const submit = useMutation({
    ...reviewsSubmitReviewRecordMutation(),
    onSuccess: async () => {
      toast.success(t.toasts.decisionRecorded)
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  const record = () => {
    if (!decision) return
    if (isDispute) {
      if (!draft.reason.trim()) return toast.error(t.errors.reasonRequired)
      if (dispute === 'neither' && !draft.conclusion.trim()) return toast.error(t.errors.conclusionRequired)
      const body = {
        task_id: view.task.id,
        target_version: view.task.target_version,
        decision,
        reason: draft.reason.trim(),
        dispute_resolution: dispute,
        neither_action: dispute === 'neither' ? 'human_conclusion' : null,
        override_conclusion: dispute === 'neither' ? draft.conclusion.trim() : null,
      } as unknown as ReviewRecordIn
      submit.mutate({ path: { eid }, body })
      return
    }
    const body = buildRecord(view, decision, draft, t)
    if (typeof body === 'string') return toast.error(body)
    submit.mutate({ path: { eid }, body })
  }

  const busy = disabled || submit.isPending
  const needsConclusion =
    decision === 'override' || decision === 'manual_result' || (isDispute && dispute === 'neither')
  const ready =
    Boolean(decision) &&
    (!reasonRequired(decision) || draft.reason.trim().length > 0) &&
    (!isDispute || dispute != null) &&
    (!needsConclusion || draft.conclusion.trim().length > 0) &&
    (decision !== 'misstatement' || (draft.account.trim().length > 0 && draft.amount.trim().length > 0))

  return (
    <DecisionFrame
      title={t.card.reviewQuestion(ranked.subject)}
      note={deferred ? t.workspace.deferredNote : undefined}
      footer={
        <>
          {deferred ? (
            <span />
          ) : (
            <Button type="button" variant="outline" size="lg" disabled={busy || deferring} onClick={onDefer}>
              {t.card.decideLater}
            </Button>
          )}
          <Button type="button" size="lg" disabled={busy || !ready} onClick={record}>
            {t.card.record}
          </Button>
        </>
      }
    >
      <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
        <legend className="sr-only">{t.card.decision}</legend>
        {view.allowed_decisions.map((item) => (
          <OptionCard
            key={item}
            name={`decision-${view.task.id}`}
            value={item}
            checked={decision === item}
            onSelect={() => setDecision(item)}
            label={t.decisions[item] ?? item}
            hint={t.decisionHints[item]}
            disabled={busy}
          />
        ))}
      </fieldset>

      {isDispute ? (
        <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
          <legend className="sr-only">{t.decisions.dispute_resolution}</legend>
          {(['a6_upheld', 'a7_upheld', 'neither'] as const).map((choice) => (
            <OptionCard
              key={choice}
              name={`dispute-${view.task.id}`}
              value={choice}
              checked={dispute === choice}
              onSelect={() => setDispute(choice)}
              label={t.card.dispute[choice]}
              disabled={busy}
            />
          ))}
        </fieldset>
      ) : null}

      {needsConclusion ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`conclusion-${view.task.id}`} className="font-semibold text-[13px]">
            {t.card.conclusion} <span className="font-normal text-muted-foreground">{t.card.conclusionHint}</span>
          </label>
          <Textarea
            id={`conclusion-${view.task.id}`}
            rows={3}
            value={draft.conclusion}
            onChange={(event) => set({ conclusion: event.target.value })}
          />
        </div>
      ) : null}

      {decision === 'misstatement' ? (
        <div className="flex flex-col gap-2.5 rounded-[10px] border p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[13px]">{t.card.misstatement}</span>
            {judgment?.misstatement_draft ? (
              <Button type="button" variant="outline" onClick={() => setDraft(draftFromJudgment(draft, judgment))}>
                {t.card.copyDraft}
              </Button>
            ) : null}
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2.5">
            <Field id={`ms-type-${view.task.id}`} label={t.card.msFields.type}>
              <select
                id={`ms-type-${view.task.id}`}
                className={SELECT}
                value={draft.kind}
                onChange={(event) => set({ kind: event.target.value as MisstatementType })}
              >
                {MISSTATEMENT_TYPES.map((item) => (
                  <option key={item} value={item}>
                    {t.misstatementTypes[item]}
                  </option>
                ))}
              </select>
            </Field>
            <Field id={`ms-account-${view.task.id}`} label={t.card.msFields.account}>
              <Input
                id={`ms-account-${view.task.id}`}
                className="h-10"
                value={draft.account}
                onChange={(event) => set({ account: event.target.value })}
              />
            </Field>
            <Field id={`ms-assertion-${view.task.id}`} label={t.card.msFields.assertion}>
              <select
                id={`ms-assertion-${view.task.id}`}
                className={SELECT}
                value={draft.assertion}
                onChange={(event) => set({ assertion: event.target.value as Assertion })}
              >
                {ASSERTIONS.map((item) => (
                  <option key={item} value={item}>
                    {t.assertions[item]}
                  </option>
                ))}
              </select>
            </Field>
            <Field id={`ms-amount-${view.task.id}`} label={`${t.judgment.amount} (AUD)`}>
              <Input
                id={`ms-amount-${view.task.id}`}
                className="h-10 font-mono"
                inputMode="decimal"
                value={draft.amount}
                onChange={(event) => set({ amount: event.target.value })}
              />
            </Field>
            <Field id={`ms-direction-${view.task.id}`} label={t.card.msFields.direction}>
              <select
                id={`ms-direction-${view.task.id}`}
                className={SELECT}
                value={draft.direction}
                onChange={(event) => set({ direction: event.target.value as Direction })}
              >
                {DIRECTIONS.map((item) => (
                  <option key={item} value={item}>
                    {t.directions[item]}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </div>
      ) : null}

      {decision === 'rerun' ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`params-${view.task.id}`} className="font-semibold text-[13px]">
            {t.card.rerunParams}
          </label>
          <Textarea
            id={`params-${view.task.id}`}
            className="font-mono"
            value={draft.params}
            onChange={(event) => set({ params: event.target.value })}
          />
        </div>
      ) : null}

      <ReasonField
        id={`reason-${view.task.id}`}
        value={draft.reason}
        onChange={(reason) => set({ reason })}
        required={reasonRequired(decision) || isDispute}
      />
    </DecisionFrame>
  )
}

function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[12.5px] text-[#4a5262]">
        {label}
      </label>
      {children}
    </div>
  )
}
