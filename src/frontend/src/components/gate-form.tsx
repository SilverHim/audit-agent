import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CircleCheck, CircleX } from 'lucide-react'
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
  ClientProfile,
  GateDecisionIn,
  GateEdit,
  GateVerdict,
  GateView,
  MaterialityCandidates,
  MaterialityChoice,
  MonthlyFigures,
  OpinionType,
  ReportSection,
  RiskItem,
  RiskLevel,
} from '@/client/types.gen'
import { Code, SectionLabel, StatusPill } from '@/components/chrome'
import { DecisionFrame, OptionCard, ReasonField } from '@/components/concierge'
import { EvidenceLink, PointerList } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { FinStaleAlert } from '@/components/rerun'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { requestTitle } from '@/lib/data-api'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { formatAud, formatPct } from '@/lib/money'
import { procedureName } from '@/lib/review'
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
const SELECT = 'h-10 rounded-lg border border-input bg-card px-2.5 text-[14px]'

export function useGate(eid: string, enabled = true) {
  return useQuery({
    ...gatesGetGateOptions({ path: { eid } }),
    enabled,
    retry: (count, error) => !isApiCode(error, 'NOT_AT_GATE') && !isApiCode(error, 'NOT_FOUND') && count < 1,
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

type FluctuationBasis = 'balance_sheet' | 'profit_or_loss'

// PLN-07 A01 的波动 (输出列见 PLN-07.yaml): 比较基数 opening 为资产负债类的期初 (含留存收益的结转) 或损益类的上期,
// 变动率 change 相对它, 基数为 0 时为空; 只在一期出现的科目, 另一期的余额为空 (显示 "—")
function FluctuationDetail({
  account,
  basis,
  opening,
  current,
  change,
}: {
  account: string
  basis: FluctuationBasis
  opening: number
  current: number | null
  change: number | null
}) {
  const { t } = useI18n()
  const labels = t.planning.fluctuation
  return (
    <span className="text-secondary-foreground">
      <span lang="en">{account}</span> · {labels.basis[basis]}: <Code>{formatAud(opening)}</Code> → {labels.current}:{' '}
      <Code>{formatAud(current)}</Code> (
      {change === null ? labels.zeroBase[basis] : `${change > 0 ? '+' : ''}${(change * 100).toFixed(1)}%`})
    </span>
  )
}

function ExceptionDetail({ detail }: { detail: Record<string, unknown> }) {
  const { account_name: account, basis, opening_cents: opening, current_cents: current, change_pct: change } = detail
  if (
    typeof account === 'string' &&
    typeof opening === 'number' &&
    (basis === 'balance_sheet' || basis === 'profit_or_loss')
  ) {
    const ratio = change == null ? Number.NaN : Number(change) // numeric 列可能序列化为字符串
    return (
      <FluctuationDetail
        account={account}
        basis={basis}
        opening={opening}
        current={typeof current === 'number' ? current : null}
        change={Number.isFinite(ratio) ? ratio : null}
      />
    )
  }
  // 有的规则 (如 ACC-04) 例外没有明细, 空的不显示
  if (Object.keys(detail).length === 0) return null
  return (
    <pre className="overflow-x-auto rounded-md bg-muted p-2.5 font-mono text-xs">{JSON.stringify(detail, null, 2)}</pre>
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
    const passed = checks.filter((check) => check.passed).length
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
            <span
              className={
                passed === checks.length ? 'font-medium text-[12.5px] text-ok' : 'font-medium text-[12.5px] text-risk'
              }
            >
              {t.card.checks(passed, checks.length)}
            </span>
          }
        >
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {checks.map((check) => (
              <li key={check.check} className="flex flex-wrap items-center gap-3 py-1.5">
                {check.passed ? (
                  <CircleCheck className="size-[18px] text-ok" aria-hidden="true" />
                ) : (
                  <CircleX className="size-[18px] text-risk" aria-hidden="true" />
                )}
                <span className="flex min-w-[200px] flex-1 flex-col">
                  <span>{t.checks[check.check]}</span>
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
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[14px]">
          {material.misstatements.map((item) => (
            <li key={item.id} className="flex flex-wrap gap-2 rounded-lg border px-3 py-2">
              <Code className="font-semibold">{item.id}</Code>
              <span>
                {item.account} · {t.assertions[item.assertion]} · {t.directions[item.direction]}
              </span>
              <span className="flex-1" />
              <Code>{formatAud(item.amount_cents)}</Code>
            </li>
          ))}
        </ul>
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
                      {requestTitle(request, t)}: {request.unavailable_reason}
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
  return <ReportPreview report={material.report} decisions={material.previous_decisions} />
}

type ReportMaterial = Extract<GateView['material'], { kind: 'RPT-01' }>

export function ReportPreview({
  report,
  decisions,
}: {
  report: ReportMaterial['report']
  decisions: ReportMaterial['previous_decisions']
}) {
  const { t } = useI18n()
  return (
    <div className="flex flex-col gap-3">
      <Panel
        title={t.reportCard.revision(report.revision, t.opinions[report.opinion_type])}
        aside={report.status === 'signed' ? <StatusPill tone="ok">{t.stageStatus.signed}</StatusPill> : null}
      >
        <div
          className="flex flex-col gap-4 rounded-md border bg-white px-6 py-5 font-serif text-[14px] leading-relaxed"
          lang="en"
        >
          {REPORT_SECTIONS.map((section) => (
            <section key={section} className="flex flex-col gap-1">
              <h3 className="m-0 font-sans font-semibold text-[13px] text-muted-foreground">
                {t.reportSections[section]}
              </h3>
              <p className="m-0 whitespace-pre-wrap">{report.sections[section] || '—'}</p>
            </section>
          ))}
        </div>
      </Panel>
      {decisions.length > 0 ? (
        <Panel title={t.reportCard.earlier}>
          {decisions.map((decision) => (
            <div key={decision.id} className="flex flex-col gap-1 text-[14px]">
              <span className="font-medium">{decision.reason}</span>
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
  const submit = useMutation({
    ...gatesSubmitGateDecisionMutation(),
    onSuccess: async () => {
      toast.success(t.toasts.decisionRecorded)
      setReason('')
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  const gate = view.gate
  const uncovered = view.material.kind === 'PLN-01/03' && view.material.uncovered_objective_ids.length > 0
  const busy = disabled || submit.isPending
  const required = verdict != null && (verdict !== 'approve' || gate === 'FIN-01')

  const send = () => {
    if (!verdict) return
    const body = buildDecision(view, verdict, { reason, choice, levels, levelReasons, disposition, notes }, t)
    if (typeof body === 'string') {
      toast.error(body)
      return
    }
    submit.mutate({ path: { eid }, body })
  }

  return (
    <DecisionFrame
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
            disabled={busy || !verdict || (required && !reason.trim()) || (verdict === 'approve' && uncovered)}
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

      <ReasonField id={`gate-reason-${gate}`} value={reason} onChange={setReason} required={required} />
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
          {candidates.candidates.map((item) => (
            <li key={item.basis} className="flex flex-wrap gap-x-2">
              <span className="min-w-28 font-medium">{t.bases[item.basis] ?? item.basis}</span>
              <Code>{formatAud(item.base_amount_cents)}</Code>
              <span className="text-muted-foreground">
                · {formatPct(item.pct_low)}–{formatPct(item.pct_high)} · <Code>{formatAud(item.low_cents)}</Code>–
                <Code>{formatAud(item.high_cents)}</Code>
              </span>
            </li>
          ))}
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
            {candidates.candidates.map((item) => (
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

function LabeledField({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[12.5px] text-[#4a5262]">
        {label}
      </label>
      {children}
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
  },
  t: Messages,
): GateDecisionIn | string {
  const reason = input.reason.trim()
  const base: GateDecisionIn = {
    gate: view.gate,
    target_id: view.target_id,
    target_version: view.target_version,
    decision,
    reason: reason || null,
  }
  if (decision !== 'approve' && !reason) return t.errors.reasonRequired
  if (view.material.kind === 'FIN-01' && decision === 'approve' && !reason) return t.errors.reasonRequired
  if (view.material.kind === 'PLN-01/03' && decision === 'approve') {
    if (!input.choice) return t.errors.chooseBasis
    const missing = view.material.risks.find(
      (risk) => (input.levels[risk.id] ?? risk.level) !== risk.level && !input.levelReasons[risk.id]?.trim(),
    )
    if (missing) return t.planning.giveReason(missing.id)
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
  if (view.material.kind === 'RPT-01' && decision === 'reject') {
    const annotations = input.notes
      .map((note) => ({ section: note.section, quote: note.quote?.trim() || null, comment: note.comment.trim() }))
      .filter((note) => note.comment)
    if (!annotations.length) return t.reportCard.needComment
    return { ...base, annotations }
  }
  return base
}

function initialChoice(candidates: MaterialityCandidates): MaterialityChoice {
  const usable = candidates.candidates.find((item) => item.base_amount_cents > 0) ?? candidates.candidates[0]
  const range = candidates.performance_pct_range ?? [0.5, 0.75]
  return {
    basis: usable?.basis ?? 'revenue',
    pct: usable?.pct_high ?? 0.01,
    performance_pct: range[1],
    clearly_trivial_pct: candidates.clearly_trivial_pct ?? 0.05,
  }
}

// 表单视图: 材料加决定卡片
export function GatePanel({ eid, disabled }: { eid: string; disabled: boolean }) {
  const { t } = useI18n()
  const gate = useGate(eid)
  if (gate.error && isApiCode(gate.error, 'NOT_AT_GATE')) {
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
          <GateDecisionCard eid={eid} view={gate.data} disabled={disabled} />
        </div>
      ) : null}
    </QueryState>
  )
}

export function RptSignForm({ eid, disabled }: { eid: string; disabled: boolean }) {
  const gate = useGate(eid)
  const view = gate.data
  if (view?.material.kind !== 'RPT-01') return null
  return <GateDecisionCard eid={eid} view={view} disabled={disabled} />
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
  const [legalName, setLegalName] = useState(profile.legal_name)
  const [abn, setAbn] = useState(profile.abn)
  const [entityType, setEntityType] = useState(profile.entity_type)
  const [industry, setIndustry] = useState(profile.industry_anzsic)
  const [listed, setListed] = useState(profile.listed)
  const [directors, setDirectors] = useState(profile.directors.join('\n'))
  const [notes, setNotes] = useState(profile.notes ?? '')
  const save = useMutation({
    ...gatesUpdateClientProfileMutation(),
    onSuccess: async () => {
      toast.success(t.toasts.profileSaved)
      await invalidateEngagement(queryClient, eid)
      onDone?.()
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  const field = 'h-10'
  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <span className="font-semibold">{t.formView.profile}</span>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
        <LabeledField id="pf-name" label={t.clientPage.legalName}>
          <Input
            id="pf-name"
            className={field}
            value={legalName}
            onChange={(event) => setLegalName(event.target.value)}
          />
        </LabeledField>
        <LabeledField id="pf-abn" label={t.clientPage.abn}>
          <Input
            id="pf-abn"
            className={`${field} font-mono`}
            value={abn}
            onChange={(event) => setAbn(event.target.value)}
          />
        </LabeledField>
        <LabeledField id="pf-type" label={t.clientPage.entityType}>
          <select
            id="pf-type"
            className={SELECT}
            value={entityType}
            onChange={(event) => setEntityType(event.target.value)}
          >
            {Object.entries(t.clientPage.entityTypes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </LabeledField>
        <LabeledField id="pf-industry" label={t.clientPage.industry}>
          <Input
            id="pf-industry"
            className={`${field} font-mono`}
            value={industry}
            onChange={(event) => setIndustry(event.target.value)}
          />
        </LabeledField>
      </div>
      <label className="flex items-center gap-2 text-[14px]">
        <input
          type="checkbox"
          className="size-4 accent-primary"
          checked={listed}
          onChange={(event) => setListed(event.target.checked)}
        />
        {t.clientPage.listed}
      </label>
      <LabeledField id="pf-directors" label={`${t.clientPage.directors} · ${t.clientPage.directorsHint}`}>
        <Textarea id="pf-directors" value={directors} onChange={(event) => setDirectors(event.target.value)} />
      </LabeledField>
      <LabeledField id="pf-notes" label={t.clientPage.notes}>
        <Textarea id="pf-notes" value={notes} onChange={(event) => setNotes(event.target.value)} />
      </LabeledField>
      <span className="text-[13px] text-muted-foreground">{t.clientPage.rerunNote}</span>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          disabled={disabled || save.isPending}
          onClick={() =>
            save.mutate({
              path: { eid },
              body: {
                version: (profile.version ?? 1) + 1,
                legal_name: legalName.trim(),
                abn: abn.replaceAll(' ', ''),
                entity_type: entityType,
                industry_anzsic: industry.trim(),
                listed,
                directors: directors
                  .split('\n')
                  .map((line) => line.trim())
                  .filter(Boolean),
                notes,
              },
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
      </div>
    </section>
  )
}
