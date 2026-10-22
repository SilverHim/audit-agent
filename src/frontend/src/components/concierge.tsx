import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cn } from 'cn'
import { MessageCircle } from 'lucide-react'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  dataTablePageOptions,
  reportsGetMisstatementsOptions,
  reviewsPresentReviewTaskMutation,
  reviewsSubmitReviewRecordMutation,
} from '@/client/@tanstack/react-query.gen'
import type {
  Assertion,
  Decision,
  Direction,
  Judgment,
  MisstatementDraft,
  MisstatementType,
  PresentedFailure,
  PresentedSide,
  ReasoningStep,
  ReviewRecordIn,
  ReviewTaskView,
  TaskPresentation,
} from '@/client/types.gen'
import { BusyNote } from '@/components/activity-bar'
import { Code, SectionLabel, StatusPill } from '@/components/chrome'
import { EvidenceLink, PointerList } from '@/components/evidence-link'
import { RejudgeNotice } from '@/components/evidence-requests'
import { ExceptionDetail } from '@/components/exception-detail'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { UpdateFields, UpdatePayload } from '@/components/update-fields'
import { CorrectionList, ProposalVerdict, VerdictBanner } from '@/components/verdict'
import { GeneratedLanguage, useContentLanguage } from '@/components/working-language'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import {
  accountOptions,
  emptyUpdate,
  misstatementUpdate,
  type UpdateDraft,
  updateBase,
  updateFromPayload,
  updateKindFor,
} from '@/lib/misstatements'
import {
  cardLayout,
  failureParts,
  needsPresentation,
  pointerKey,
  presentationFor,
  presentationKey,
  showSideProposals,
} from '@/lib/presentation'
import {
  attachRule,
  buildDisputeRecord,
  buildRecord,
  CHOICE_DECISIONS,
  criticReasonText,
  type DisputeSide,
  defaultChoice,
  draftFromMisstatement,
  emptyDraft,
  isException,
  isJudgment,
  isRun,
  PREFILLED,
  procedureName,
  type RankedTask,
  type ReviewChoice,
  type ReviewDraft,
  reviewChoices,
  type SideProposal,
  sideProposal,
} from '@/lib/review'
import { useOverview } from '@/lib/use-engagement'
import { useMoney } from '@/lib/use-money'
import { correctionEntries } from '@/lib/verdict'
import type { WorkingLanguage } from '@/lib/working-language'

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

