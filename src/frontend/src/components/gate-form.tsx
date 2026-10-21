import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { type ReactNode, useEffect, useState } from 'react'
import { toast } from 'sonner'
import {
  gatesGetGateOptions,
  gatesSubmitGateDecisionMutation,
  gatesUpdateClientProfileMutation,
} from '@/client/@tanstack/react-query.gen'
import type {
  Annotation,
  AuditException,
  AuditPlan,
  ClientProfile,
  GateDecisionIn,
  GateEdit,
  GateVerdict,
  GateView,
  MaterialityCandidates,
  MaterialityChoice,
  MonthlyFigures,
  OpinionType,
  PlanConfirmation,
  ReportSection,
  RiskItem,
  RiskLevel,
} from '@/client/types.gen'
import { AbnLookupNote } from '@/components/abn-lookup-note'
import { BusyNote } from '@/components/activity-bar'
import { Code, SectionLabel, StatusPill } from '@/components/chrome'
import { DecisionFrame, OptionCard, ReasonField } from '@/components/concierge'
import { EvidenceLink, PointerList } from '@/components/evidence-link'
import { ExceptionDetail } from '@/components/exception-detail'
import { MisstatementList } from '@/components/misstatement-lifecycle'
import {
  CheckIcon,
  checkTone,
  draftFields,
  LabeledField,
  ProfileFields,
  profileDraft,
  SELECT,
} from '@/components/profile-fields'
import { QueryState } from '@/components/query-state'
import { FinStaleAlert } from '@/components/rerun'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useContentLanguage } from '@/components/working-language'
import { abnProblem, normalizeAbn } from '@/lib/abn'
import { requestTitle } from '@/lib/data-api'
import { decisionCardKey, invalidateEngagement } from '@/lib/engagement'
import { apiMessage, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { inapplicable, initialChoice, selectableCandidates } from '@/lib/materiality'
import { formatAud, formatPct } from '@/lib/money'
import { initialConfirmation, planApprovalBody, planDecisionErrorText } from '@/lib/plan'
import { reasonText } from '@/lib/reason'
import { reportPart } from '@/lib/report-sections'
import { procedureName } from '@/lib/review'
import { useOverview } from '@/lib/use-engagement'
import type { Messages } from '@/locales/en'

// 阶段决定 (闸门) 的材料与决定卡片. 对话工作台和表单视图共用

type NoteDraft = Annotation & { id: string }

function newNote(): NoteDraft {
  return { id: crypto.randomUUID(), section: 'opinion', quote: '', comment: '' }
}
export const REPORT_SECTIONS: ReportSection[] = [
  'opinion',
  'basis_for_opinion',
  'directors_responsibilities',
  'auditor_responsibilities',
  'other_legal_and_regulatory',
  'signature',
]
const FIN_OPINIONS: OpinionType[] = ['unqualified', 'qualified', 'adverse', 'disclaimer']
const RISK_LEVELS: RiskLevel[] = ['low', 'medium', 'high', 'significant']
const VERDICTS: GateVerdict[] = ['approve', 'reject', 'insufficient_info']
// 当前闸门的材料; 不在闸门时 GET /gate 回 null (需求 R346)
export function useGate(eid: string, enabled = true) {
  return useQuery({
    ...gatesGetGateOptions({ path: { eid } }),
    enabled,
    retry: (count, error) => !isApiCode(error, 'NOT_FOUND') && count < 1,
  })
}

function Panel({ title, aside, children }: { title: ReactNode; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="overflow-hidden rounded-xl border bg-card">
      <div className="flex flex-wrap items-center gap-2.5 border-line-soft border-b px-4 py-3">
        <span className="font-semibold">{title}</span>
        <span className="flex-1" />
        {aside}
      </div>
      <div className="flex flex-col gap-3 p-4">{children}</div>
    </section>
  )
}

export function ExceptionList({ eid, items }: { eid: string; items: AuditException[] }) {
  const { t } = useI18n()
  if (!items.length) return null
  return (
    <Panel title={t.card.exceptions}>
      {items.map((item) => (
        <div key={item.id} className="flex flex-col gap-1.5 text-[14px]">
          <div className="flex flex-wrap items-center gap-2">
            <Code className="font-semibold">{item.id}</Code>
            <StatusPill tone={item.severity === 'high' ? 'risk' : 'warn'}>{item.severity}</StatusPill>
            <Code>{formatAud(item.amount_cents)}</Code>
          </div>
          <ExceptionDetail detail={item.detail ?? {}} />
          <PointerList eid={eid} pointers={item.evidence} />
        </div>
      ))}
    </Panel>
  )
}

// 小数比例 → 百分比文本, 去掉浮点误差 (0.05 → "5")
function percentText(fraction: number): string {
  return String(Math.round(fraction * 100 * 10000) / 10000)
}

// 比例按百分比输入与显示 (1 表示 1%), 存与提交仍是小数 (0.01); 换基准等外部改值时同步显示
function PercentInput({
  id,
  value,
  step,
  onChange,
}: {
  id: string
  value: number
  step: string
  onChange: (value: number) => void
}) {
  const [text, setText] = useState(() => percentText(value))
  useEffect(() => {
    setText((current) => (Number(current) / 100 === value ? current : percentText(value)))
  }, [value])
  return (
    <div className="relative">
      <Input
        id={id}
        className="h-10 pr-8 font-mono"
        type="number"
        inputMode="decimal"
        step={step}
        value={text}
        onChange={(event) => {
          setText(event.target.value)
          const percent = Number(event.target.value)
          if (event.target.value.trim() !== '' && Number.isFinite(percent)) onChange(percent / 100)
        }}
      />
      <span aria-hidden="true" className="-translate-y-1/2 absolute top-1/2 right-3 text-[13px] text-muted-foreground">
        %
      </span>
    </div>
  )
}

// PLN-07 A03 的月度趋势 (本期按月的收入, 费用与员工费用); 没有月度数 (旧结果或没有总账) 时不显示
function MonthlyTable({ rows }: { rows: MonthlyFigures[] }) {
  const { t } = useI18n()
  if (rows.length === 0) return null
  const labels = t.planning.monthly
  return (
    <div className="mt-3 flex flex-col gap-1.5">
      <SectionLabel>{labels.title}</SectionLabel>
      <div className="overflow-x-auto rounded-[10px] border">
        <table className="w-full min-w-[520px] border-collapse text-[13.5px]">
          <thead>
            <tr className="text-left text-[12px] text-muted-foreground">
              <th scope="col" className="border-b px-3 py-2 font-semibold">
                {labels.month}
              </th>
              <th scope="col" className="border-b px-3 py-2 text-right font-semibold">
                {labels.revenue}
              </th>
              <th scope="col" className="border-b px-3 py-2 text-right font-semibold">
                {labels.expense}
              </th>
              <th scope="col" className="border-b px-3 py-2 text-right font-semibold">
                {labels.employee}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.month}>
                <td className="border-b px-3 py-1.5 font-mono">{row.month.slice(0, 7)}</td>
                <td className="border-b px-3 py-1.5 text-right font-mono">{formatAud(row.revenue_cents)}</td>
                <td className="border-b px-3 py-1.5 text-right font-mono">{formatAud(row.expense_cents)}</td>
                <td className="border-b px-3 py-1.5 text-right font-mono">{formatAud(row.employee_expense_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function GateMaterial({ eid, view }: { eid: string; view: GateView }) {
  const { t } = useI18n()
  const material = view.material
  if (material.kind === 'ACC-01') {
    const checks = material.scorecard.registry_checks
    const passed = checks.filter((check) => check.passed === true).length
    const manual = checks.filter((check) => checkTone(check.passed) === 'warn').length
    const tone =
      passed === checks.length ? 'text-ok' : manual > 0 && passed + manual === checks.length ? 'text-warn' : 'text-risk'
    return (
      <div className="flex flex-col gap-3">
        <Panel
          title={
            <>
              <Code className="mr-2 rounded-md bg-muted px-1.5 py-0.5 text-[12px]">ACC-04</Code>
              {t.procedureNames['ACC-04']}
            </>
          }
          aside={
            <span className={`font-medium text-[12.5px] ${tone}`}>
              {manual > 0 ? t.card.checksManual(passed, manual, checks.length) : t.card.checks(passed, checks.length)}
            </span>
          }
        >
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {checks.map((check) => (
              <li key={check.check} className="flex flex-wrap items-center gap-3 py-1.5">
                <CheckIcon passed={check.passed} className="size-[18px]" />
                <span className="flex min-w-[200px] flex-1 flex-col">
                  <span>
                    {t.checks[check.check]}
                    {check.passed === null ? (
                      <span className="ml-2 text-[12.5px] text-warn">{t.card.manualCheck}</span>
                    ) : null}
                  </span>
                  <span className="text-[12.5px] text-muted-foreground" lang="en">
                    {check.detail}
                  </span>
                </span>
                <EvidenceLink eid={eid} pointer={check.evidence} label={t.card.registers[check.check]} />
              </li>
            ))}
          </ul>
        </Panel>
        <ExceptionList eid={eid} items={material.exceptions} />
      </div>
    )
  }
  if (material.kind === 'PLN-01/03') {
    return (
      <div className="flex flex-col gap-3">
        <Panel title={t.planning.analytics}>
          <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[14px]">
            {Object.entries(material.analytics.ratios).map(([name, value]) => (
              <div key={name} className="contents">
                <dt className="text-muted-foreground">{t.ratios[name] ?? name}</dt>
                <dd className="m-0 font-mono">{formatPct(value)}</dd>
              </div>
            ))}
          </dl>
          <MonthlyTable rows={material.analytics.monthly ?? []} />
        </Panel>
        <ExceptionList eid={eid} items={material.analytics_exceptions} />
      </div>
    )
  }
  if (material.kind === 'FIN-01') {
    return (
      <Panel title={t.gates['FIN-01']}>
        <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[14px]">
          <dt className="text-muted-foreground">{t.completion.candidate}</dt>
          <dd className="m-0 font-semibold">{t.opinions[material.schedule.candidate_opinion]}</dd>
          <dt className="text-muted-foreground">{t.completion.total}</dt>
          <dd className="m-0 font-mono">{formatAud(material.schedule.total_cents)}</dd>
          <dt className="text-muted-foreground">{t.completion.materiality}</dt>
          <dd className="m-0 font-mono">{formatAud(material.schedule.materiality_cents)}</dd>
        </dl>
        <FinStaleAlert eid={eid} ids={material.stale_procedure_ids ?? []} />
        <span className="text-[13px] text-muted-foreground">{t.completion.count(material.misstatements.length)}</span>
        <MisstatementList items={material.misstatements} />
        {material.not_executed?.length ? (
          <>
            <span className="text-[13px] text-muted-foreground">
              {t.completion.notExecuted(material.not_executed.length)}
            </span>
            <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[14px]">
              {material.not_executed.map((item) => (
                <li key={item.procedure_id} className="flex flex-col gap-1 rounded-lg border px-3 py-2">
                  <span className="flex flex-wrap gap-2">
                    <Code className="font-semibold">{item.procedure_id}</Code>
                    <span>{procedureName(t, item.procedure_id)}</span>
                  </span>
                  {item.via?.length ? (
                    <span className="text-[13px] text-muted-foreground">{t.completion.via(item.via.join(', '))}</span>
                  ) : null}
                  {item.requests.map((request) => (
                    <span key={request.id} className="text-[13px] text-[#4a5262]">
                      {requestTitle(request, t)}: {reasonText(request.unavailable_reason, t)}
                    </span>
                  ))}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </Panel>
    )
  }
  if (material.kind === 'PLN-15') {
    // 计划的叙述各段; 程序表, 时间表与修订历史在审计计划页 (需求 R315)
    return (
      <Panel
        title={t.gates['PLN-15']}
        aside={
          <Link to="/engagements/$eid/plan" params={{ eid }} className="text-[13px] text-primary hover:underline">
            {t.planPage.open}
          </Link>
        }
      >
        <PlanNarrative eid={eid} plan={material.plan} />
      </Panel>
    )
  }
  return <ReportPreview eid={eid} report={material.report} decisions={material.previous_decisions} />
}

// 计划叙述各段 (计划页与 PLN-15 闸门材料共用): 段名本地化, 正文为 AI 写的, lang 取计划那次执行的语言 (需求 R340)
export function PlanNarrative({ eid, plan }: { eid: string; plan: AuditPlan }) {
  const { t } = useI18n()
  const lang = useContentLanguage(eid, plan.run_id)
  return (
    <div className="flex flex-col gap-3 text-[14px]">
      {plan.narrative.map((part) => (
        <section key={part.section} className="flex flex-col gap-1">
          <h3 className="m-0 font-sans font-semibold text-[13px] text-muted-foreground">
            {t.planPage.sections[part.section] ?? part.section}
          </h3>
          <p className="m-0 whitespace-pre-wrap" lang={lang}>
            {part.text}
          </p>
        </section>
      ))}
    </div>
  )
}

type ReportMaterial = Extract<GateView['material'], { kind: 'RPT-01' }>

// 报告各段; 正文的 lang 取起草它的那次 RPT-03 执行的语言 (需求 R340; 报告按语言出正文归 R341)
export function ReportPreview({
  eid,
  report,
  decisions,
}: {
  eid: string
  report: ReportMaterial['report']
  decisions: ReportMaterial['previous_decisions']
}) {
  const { t } = useI18n()
  const lang = useContentLanguage(eid, report.run_id)
  return (
    <div className="flex flex-col gap-3">
      <Panel
        title={t.reportCard.revision(report.revision, t.opinions[report.opinion_type])}
        aside={report.status === 'signed' ? <StatusPill tone="ok">{t.stageStatus.signed}</StatusPill> : null}
      >
        <div
          className="flex flex-col gap-4 rounded-md border bg-white px-6 py-5 font-serif text-[14px] leading-relaxed"
          lang={lang}
        >
          {REPORT_SECTIONS.map((section) => {
            const part = reportPart(section, report.sections[section] ?? '', t.reportSections[section])
            return (
              <section key={section} className="flex flex-col gap-1">
                <h3 className="m-0 font-sans font-semibold text-[13px] text-muted-foreground">{part.heading}</h3>
                <p className="m-0 whitespace-pre-wrap">{part.body}</p>
              </section>
            )
          })}
        </div>
      </Panel>
      {decisions.length > 0 ? (
        <Panel title={t.reportCard.earlier}>
          {decisions.map((decision) => (
            <div key={decision.id} className="flex flex-col gap-1 text-[14px]">
              <span className="font-medium">{reasonText(decision.reason, t)}</span>
              {(decision.annotations ?? []).map((note) => (
                <span key={`${decision.id}-${note.section}-${note.comment}`} className="text-secondary-foreground">
                  {t.reportSections[note.section]}: {note.comment}
                </span>
              ))}
            </div>
          ))}
        </Panel>
      ) : null}
    </div>
  )
}

export function GateDecisionCard({
  eid,
  view,
  disabled,
  deferred = false,
  onDefer,
  deferring = false,
}: {
  eid: string
  view: GateView
  disabled: boolean
  deferred?: boolean
  onDefer?: () => void
  deferring?: boolean
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [verdict, setVerdict] = useState<GateVerdict | null>(null)
  const [reason, setReason] = useState('')
  const [choice, setChoice] = useState<MaterialityChoice | null>(
    view.material.kind === 'PLN-01/03' ? initialChoice(view.material.candidates) : null,
  )
  const [levels, setLevels] = useState<Record<string, RiskLevel>>(() =>
    view.material.kind === 'PLN-01/03'
      ? Object.fromEntries(view.material.risks.map((risk) => [risk.id, risk.level]))
      : {},
  )
  const [levelReasons, setLevelReasons] = useState<Record<string, string>>({})
  const [disposition, setDisposition] = useState<OpinionType>(
    view.material.kind === 'FIN-01' ? view.material.schedule.candidate_opinion : 'qualified',
  )
  const [notes, setNotes] = useState<NoteDraft[]>([newNote()])
  // PLN-15 批准时的三项确认 (需求 R206, R315), 初值见 initialConfirmation; 别的闸门不用
  const [confirmation, setConfirmation] = useState<PlanConfirmation>(() =>
    view.material.kind === 'PLN-15'
      ? initialConfirmation(view.material.plan)
      : { eqr_required: false, eqr_basis: '', auditor_expert_not_required: false },
  )
  const submit = useMutation({
    ...gatesSubmitGateDecisionMutation(),
    onSuccess: async () => {
      toast.success(t.toasts.decisionRecorded)
      setReason('')
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) =>
      toast.error(view.material.kind === 'PLN-15' ? planDecisionErrorText(error, t) : apiMessage(error)),
  })

  const gate = view.gate
  const uncovered = view.material.kind === 'PLN-01/03' && view.material.uncovered_objective_ids.length > 0
  const busy = disabled || submit.isPending
  const planApproval = view.material.kind === 'PLN-15' && verdict === 'approve'

  const send = () => {
    if (!verdict) return
    const body = buildDecision(
      view,
      verdict,
      { reason, choice, levels, levelReasons, disposition, notes, confirmation },
      t,
    )
    if (typeof body === 'string') {
      toast.error(body)
      return
    }
    submit.mutate({ path: { eid }, body })
  }

  return (
    <DecisionFrame
      eid={eid}
      title={
        <>
          <Code className="font-semibold">{gate}</Code> · {t.gateQuestions[gate]}
        </>
      }
      note={deferred ? t.workspace.deferredNote : undefined}
      footer={
        <>
          {onDefer && !deferred ? (
            <Button type="button" variant="outline" size="lg" disabled={busy || deferring} onClick={onDefer}>
              {t.card.decideLater}
            </Button>
          ) : (
            <span />
          )}
          <Button
            type="button"
            size="lg"
            disabled={
              busy ||
              !verdict ||
              (verdict === 'approve' && uncovered) ||
              (planApproval && !confirmation.eqr_basis.trim())
            }
            onClick={send}
          >
            {t.card.record}
          </Button>
        </>
      }
    >
      {view.material.kind === 'PLN-01/03' && choice ? (
        <PlanningFields
          candidates={view.material.candidates}
          risks={view.material.risks}
          choice={choice}
          onChoice={setChoice}
          levels={levels}
          onLevel={(id, level) => setLevels((current) => ({ ...current, [id]: level }))}
          levelReasons={levelReasons}
          onLevelReason={(id, value) => setLevelReasons((current) => ({ ...current, [id]: value }))}
        />
      ) : null}
      {uncovered && view.material.kind === 'PLN-01/03' ? (
        <Alert variant="destructive">
          <AlertDescription>{t.planning.uncovered(view.material.uncovered_objective_ids.join(', '))}</AlertDescription>
        </Alert>
      ) : null}

      <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
        <legend className="sr-only">{t.card.decision}</legend>
        {VERDICTS.map((item) => (
          <OptionCard
            key={item}
            name={`gate-${gate}`}
            value={item}
            checked={verdict === item}
            onSelect={() => setVerdict(item)}
            label={t.verdicts[gate][item]}
            hint={t.verdictHints[gate][item]}
            disabled={busy || (item === 'approve' && uncovered)}
          />
        ))}
      </fieldset>

      {view.material.kind === 'FIN-01' && verdict === 'approve' ? (
        <div className="flex flex-col gap-1">
          <label htmlFor="fin-opinion" className="font-semibold text-[13px]">
            {t.completion.opinion}
          </label>
          <select
            id="fin-opinion"
            className={SELECT}
            value={disposition}
            onChange={(event) => setDisposition(event.target.value as OpinionType)}
          >
            {FIN_OPINIONS.map((item) => (
              <option key={item} value={item}>
                {t.opinions[item]}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {view.material.kind === 'RPT-01' && verdict === 'reject' ? (
        <AnnotationFields notes={notes} onChange={setNotes} />
      ) : null}
      {view.material.kind === 'PLN-15' && planApproval ? (
        <PlanConfirmationFields
          suggested={view.material.plan.eqr_recommendation.required}
          value={confirmation}
          onChange={setConfirmation}
          disabled={busy}
        />
      ) : null}

      <ReasonField id={`gate-reason-${gate}`} value={reason} onChange={setReason} />
    </DecisionFrame>
  )
}

function PlanningFields({
  candidates,
  risks,
  choice,
  onChoice,
  levels,
  onLevel,
  levelReasons,
  onLevelReason,
}: {
  candidates: MaterialityCandidates
  risks: RiskItem[]
  choice: MaterialityChoice
  onChoice: (choice: MaterialityChoice) => void
  levels: Record<string, RiskLevel>
  onLevel: (id: string, level: RiskLevel) => void
  levelReasons: Record<string, string>
  onLevelReason: (id: string, reason: string) => void
}) {
  const { t } = useI18n()
  const selected = candidates.candidates.find((item) => item.basis === choice.basis)
  const range = candidates.performance_pct_range ?? [0.5, 0.75]
  const overall = selected ? Math.round(selected.base_amount_cents * choice.pct) : null
  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex flex-col gap-1.5">
        <SectionLabel>{t.planning.candidates}</SectionLabel>
        <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13.5px]">
          {candidates.candidates.map((item) => {
            const reason = inapplicable(item)
            return (
              <li key={item.basis} className="flex flex-wrap gap-x-2">
                <span className="min-w-28 font-medium">{t.bases[item.basis] ?? item.basis}</span>
                <Code>{formatAud(item.base_amount_cents)}</Code>
                {reason ? (
                  <span className="text-muted-foreground">· {t.planning.notApplicable[reason]}</span>
                ) : (
                  <span className="text-muted-foreground">
                    · {formatPct(item.pct_low)}–{formatPct(item.pct_high)} · <Code>{formatAud(item.low_cents)}</Code>–
                    <Code>{formatAud(item.high_cents)}</Code>
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
        <LabeledField id="pln-basis" label={t.planning.basis}>
          <select
            id="pln-basis"
            className={SELECT}
            value={choice.basis}
            onChange={(event) => {
              const basis = event.target.value as MaterialityChoice['basis']
              const next = candidates.candidates.find((item) => item.basis === basis)
              onChoice({ ...choice, basis, pct: next?.pct_high ?? choice.pct })
            }}
          >
            {selectableCandidates(candidates).map((item) => (
              <option key={item.basis} value={item.basis}>
                {t.bases[item.basis] ?? item.basis}
              </option>
            ))}
          </select>
        </LabeledField>
        <LabeledField
          id="pln-pct"
          label={t.planning.overallPct(formatPct(selected?.pct_low), formatPct(selected?.pct_high))}
        >
          <PercentInput id="pln-pct" step="0.05" value={choice.pct} onChange={(pct) => onChoice({ ...choice, pct })} />
        </LabeledField>
        <LabeledField id="pln-perf" label={t.planning.performancePct(formatPct(range[0]), formatPct(range[1]))}>
          <PercentInput
            id="pln-perf"
            step="1"
            value={choice.performance_pct}
            onChange={(performance_pct) => onChoice({ ...choice, performance_pct })}
          />
        </LabeledField>
        <LabeledField id="pln-trivial" label={t.planning.trivialPct}>
          <PercentInput
            id="pln-trivial"
            step="1"
            value={choice.clearly_trivial_pct}
            onChange={(clearly_trivial_pct) => onChoice({ ...choice, clearly_trivial_pct })}
          />
        </LabeledField>
      </div>
      <span className="rounded-lg bg-muted px-3 py-2 text-[13.5px]">
        {t.planning.preview(
          formatAud(overall),
          overall != null ? formatAud(Math.round(overall * choice.performance_pct)) : '—',
        )}
      </span>
      <div className="flex flex-col gap-2">
        <SectionLabel>{t.planning.risks}</SectionLabel>
        {risks.map((risk) => {
          const level = levels[risk.id] ?? risk.level
          return (
            <div key={risk.id} className="flex flex-col gap-2 rounded-[10px] border p-3 text-[14px]">
              <div className="flex flex-wrap items-center gap-2">
                <Code className="font-semibold">{risk.id}</Code>
                <span>{t.assertions[risk.assertion]}</span>
                {risk.fraud_related ? <StatusPill tone="risk">{t.planning.fraud}</StatusPill> : null}
                <span className="flex-1" />
                <label htmlFor={`level-${risk.id}`} className="sr-only">
                  {risk.id}
                </label>
                <select
                  id={`level-${risk.id}`}
                  className={SELECT}
                  value={level}
                  onChange={(event) => onLevel(risk.id, event.target.value as RiskLevel)}
                >
                  {RISK_LEVELS.map((item) => (
                    <option key={item} value={item}>
                      {t.riskLevels[item]}
                    </option>
                  ))}
                </select>
              </div>
              <span className="text-secondary-foreground" lang="en">
                {risk.description}
              </span>
              {level !== risk.level ? (
                <Input
                  placeholder={t.planning.levelReason}
                  value={levelReasons[risk.id] ?? ''}
                  onChange={(event) => onLevelReason(risk.id, event.target.value)}
                />
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// PLN-15 批准时的确认: 是否需要项目质量复核 (默认取 AI 建议) 与依据 (必填), 不需要审计师的专家 (不勾时提示只作记录)
function PlanConfirmationFields({
  suggested,
  value,
  onChange,
  disabled,
}: {
  suggested: boolean
  value: PlanConfirmation
  onChange: (value: PlanConfirmation) => void
  disabled: boolean
}) {
  const { t } = useI18n()
  const labels = t.planPage.confirm
  return (
    <div className="flex flex-col gap-3">
      <fieldset className="m-0 flex flex-col gap-2 border-0 p-0">
        <legend className="pb-1 font-semibold text-[13px]">
          {labels.eqr} <span className="font-normal text-muted-foreground">{t.planPage.eqrSuggested(suggested)}</span>
        </legend>
        <div className="flex flex-wrap gap-2.5">
          {([true, false] as const).map((required) => (
            <OptionCard
              key={String(required)}
              name="plan-eqr"
              value={String(required)}
              checked={value.eqr_required === required}
              onSelect={() => onChange({ ...value, eqr_required: required })}
              label={required ? labels.yes : labels.no}
              disabled={disabled}
            />
          ))}
        </div>
      </fieldset>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="plan-eqr-basis" className="font-semibold text-[13px]">
          {labels.basis} <span className="font-normal text-muted-foreground">{labels.basisHint}</span>
        </label>
        <Textarea
          id="plan-eqr-basis"
          rows={3}
          lang="en"
          value={value.eqr_basis}
          disabled={disabled}
          onChange={(event) => onChange({ ...value, eqr_basis: event.target.value })}
          className="text-[14px]"
        />
      </div>
      <div className="flex flex-col gap-1">
        <label className="flex items-center gap-2 text-[14px]">
          <input
            type="checkbox"
            className="size-4 accent-primary"
            checked={value.auditor_expert_not_required}
            disabled={disabled}
            aria-describedby={value.auditor_expert_not_required ? undefined : 'plan-expert-hint'}
            onChange={(event) => onChange({ ...value, auditor_expert_not_required: event.target.checked })}
          />
          {labels.expert}
        </label>
        {value.auditor_expert_not_required ? null : (
          <span id="plan-expert-hint" className="text-[13px] text-muted-foreground">
            {labels.expertHint}
          </span>
        )}
      </div>
    </div>
  )
}

function AnnotationFields({ notes, onChange }: { notes: NoteDraft[]; onChange: (notes: NoteDraft[]) => void }) {
  const { t } = useI18n()
  const update = (id: string, patch: Partial<NoteDraft>) =>
    onChange(notes.map((item) => (item.id === id ? { ...item, ...patch } : item)))
  return (
    <div className="flex flex-col gap-3">
      {notes.map((note) => (
        <div key={note.id} className="flex flex-col gap-2 rounded-[10px] border p-3">
          <select
            aria-label={t.reportCard.section}
            className={SELECT}
            value={note.section}
            onChange={(event) => update(note.id, { section: event.target.value as ReportSection })}
          >
            {REPORT_SECTIONS.map((section) => (
              <option key={section} value={section}>
                {t.reportSections[section]}
              </option>
            ))}
          </select>
          <Input
            placeholder={t.reportCard.quote}
            value={note.quote ?? ''}
            onChange={(event) => update(note.id, { quote: event.target.value })}
          />
          <Textarea
            placeholder={t.reportCard.comment}
            value={note.comment}
            onChange={(event) => update(note.id, { comment: event.target.value })}
          />
        </div>
      ))}
      <Button type="button" variant="outline" className="self-start" onClick={() => onChange([...notes, newNote()])}>
        {t.reportCard.addComment}
      </Button>
    </div>
  )
}

function buildDecision(
  view: GateView,
  decision: GateVerdict,
  input: {
    reason: string
    choice: MaterialityChoice | null
    levels: Record<string, RiskLevel>
    levelReasons: Record<string, string>
    disposition: OpinionType
    notes: NoteDraft[]
    confirmation: PlanConfirmation
  },
  t: Messages,
): GateDecisionIn | string {
  const reason = input.reason.trim()
  const base: GateDecisionIn = {
    gate: view.gate,
    target_id: view.target_id,
    target_version: view.target_version,
    decision,
    reason,
  }
  if (view.material.kind === 'PLN-01/03' && decision === 'approve') {
    if (!input.choice) return t.errors.chooseBasis
    const edits: GateEdit[] = view.material.risks.flatMap((risk) => {
      const level = input.levels[risk.id] ?? risk.level
      if (level === risk.level) return []
      return [
        {
          target_id: risk.id,
          target_version: risk.version ?? 1,
          field: 'level',
          new_value: level,
          reason: input.levelReasons[risk.id]?.trim() ?? '',
        },
      ]
    })
    return { ...base, materiality: input.choice, edits }
  }
  if (view.material.kind === 'FIN-01' && decision === 'approve') return { ...base, disposition: input.disposition }
  if (view.material.kind === 'PLN-15' && decision === 'approve') return planApprovalBody(base, input.confirmation, t)
  if (view.material.kind === 'RPT-01' && decision === 'reject') {
    const annotations = input.notes
      .map((note) => ({ section: note.section, quote: note.quote?.trim() || null, comment: note.comment.trim() }))
      .filter((note) => note.comment)
    if (!annotations.length) return t.reportCard.needComment
    return { ...base, annotations }
  }
  return base
}

// 表单视图: 材料加决定卡片
export function GatePanel({ eid, disabled }: { eid: string; disabled: boolean }) {
  const { t } = useI18n()
  const gate = useGate(eid)
  if (gate.data === null) {
    return <p className="text-muted-foreground text-sm">{t.formView.notAtGate}</p>
  }
  return (
    <QueryState isPending={gate.isPending} error={gate.error}>
      {gate.data ? (
        <div className="flex flex-col gap-5">
          <GateMaterial eid={eid} view={gate.data} />
          {gate.data.material.kind === 'ACC-01' ? (
            <ProfileForm eid={eid} profile={gate.data.material.client_profile} disabled={disabled} />
          ) : null}
          <GateDecisionCard key={decisionCardKey(gate.data)} eid={eid} view={gate.data} disabled={disabled} />
        </div>
      ) : null}
    </QueryState>
  )
}

export function RptSignForm({ eid, disabled }: { eid: string; disabled: boolean }) {
  const gate = useGate(eid)
  const view = gate.data
  if (view?.material.kind !== 'RPT-01') return null
  return <GateDecisionCard key={decisionCardKey(view)} eid={eid} view={view} disabled={disabled} />
}

export function ProfileForm({
  eid,
  profile,
  disabled,
  onDone,
}: {
  eid: string
  profile: ClientProfile
  disabled: boolean
  onDone?: () => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(() => profileDraft(profile))
  // 新建的客户 (项目没有数据集) 的 ACC-04 经 ABN Lookup 实查, 这里同样在线核对; 有数据集的读数据集的登记, 不查
  const live = useOverview(eid)?.engagement.dataset === null
  // ABN 的位数与校验位同新建客户 (保存时后端照查): 填满 11 位前不提示位数
  const problem = abnProblem(draft.abn)
  const abnError =
    problem === 'checksum'
      ? t.newClient.errors.abnChecksum
      : problem === 'format' && normalizeAbn(draft.abn).length >= 11
        ? t.newClient.errors.abnFormat
        : undefined
  const save = useMutation({
    ...gatesUpdateClientProfileMutation(),
    onSuccess: async () => {
      toast.success(t.toasts.profileSaved)
      await invalidateEngagement(queryClient, eid)
      onDone?.()
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <span className="font-semibold">{t.formView.profile}</span>
      <ProfileFields
        idPrefix="pf"
        draft={draft}
        onChange={setDraft}
        errors={{ abn: abnError }}
        abnHint={live && !problem ? <AbnLookupNote abn={draft.abn} legalName={draft.legalName} /> : undefined}
      />
      <span className="text-[13px] text-muted-foreground">{t.clientPage.rerunNote}</span>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          disabled={disabled || save.isPending}
          onClick={() =>
            save.mutate({
              path: { eid },
              body: { version: (profile.version ?? 1) + 1, ...draftFields(draft) },
            })
          }
        >
          {t.clientPage.save}
        </Button>
        {onDone ? (
          <Button type="button" variant="outline" onClick={onDone}>
            {t.clientPage.cancel}
          </Button>
        ) : null}
        <BusyNote eid={eid} className="basis-full" />
      </div>
    </section>
  )
}