// 推理链; lang 为生成它的那次执行的语言 (useContentLanguage, 需求 R340)
function ReasoningChain({ eid, steps, lang }: { eid: string; steps: ReasoningStep[]; lang: WorkingLanguage }) {
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
            <span className="text-[14px] leading-[1.65]" lang={lang}>
              {step.text}
            </span>
            {step.from_steps?.length ? (
              <span className="text-[12.5px] text-muted-foreground">
                {t.judgment.fromSteps(step.from_steps.map((index) => index + 1).join(', '))}
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

function CriticPanel({ judgment, lang }: { judgment: Judgment; lang: WorkingLanguage }) {
  const { t } = useI18n()
  const critic = judgment as Judgment & Critic
  if (!critic.critic_verdict) return null
  const verdict = critic.critic_verdict
  const reason = criticReasonText(judgment, t)
  const modelWritten = verdict === 'consistent' || verdict === 'inconsistent' // 理由由复核模型写 (执行记录的语言)
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
      {/* 跳过或没做成的原因按界面语言显示 (R309), 技术细节原样; 模型写的理由与旧数据照原文, 只有前者标执行的语言 */}
      {reason ? (
        <p className="m-0 text-[14px] text-secondary-foreground" lang={reason.raw && modelWritten ? lang : undefined}>
          {reason.text}
        </p>
      ) : null}
      {reason?.detail ? (
        <p className="m-0 break-words font-mono text-[12px] text-muted-foreground" data-testid="critic-reason-detail">
          {reason.detail}
        </p>
      ) : null}
      {critic.critic_conclusion ? (
        <div className="flex flex-col gap-1 pt-1">
          <span className="text-[12px] text-muted-foreground">{t.judgment.critic.ownConclusion}</span>
          <p className="m-0 text-[14px]" lang={lang}>
            {critic.critic_conclusion}
          </p>
        </div>
      ) : null}
    </div>
  )
}

// 判断呈递 (后端 R152, GEN-11): 卡片挂载或换一条任务即呈递, 同一条在同一次挂载里不再调 (lib/presentation 的
// needsPresentation: StrictMode 的第二次 effect, 轮询刷新与重新渲染都不算). 工作台在相关查询都返回后才确定当前事项
// (lib/workspace.workspaceReady), 不会先闪过一条复核任务. 返回这条任务的呈递视图; 还没回来或失败时为 undefined, 卡片照
// 复核队列的数据显示. 卡片的头部, 金额, 档位与决定卡取复核队列, 呈递只补争议两方, 证据并集与失败原因, 同一字段只取一处
function usePresentation(eid: string, taskId: string): TaskPresentation | undefined {
  const { mutate, data } = useMutation({
    ...reviewsPresentReviewTaskMutation(),
    onError: (error) => toast.error(apiMessage(error)),
  })
  const presented = useRef<string | null>(null)
  useEffect(() => {
    if (!needsPresentation(presented.current, eid, taskId)) return
    presented.current = presentationKey(eid, taskId)
    mutate({ path: { eid, task_id: taskId } })
  }, [eid, taskId, mutate])
  return presentationFor(data, taskId)
}

// 失败原因 (呈递的 failure, 显示哪几段见 lib/presentation.failureParts): 无归因的例外先给规则与明细, 再与带对象的
// 失败一样给调用次数, 没通过的校验与错误
function FailureDetail({ failure }: { failure: PresentedFailure | null | undefined }) {
  const { t } = useI18n()
  const p = t.judgment.presentation
  const parts = failureParts(failure)
  if (!parts) return null
  return (
    <div className="flex flex-col gap-1.5" data-testid="presented-failure">
      {parts.rule?.id ? <span>{p.rule(parts.rule.id)}</span> : null}
      {parts.rule ? <ExceptionDetail detail={parts.rule.detail} /> : null}
      {parts.call ? <span>{p.attempts(parts.call.attempts, p.levels[parts.call.level])}</span> : null}
      {parts.errors.length ? (
        <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-[13px] text-secondary-foreground" lang="en">
          {parts.errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

// 争议两方的一栏: 两栏同一样式, 不标哪方更有理. proposals 为真时另显示本方的建议动作与错报草稿 (见
// lib/presentation.showSideProposals: collab 打开且任务待决时决定卡已并排显示, 这里不重复)
function SideColumn({
  eid,
  side,
  which,
  proposals,
  lang,
}: {
  eid: string
  side: PresentedSide
  which: 'generator' | 'reviewer'
  proposals: boolean
  lang: WorkingLanguage
}) {
  const { formatMoney } = useMoney()
  const { t } = useI18n()
  const p = t.judgment.presentation
  const draft = side.misstatement_draft
  return (
    <section className="flex min-w-0 flex-col gap-3 p-4" data-testid={`presented-${which}`}>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <SectionLabel>{p[which]}</SectionLabel>
        {side.confidence != null ? (
          <Code className="text-[12.5px] text-muted-foreground">
            {t.judgment.confidence} {side.confidence.toFixed(2)}
          </Code>
        ) : null}
      </div>
      {proposals ? (
        <div className="flex flex-col gap-1" data-testid={`presented-${which}-proposal`}>
          <ProposalVerdict action={side.proposed_action} />
          {draft ? (
            <Code className="text-[12.5px] text-secondary-foreground">
              {draft.account} · {t.assertions[draft.assertion]} · {formatMoney(draft.amount_cents)} ·{' '}
              {t.directions[draft.direction]}
            </Code>
          ) : null}
          <CorrectionList entries={correctionEntries(draft)} />
        </div>
      ) : null}
      {side.conclusion ? (
        <p className="m-0 text-[15px] leading-[1.65]" lang={lang}>
          {side.conclusion}
        </p>
      ) : null}
      <div className="flex flex-col gap-2.5">
        <SectionLabel>{t.judgment.reasoning(side.reasoning_chain.length)}</SectionLabel>
        <ReasoningChain eid={eid} steps={side.reasoning_chain} lang={lang} />
      </div>
      {side.counter_argument ? (
        <div className="flex flex-col gap-1.5 rounded-[10px] bg-counter p-3">
          <SectionLabel className="text-warn">{t.judgment.counter}</SectionLabel>
          <p className="m-0 text-[14px] leading-[1.6]" lang={lang}>
            {side.counter_argument}
          </p>
        </div>
      ) : null}
      {side.reason_type || side.reason ? (
        <div className="flex flex-col gap-1.5 rounded-[10px] bg-muted p-3">
          {side.reason_type ? (
            <span className="font-semibold text-[13px]">
              {t.judgment.critic.reasonTypes[side.reason_type] ?? side.reason_type}
            </span>
          ) : null}
          {side.reason ? (
            <p className="m-0 text-[14px] leading-[1.6]" lang={lang}>
              {side.reason}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

// 争议任务: 两方左右并排 (窄屏依次堆叠), 两方证据的并集在下方居中, 每项标出由哪一方引用
function DisputeBody({
  eid,
  presentation,
  proposals,
  lang,
}: {
  eid: string
  presentation: TaskPresentation
  proposals: boolean
  lang: WorkingLanguage
}) {
  const { t } = useI18n()
  const p = t.judgment.presentation
  const { generator, reviewer } = presentation.sides ?? {}
  if (!generator || !reviewer) return null
  return (
    <>
      <div className="grid grid-cols-1 divide-y divide-line-soft border-line-soft border-b sm:grid-cols-2 sm:divide-x sm:divide-y-0">
        <SideColumn eid={eid} side={generator} which="generator" proposals={proposals} lang={lang} />
        <SideColumn eid={eid} side={reviewer} which="reviewer" proposals={proposals} lang={lang} />
      </div>
      <div className="flex flex-col items-center gap-2.5 px-4 py-4 text-center" data-testid="presented-evidence">
        <SectionLabel>{p.evidence}</SectionLabel>
        <ul className="m-0 flex list-none flex-wrap justify-center gap-2 p-0">
          {(presentation.evidence ?? []).map(({ pointer, origin }) => (
            <li key={pointerKey(pointer)} className="flex items-center gap-1.5 rounded-[10px] border px-2 py-1">
              <EvidenceLink eid={eid} pointer={pointer} />
              <span className="text-[12px] text-muted-foreground">{p.origins[origin]}</span>
            </li>
          ))}
        </ul>
      </div>
    </>
  )
}

export function JudgmentCard({ eid, ranked }: { eid: string; ranked: RankedTask }) {
  const { formatMoney } = useMoney()
  const { t } = useI18n()
  const { view, amount, tier, subject } = ranked
  const presentation = usePresentation(eid, view.task.id)
  const collab = useOverview(eid)?.features?.includes('collab') ?? false
  const layout = cardLayout(view, presentation)
  const target = view.target
  // 判断正文 (两方的结论, 推理, 复核理由) 的语言: 生成它的那次执行记录的语言 (需求 R340)
  const lang = useContentLanguage(eid, isJudgment(target) ? target.run_id : null)
  const proc = procedureName(t, view.task.procedure_id, view.procedure_name)
  const header = (
    <div className="flex flex-wrap items-center gap-2.5 border-line-soft border-b px-4 py-3.5">
      <Code className="rounded-md bg-muted px-1.5 py-0.5 font-semibold text-[12px] text-secondary-foreground">
        {view.task.procedure_id}
      </Code>
      <span className="font-semibold">{proc}</span>
      {isJudgment(target) ? <GeneratedLanguage eid={eid} runId={target.run_id} /> : null}
      <span className="flex-1" />
      <StatusPill tone={tier === 'full_review' ? 'risk' : tier === 'check_evidence' ? 'warn' : 'accent'}>
        {t.tiers[tier]}
        {tier === 'full_review' && amount != null ? ` · ${t.tierRules.full_review}` : ''}
      </StatusPill>
    </div>
  )

  if (layout === 'target' || !isJudgment(target)) {
    const failure = presentation?.failure
    // 带对象的失败: 执行记录本身是成功终态, 说的是这个对象没做成
    const subjectFailed = isRun(target) && view.task.subject_id
    return (
      <article className="overflow-hidden rounded-[14px] border bg-card">
        {header}
        <div className="flex flex-col gap-2 p-4 text-[14px]">
          {isRun(target) ? (
            subjectFailed ? (
              <span className="font-semibold">{t.judgment.presentation.subjectFailed(subject)}</span>
            ) : (
              <>
                <span className="font-semibold">{t.judgment.runFailed(target.run_no)}</span>
                <span lang="en">{target.failure_reason || t.judgment.noReason}</span>
              </>
            )
          ) : isException(target) ? (
            <>
              <span className="font-semibold">
                {t.judgment.exception} <Code>{target.id}</Code> · {formatMoney(target.amount_cents)}
              </span>
              <PointerList eid={eid} pointers={target.evidence} />
            </>
          ) : null}
          <FailureDetail failure={failure} />
        </div>
      </article>
    )
  }

  const facts = (
    <>
      <div className="flex flex-1 basis-[200px] flex-col gap-0.5">
        <span className="text-[12px] text-muted-foreground">{t.judgment.subject}</span>
        <Code className="font-semibold">{subject}</Code>
      </div>
      <div className="flex basis-[170px] flex-col gap-0.5">
        <span className="text-[12px] text-muted-foreground">{t.judgment.amount}</span>
        <Code className="font-semibold text-[16px]">{formatMoney(amount)}</Code>
      </div>
    </>
  )
  const corrections = correctionEntries(target.misstatement_draft)
  // 争议任务的呈递回来后两方并排: 结论, 推理链, 置信度与 (须时) 建议都从呈递取, 不再从队列的判断上重复显示
  if (layout === 'dispute' && presentation) {
    return (
      <article className="overflow-hidden rounded-[14px] border bg-card" data-testid="dispute-presentation">
        {header}
        <div className="flex flex-col gap-3 border-line-soft border-b px-4 py-4">
          <VerdictBanner judgment={target} />
          {/* 由补充证据请求重判的判断给出重判来源 (需求 R370) */}
          <RejudgeNotice eid={eid} judgmentId={target.id} />
          <div className="flex flex-wrap gap-5">{facts}</div>
        </div>
        <DisputeBody eid={eid} presentation={presentation} proposals={showSideProposals(collab, view)} lang={lang} />
      </article>
    )
  }

  const parts = Object.entries(target.confidence_parts)
    .map(([key, value]) => `${t.judgment.parts[key] ?? key} ${value.toFixed(2)}`)
    .join(' · ')
  return (
    <article className="overflow-hidden rounded-[14px] border bg-card">
      {header}
      <div className="flex flex-col gap-3 border-line-soft border-b px-4 py-4">
        {/* 定论放最上面: 大字号分颜色, 是错报的同一行给金额与方向 (需求 R368) */}
        <VerdictBanner judgment={target} />
        {/* 由补充证据请求重判的判断给出重判来源 (需求 R370) */}
        <RejudgeNotice eid={eid} judgmentId={target.id} />
        <div className="flex flex-wrap gap-5">
          {facts}
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
      </div>
      {corrections.length ? (
        <CorrectionList entries={corrections} className="border-line-soft border-b px-4 py-4" />
      ) : null}
      <div className="flex flex-col gap-1.5 border-line-soft border-b px-4 py-4">
        {/* AI 的叙述文字: 定论下面是理由 (需求 R368) */}
        <SectionLabel>{t.judgment.reason}</SectionLabel>
        <p className="m-0 text-[15px] leading-[1.65]" lang={lang}>
          {target.conclusion}
        </p>
      </div>
      {target.misstatement_update ? <UpdatePayload update={target.misstatement_update} /> : null}
      <div className="flex flex-col gap-2.5 border-line-soft border-b px-4 py-4">
        <SectionLabel>{t.judgment.reasoning(target.reasoning_chain.length)}</SectionLabel>
        <ReasoningChain eid={eid} steps={target.reasoning_chain} lang={lang} />
      </div>
      <div className="flex flex-wrap">
        <div className="flex flex-1 basis-[300px] flex-col gap-1.5 bg-counter p-4">
          <SectionLabel className="text-warn">{t.judgment.counter}</SectionLabel>
          <p className="m-0 text-[14px] leading-[1.6]" lang={lang}>
            {target.counter_argument}
          </p>
        </div>
        <CriticPanel judgment={target} lang={lang} />
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

// 决定卡片的框: 标题, 说明, 内容与按钮; 系统忙使按钮变灰时按钮下写明正在进行的活动 (需求 R362)
export function DecisionFrame({
  eid,
  title,
  note,
  children,
  footer,
}: {
  eid: string
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
      <BusyNote eid={eid} className="self-end text-right" />
    </section>
  )
}

// 决定的理由一律选填 (需求 R360)
export function ReasonField({ id, value, onChange }: { id: string; value: string; onChange: (value: string) => void }) {
  const { t } = useI18n()
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="font-semibold text-[13px]">
        {t.reason.label}
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
const DISPUTE_CHOICES: DisputeChoice[] = ['a6_upheld', 'a7_upheld', 'neither']
const SIDES: DisputeSide[] = ['a6', 'a7']

// 错报框上 "复制建议数据" 的来源: 功能 collab 打开的仲裁按采纳的一方 (neither 时两方都给), 其余为原判断的草稿
function copySources(
  judgment: Judgment | null,
  dispute: DisputeChoice | null,
  collabDispute: boolean,
): { side: DisputeSide; source: MisstatementDraft }[] {
  const sides: DisputeSide[] = !collabDispute
    ? ['a6']
    : dispute === 'a6_upheld'
      ? ['a6']
      : dispute === 'a7_upheld'
        ? ['a7']
        : SIDES
  return sides.flatMap((side) => {
    const source = sideProposal(judgment, side).draft
    return source ? [{ side, source }] : []
  })
}

// 争议卡上一方的建议动作与错报草稿 (功能 collab)
function SideCard({ side, proposal }: { side: DisputeSide; proposal: SideProposal }) {
  const { formatMoney } = useMoney()
  const { t } = useI18n()
  const { action, draft } = proposal
  return (
    <div className="flex flex-col gap-1 rounded-[10px] border px-3.5 py-3" data-testid={`dispute-side-${side}`}>
      <span className="text-[12px] text-muted-foreground">{t.card.sides[side]}</span>
      <ProposalVerdict action={action} />
      {draft ? (
        <Code className="text-[12.5px] text-secondary-foreground">
          {draft.account} · {t.assertions[draft.assertion]} · {formatMoney(draft.amount_cents)} ·{' '}
          {t.directions[draft.direction]}
        </Code>
      ) : null}
      <CorrectionList entries={correctionEntries(draft)} />
    </div>
  )
}

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
  const { currency } = useMoney()
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const view: ReviewTaskView = ranked.view
  const judgment = isJudgment(view.target) ? view.target : null
  const proposedDraft = judgment?.misstatement_draft ?? null
  // 判断的复核选项按 AI 结论给 (需求 R364): AI 判为错报时默认 "同意, 认定为错报", 错报字段按草稿带出; 其余任务照
  // 后端的 allowed_decisions, 只有一种决定可选 (如争议任务只能裁决分歧) 时直接选中
  const choices = reviewChoices(view)
  const [choice, setChoice] = useState<ReviewChoice | null>(() => defaultChoice(view, choices))
  const [picked, setPicked] = useState<Decision | null>(
    view.allowed_decisions.length === 1 ? view.allowed_decisions[0] : null,
  )
  const decision = choices ? (choice ? CHOICE_DECISIONS[choice] : null) : picked
  const [draft, setDraft] = useState<ReviewDraft>(() =>
    choice && PREFILLED.has(choice) && proposedDraft
      ? draftFromMisstatement(emptyDraft(), proposedDraft)
      : emptyDraft(),
  )
  // 人给的错报更新载荷 (需求 R159, R163): 以判断带的 AI 载荷为起点, 可改
  const carried = judgment?.misstatement_update ?? null
  const [updateDraft, setUpdateDraft] = useState<UpdateDraft>(() =>
    carried ? updateFromPayload(carried) : emptyUpdate(),
  )
  const [dispute, setDispute] = useState<DisputeChoice | null>(null)
  const [withMisstatement, setWithMisstatement] = useState(false)
  const [consulted, setConsulted] = useState(false)
  const [consultation, setConsultation] = useState('')
  // 项目开了智能体协作 (功能 collab) 时争议卡并排显示两方的建议, 另可记系统外咨询; 关闭时照原样.
  // "同时登记错报" 一项只按后端给的 draft_rules (attachRule), 不看 collab
  const collab = useOverview(eid)?.features?.includes('collab') ?? false
  // 后端给出可选的裁决; A7 推理链没通过校验时不能采用独立复核的结论
  const allowedResolutions = new Set<DisputeChoice>(view.allowed_resolutions ?? DISPUTE_CHOICES)
  const set = (patch: Partial<ReviewDraft>) => setDraft((current) => ({ ...current, ...patch }))
  // 改了错报字段的 "同意" 即 "修改后认定为错报"; AI 建议的更正分录按原金额与方向校验, 改过数据后不再带, 重新
  // "复制建议数据" 会再带出 (需求 R367, R368)
  const setFigures = (patch: Partial<ReviewDraft>) => {
    setDraft((current) => ({ ...current, ...patch, correction: [] }))
    if (choice === 'agree_misstatement') setChoice('amend_misstatement')
  }
  // 选 "同意" 时错报字段回到 AI 草稿; 选 "不是错报" (推翻) 时结论为空则预填一句, 可改
  const pick = (next: ReviewChoice) => {
    setChoice(next)
    if (next === 'agree_misstatement' && proposedDraft)
      setDraft((current) => draftFromMisstatement(current, proposedDraft))
    if (CHOICE_DECISIONS[next] === 'override')
      setDraft((current) =>
        current.conclusion.trim() ? current : { ...current, conclusion: t.card.notMisstatementConclusion },
      )
  }
  const updateKind = updateKindFor(view, decision)
  // 失败与无归因任务的载荷依据错报的当前版本, 从错报列表取; 判断带载荷时不用
  const misstatements = useQuery({
    ...reportsGetMisstatementsOptions({ path: { eid } }),
    enabled: Boolean(updateKind) && !carried,
  })
  const isDispute = (decision as string | null) === 'dispute_resolution'
  const collabDispute = isDispute && collab
  const rule = isDispute ? attachRule(view, dispute) : 'optional'
  const attaching = rule === 'required' || (rule === 'optional' && withMisstatement)

  const choose = (choice: DisputeChoice) => {
    setDispute(choice)
    // 采纳的一方建议错报: 草稿框按该方的草稿预填, 可改
    if (attachRule(view, choice) !== 'required') return
    const proposed = sideProposal(judgment, choice === 'a6_upheld' ? 'a6' : 'a7').draft
    if (proposed) setDraft((current) => draftFromMisstatement(current, proposed))
  }

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
      if (!dispute) return
      const input = { choice: dispute, attach: withMisstatement, consulted, consultation, collab }
      const body: ReviewRecordIn | string = buildDisputeRecord(view, draft, input, t)
      if (typeof body === 'string') return toast.error(body)
      submit.mutate({ path: { eid }, body })
      return
    }
    const body = buildRecord(view, decision, draft, t)
    if (typeof body === 'string') return toast.error(body)
    if (!updateKind) return submit.mutate({ path: { eid }, body })
    const base = updateBase(view, misstatements.data?.items ?? [])
    const update = misstatementUpdate(updateKind, base, updateDraft, t)
    if (typeof update === 'string') return toast.error(update)
    submit.mutate({ path: { eid }, body: { ...body, misstatement_update: update } })
  }

  const busy = disabled || submit.isPending
  const showsMisstatement = decision === 'misstatement' || (isDispute && attaching)
  // 科目代码从本项目科目表选 (复用账表接口; 认定错报时才取)
  const accounts = useQuery({
    ...dataTablePageOptions({ path: { eid, kind: 'accounts' }, query: { limit: 200 } }),
    enabled: showsMisstatement,
  })
  const codes = accountOptions(accounts.data?.rows ?? [], draft.accountCode)
  const copies = showsMisstatement ? copySources(judgment, dispute, collabDispute) : []
  const needsConclusion =
    decision === 'override' || decision === 'manual_result' || (isDispute && dispute === 'neither')
  const ready =
    Boolean(decision) &&
    (!isDispute || dispute != null) &&
    (!needsConclusion || draft.conclusion.trim().length > 0) &&
    (!showsMisstatement || (draft.account.trim().length > 0 && draft.amount.trim().length > 0)) &&
    (!collabDispute || !consulted || consultation.trim().length > 0)

  return (
    <DecisionFrame
      eid={eid}
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
      {/* 复核卡也把 AI 的定论放最上面 (需求 R368): 是错报的同一行给金额与方向, 下面列出建议的更正分录 */}
      {judgment ? <VerdictBanner judgment={judgment} /> : null}
      {judgment ? <CorrectionList entries={correctionEntries(judgment.misstatement_draft)} /> : null}

      <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
        <legend className="sr-only">{t.card.decision}</legend>
        {choices
          ? choices.map((item) => (
              <OptionCard
                key={item}
                name={`decision-${view.task.id}`}
                value={item}
                checked={choice === item}
                onSelect={() => pick(item)}
                label={t.card.choices[item].label}
                hint={t.card.choices[item].hint}
                disabled={busy}
              />
            ))
          : view.allowed_decisions.map((item) => (
              <OptionCard
                key={item}
                name={`decision-${view.task.id}`}
                value={item}
                checked={picked === item}
                onSelect={() => setPicked(item)}
                label={t.decisions[item] ?? item}
                hint={t.decisionHints[item]}
                disabled={busy}
              />
            ))}
      </fieldset>

      {collabDispute ? (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>{t.card.sides.title}</SectionLabel>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-2.5">
            {SIDES.map((side) => (
              <SideCard key={side} side={side} proposal={sideProposal(judgment, side)} />
            ))}
          </div>
        </div>
      ) : null}

      {isDispute ? (
        <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
          <legend className="sr-only">{t.decisions.dispute_resolution}</legend>
          {DISPUTE_CHOICES.map((choice) => (
            <OptionCard
              key={choice}
              name={`dispute-${view.task.id}`}
              value={choice}
              checked={dispute === choice}
              onSelect={() => choose(choice)}
              label={t.card.dispute[choice]}
              hint={allowedResolutions.has(choice) ? undefined : t.card.disputeUnavailable}
              disabled={busy || !allowedResolutions.has(choice)}
            />
          ))}
        </fieldset>
      ) : null}

      {isDispute && rule !== 'hidden' ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2 text-[13.5px]">
            <input
              id={`attach-ms-${view.task.id}`}
              type="checkbox"
              className="size-4 accent-primary"
              checked={attaching}
              disabled={busy || rule === 'required'}
              aria-describedby={rule === 'required' ? `attach-ms-note-${view.task.id}` : undefined}
              onChange={(event) => setWithMisstatement(event.target.checked)}
            />
            <label htmlFor={`attach-ms-${view.task.id}`}>{t.card.attachMisstatement}</label>
          </div>
          {rule === 'required' ? (
            <span id={`attach-ms-note-${view.task.id}`} className="text-[12.5px] text-muted-foreground">
              {t.card.attachRequired}
            </span>
          ) : null}
        </div>
      ) : null}

      {needsConclusion ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`conclusion-${view.task.id}`} className="font-semibold text-[13px]">
            {t.card.conclusion}{' '}
            <span className="font-normal text-muted-foreground">
              {isDispute ? t.card.conclusionHintDispute : t.card.conclusionHint}
            </span>
          </label>
          <Textarea
            id={`conclusion-${view.task.id}`}
            rows={3}
            value={draft.conclusion}
            onChange={(event) => set({ conclusion: event.target.value })}
          />
        </div>
      ) : null}

      {showsMisstatement ? (
        <div className="flex flex-col gap-2.5 rounded-[10px] border p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-[13px]">{t.card.misstatement}</span>
            <div className="flex flex-wrap gap-2">
              {copies.map(({ side, source }) => (
                <Button
                  key={side}
                  type="button"
                  variant="outline"
                  onClick={() => setDraft((current) => draftFromMisstatement(current, source))}
                >
                  {collabDispute ? t.card.copyDraftFrom[side] : t.card.copyDraft}
                </Button>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2.5">
            <Field id={`ms-type-${view.task.id}`} label={t.card.msFields.type}>
              <select
                id={`ms-type-${view.task.id}`}
                className={SELECT}
                value={draft.kind}
                onChange={(event) => setFigures({ kind: event.target.value as MisstatementType })}
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
                onChange={(event) => setFigures({ account: event.target.value })}
              />
            </Field>
            <Field id={`ms-code-${view.task.id}`} label={t.card.msFields.accountCode}>
              <select
                id={`ms-code-${view.task.id}`}
                className={SELECT}
                value={draft.accountCode}
                onChange={(event) => setFigures({ accountCode: event.target.value })}
              >
                <option value="">{t.card.msFields.noAccountCode}</option>
                {codes.map((item) => (
                  <option key={item.code} value={item.code}>
                    {item.name ? `${item.code} · ${item.name}` : item.code}
                  </option>
                ))}
              </select>
            </Field>
            <Field id={`ms-assertion-${view.task.id}`} label={t.card.msFields.assertion}>
              <select
                id={`ms-assertion-${view.task.id}`}
                className={SELECT}
                value={draft.assertion}
                onChange={(event) => setFigures({ assertion: event.target.value as Assertion })}
              >
                {ASSERTIONS.map((item) => (
                  <option key={item} value={item}>
                    {t.assertions[item]}
                  </option>
                ))}
              </select>
            </Field>
            <Field id={`ms-amount-${view.task.id}`} label={`${t.judgment.amount} (${currency})`}>
              <Input
                id={`ms-amount-${view.task.id}`}
                className="h-10 font-mono"
                inputMode="decimal"
                value={draft.amount}
                onChange={(event) => setFigures({ amount: event.target.value })}
              />
            </Field>
            <Field id={`ms-direction-${view.task.id}`} label={t.card.msFields.direction}>
              <select
                id={`ms-direction-${view.task.id}`}
                className={SELECT}
                value={draft.direction}
                onChange={(event) => setFigures({ direction: event.target.value as Direction })}
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

      {updateKind ? (
        <UpdateFields
          id={`update-${view.task.id}`}
          kind={updateKind}
          value={updateDraft}
          onChange={(patch) => setUpdateDraft((current) => ({ ...current, ...patch }))}
          disabled={busy}
        />
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

      {collabDispute ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2 text-[13.5px]">
            <input
              id={`consulted-${view.task.id}`}
              type="checkbox"
              className="size-4 accent-primary"
              checked={consulted}
              disabled={busy}
              onChange={(event) => setConsulted(event.target.checked)}
            />
            <label htmlFor={`consulted-${view.task.id}`}>{t.card.consulted}</label>
          </div>
          {consulted ? (
            <>
              <label htmlFor={`consultation-${view.task.id}`} className="font-semibold text-[13px]">
                {t.card.consultation}{' '}
                <span className="font-normal text-muted-foreground">{t.card.consultationHint}</span>
              </label>
              <Textarea
                id={`consultation-${view.task.id}`}
                rows={3}
                value={consultation}
                onChange={(event) => setConsultation(event.target.value)}
              />
            </>
          ) : null}
        </div>
      ) : null}

      <ReasonField id={`reason-${view.task.id}`} value={draft.reason} onChange={(reason) => set({ reason })} />
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
