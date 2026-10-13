import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'
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
  OpinionType,
  ReportSection,
  RiskItem,
  RiskLevel,
} from '@/client/types.gen'
import { PointerList } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage, isApiCode } from '@/lib/errors'
import { formatAud, formatPct } from '@/lib/money'

type NoteDraft = Annotation & { id: string }

function newNote(): NoteDraft {
  return { id: crypto.randomUUID(), section: 'opinion', quote: '', comment: '' }
}
const REPORT_SECTIONS: ReportSection[] = [
  'opinion',
  'basis_for_opinion',
  'directors_responsibilities',
  'auditor_responsibilities',
  'other_legal_and_regulatory',
  'signature',
]
const FIN_OPINIONS: OpinionType[] = ['unqualified', 'qualified']
const RISK_LEVELS: RiskLevel[] = ['low', 'medium', 'high', 'significant']

export function GatePanel({ eid, disabled }: { eid: string; disabled: boolean }) {
  const gate = useQuery({
    ...gatesGetGateOptions({ path: { eid } }),
    retry: (count, error) => !isApiCode(error, 'NOT_AT_GATE') && !isApiCode(error, 'NOT_FOUND') && count < 1,
  })
  if (gate.error && isApiCode(gate.error, 'NOT_AT_GATE')) {
    return <p className="text-muted-foreground text-sm">The engagement is not waiting at a gate.</p>
  }
  return (
    <QueryState isPending={gate.isPending} error={gate.error}>
      {gate.data ? (
        <div className="space-y-6">
          <GateMaterial eid={eid} view={gate.data} disabled={disabled} />
          <GateDecisionForm eid={eid} view={gate.data} disabled={disabled} />
        </div>
      ) : null}
    </QueryState>
  )
}

export function RptSignForm({ eid, disabled }: { eid: string; disabled: boolean }) {
  const gate = useQuery({
    ...gatesGetGateOptions({ path: { eid } }),
    retry: (count, error) => !isApiCode(error, 'NOT_AT_GATE') && count < 1,
  })
  const view = gate.data
  if (view?.material.kind !== 'RPT-01') return null
  return <GateDecisionForm eid={eid} view={view} disabled={disabled} />
}

function GateMaterial({ eid, view, disabled }: { eid: string; view: GateView; disabled: boolean }) {
  const material = view.material
  if (material.kind === 'ACC-01') {
    return (
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Acceptance checks</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {material.scorecard.registry_checks.map((check) => (
              <div key={check.check} className="space-y-1 text-sm">
                <p>
                  <span className="font-medium">{check.check}</span> · {check.passed ? 'passed' : 'failed'}
                </p>
                <p>{check.detail}</p>
                <PointerList eid={eid} pointers={[check.evidence]} />
              </div>
            ))}
          </CardContent>
        </Card>
        <ExceptionList eid={eid} items={material.exceptions} />
        <ProfileForm eid={eid} profile={material.client_profile} disabled={disabled} />
      </div>
    )
  }
  if (material.kind === 'PLN-01/03') {
    return (
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Analytics</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {Object.entries(material.analytics.ratios).map(([name, value]) => (
              <p key={name}>
                {name}: {value.toFixed(4)}
              </p>
            ))}
          </CardContent>
        </Card>
        <ExceptionList eid={eid} items={material.analytics_exceptions} />
        {material.uncovered_objective_ids.length > 0 ? (
          <Alert variant="destructive">
            <AlertDescription>
              These objectives have no procedure coverage: {material.uncovered_objective_ids.join(', ')}. Approve stays
              disabled until the risk levels change or the plan is rejected.
            </AlertDescription>
          </Alert>
        ) : null}
      </div>
    )
  }
  if (material.kind === 'FIN-01') {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Uncorrected misstatements</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p>Candidate opinion: {material.schedule.candidate_opinion}</p>
          <p>Total: {formatAud(material.schedule.total_cents)}</p>
          <p>Overall materiality: {formatAud(material.schedule.materiality_cents)}</p>
          <p>{material.misstatements.length} misstatement(s) recorded.</p>
        </CardContent>
      </Card>
    )
  }
  return <ReportPreview report={material.report} decisions={material.previous_decisions} />
}

function ExceptionList({ eid, items }: { eid: string; items: AuditException[] }) {
  if (!items.length) return <p className="text-muted-foreground text-sm">No exceptions.</p>
  return (
    <Card>
      <CardHeader>
        <CardTitle>Exceptions</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {items.map((item) => (
          <div key={item.id} className="space-y-1 text-sm">
            <p className="font-medium">
              {item.id} · {item.severity} · {formatAud(item.amount_cents)}
            </p>
            <p className="text-muted-foreground">{JSON.stringify(item.detail)}</p>
            <PointerList eid={eid} pointers={item.evidence} />
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export function ReportPreview({
  report,
  decisions,
}: {
  report: Extract<GateView['material'], { kind: 'RPT-01' }>['report']
  decisions: Extract<GateView['material'], { kind: 'RPT-01' }>['previous_decisions']
}) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>
            Revision {report.revision} · {report.opinion_type} · {report.status}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          {REPORT_SECTIONS.map((section) => (
            <section key={section}>
              <h3 className="font-medium capitalize">{section.replaceAll('_', ' ')}</h3>
              <p className="whitespace-pre-wrap">{report.sections[section] || '—'}</p>
            </section>
          ))}
        </CardContent>
      </Card>
      {decisions.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Earlier comments</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {decisions.map((decision) => (
              <div key={decision.id}>
                <p className="font-medium">
                  {decision.decision} · {decision.reason}
                </p>
                {(decision.annotations ?? []).map((note) => (
                  <p key={`${decision.id}-${note.section}-${note.comment}`}>
                    {note.section}: {note.comment}
                  </p>
                ))}
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}

function ProfileForm({ eid, profile, disabled }: { eid: string; profile: ClientProfile; disabled: boolean }) {
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
      toast.success('Client profile saved. ACC-04 will run again.')
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Client profile</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <Field label="Legal name">
          <Input value={legalName} onChange={(event) => setLegalName(event.target.value)} />
        </Field>
        <Field label="ABN">
          <Input value={abn} onChange={(event) => setAbn(event.target.value)} />
        </Field>
        <Field label="Entity type">
          <Input value={entityType} onChange={(event) => setEntityType(event.target.value)} />
        </Field>
        <Field label="ANZSIC">
          <Input value={industry} onChange={(event) => setIndustry(event.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={listed} onChange={(event) => setListed(event.target.checked)} />
          Listed
        </label>
        <Field label="Directors, one per line">
          <Textarea value={directors} onChange={(event) => setDirectors(event.target.value)} />
        </Field>
        <Field label="Notes">
          <Textarea value={notes} onChange={(event) => setNotes(event.target.value)} />
        </Field>
        <Button
          type="button"
          variant="outline"
          disabled={disabled || save.isPending}
          onClick={() =>
            save.mutate({
              path: { eid },
              body: {
                version: (profile.version ?? 1) + 1,
                legal_name: legalName,
                abn,
                entity_type: entityType,
                industry_anzsic: industry,
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
          Save profile and rerun ACC-04
        </Button>
      </CardContent>
    </Card>
  )
}

function GateDecisionForm({ eid, view, disabled }: { eid: string; view: GateView; disabled: boolean }) {
  const queryClient = useQueryClient()
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
    view.material.kind === 'FIN-01' && FIN_OPINIONS.includes(view.material.schedule.candidate_opinion)
      ? view.material.schedule.candidate_opinion
      : 'qualified',
  )
  const [notes, setNotes] = useState<NoteDraft[]>([newNote()])
  const submit = useMutation({
    ...gatesSubmitGateDecisionMutation(),
    onSuccess: async () => {
      toast.success('Gate decision submitted')
      setReason('')
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  const send = (decision: GateVerdict) => {
    const body = buildDecision(view, decision, { reason, choice, levels, levelReasons, disposition, notes })
    if (typeof body === 'string') {
      toast.error(body)
      return
    }
    submit.mutate({ path: { eid }, body })
  }

  const uncovered = view.material.kind === 'PLN-01/03' && view.material.uncovered_objective_ids.length > 0
  const busy = disabled || submit.isPending

  return (
    <Card>
      <CardHeader>
        <CardTitle>{view.gate}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
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
        {view.material.kind === 'FIN-01' ? (
          <Field label="Opinion">
            <select
              className="h-8 rounded-lg border px-2 text-sm"
              value={disposition}
              onChange={(event) => setDisposition(event.target.value as OpinionType)}
            >
              {FIN_OPINIONS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
        ) : null}
        {view.material.kind === 'RPT-01' ? <AnnotationFields notes={notes} onChange={setNotes} /> : null}
        <Field label="Reason">
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} />
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button type="button" disabled={busy || uncovered} onClick={() => send('approve')}>
            {view.gate === 'RPT-01' ? 'Sign and archive' : 'Approve'}
          </Button>
          <Button type="button" variant="outline" disabled={busy} onClick={() => send('reject')}>
            {view.gate === 'ACC-01' ? 'Decline' : 'Reject'}
          </Button>
          <Button type="button" variant="outline" disabled={busy} onClick={() => send('insufficient_info')}>
            Insufficient information
          </Button>
        </div>
      </CardContent>
    </Card>
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
  const selected = candidates.candidates.find((item) => item.basis === choice.basis)
  const range = candidates.performance_pct_range ?? [0.5, 0.75]
  const overall = selected ? Math.round(selected.base_amount_cents * choice.pct) : null
  return (
    <div className="grid gap-3">
      {candidates.candidates.map((item) => (
        <p key={item.basis} className="text-sm">
          {item.basis}: base {formatAud(item.base_amount_cents)}, range {formatPct(item.pct_low)}–
          {formatPct(item.pct_high)} ({formatAud(item.low_cents)}–{formatAud(item.high_cents)})
        </p>
      ))}
      <Field label="Basis">
        <select
          className="h-8 rounded-lg border px-2 text-sm"
          value={choice.basis}
          onChange={(event) => {
            const basis = event.target.value as MaterialityChoice['basis']
            const next = candidates.candidates.find((item) => item.basis === basis)
            onChoice({ ...choice, basis, pct: next?.pct_high ?? choice.pct })
          }}
        >
          {candidates.candidates.map((item) => (
            <option key={item.basis} value={item.basis}>
              {item.basis}
            </option>
          ))}
        </select>
      </Field>
      <Field
        label={`Overall materiality percentage (${formatPct(selected?.pct_low)}–${formatPct(selected?.pct_high)})`}
      >
        <Input
          type="number"
          step="0.001"
          value={choice.pct}
          onChange={(event) => onChoice({ ...choice, pct: Number(event.target.value) })}
        />
      </Field>
      <Field label={`Performance materiality percentage (${formatPct(range[0])}–${formatPct(range[1])})`}>
        <Input
          type="number"
          step="0.01"
          value={choice.performance_pct}
          onChange={(event) => onChoice({ ...choice, performance_pct: Number(event.target.value) })}
        />
      </Field>
      <Field label="Clearly trivial percentage">
        <Input
          type="number"
          step="0.01"
          value={choice.clearly_trivial_pct}
          onChange={(event) => onChoice({ ...choice, clearly_trivial_pct: Number(event.target.value) })}
        />
      </Field>
      <p className="text-sm">
        Preview overall {formatAud(overall)}
        {overall != null ? `, performance ${formatAud(Math.round(overall * choice.performance_pct))}` : ''}
      </p>
      {risks.map((risk) => (
        <div key={risk.id} className="grid gap-2 rounded-md border p-3 text-sm">
          <p className="font-medium">
            {risk.id} · {risk.assertion}
            {risk.fraud_related ? ' · fraud' : ''}
          </p>
          <p>{risk.description}</p>
          <select
            className="h-8 rounded-lg border px-2"
            value={levels[risk.id] ?? risk.level}
            onChange={(event) => onLevel(risk.id, event.target.value as RiskLevel)}
          >
            {RISK_LEVELS.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
          {(levels[risk.id] ?? risk.level) !== risk.level ? (
            <Input
              placeholder="Reason for the level change"
              value={levelReasons[risk.id] ?? ''}
              onChange={(event) => onLevelReason(risk.id, event.target.value)}
            />
          ) : null}
        </div>
      ))}
    </div>
  )
}

function AnnotationFields({ notes, onChange }: { notes: NoteDraft[]; onChange: (notes: NoteDraft[]) => void }) {
  return (
    <div className="grid gap-3">
      {notes.map((note) => (
        <div key={note.id} className="grid gap-2">
          <select
            className="h-8 rounded-lg border px-2 text-sm"
            value={note.section}
            onChange={(event) =>
              onChange(
                notes.map((item) =>
                  item.id === note.id ? { ...item, section: event.target.value as ReportSection } : item,
                ),
              )
            }
          >
            {REPORT_SECTIONS.map((section) => (
              <option key={section} value={section}>
                {section}
              </option>
            ))}
          </select>
          <Input
            placeholder="Quoted text"
            value={note.quote ?? ''}
            onChange={(event) =>
              onChange(notes.map((item) => (item.id === note.id ? { ...item, quote: event.target.value } : item)))
            }
          />
          <Textarea
            placeholder="Comment"
            value={note.comment}
            onChange={(event) =>
              onChange(notes.map((item) => (item.id === note.id ? { ...item, comment: event.target.value } : item)))
            }
          />
        </div>
      ))}
      <Button type="button" variant="outline" onClick={() => onChange([...notes, newNote()])}>
        Add comment
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
  },
): GateDecisionIn | string {
  const reason = input.reason.trim()
  const base: GateDecisionIn = {
    gate: view.gate,
    target_id: view.target_id,
    target_version: view.target_version,
    decision,
    reason: reason || null,
  }
  if (decision !== 'approve' && !reason) return 'A reason is required'
  if (view.material.kind === 'FIN-01' && decision === 'approve' && !reason) return 'FIN-01 approval needs a reason'
  if (view.material.kind === 'PLN-01/03' && decision === 'approve') {
    if (!input.choice) return 'Choose a materiality basis'
    const edits: GateEdit[] = view.material.risks.flatMap((risk) => {
      const level = input.levels[risk.id] ?? risk.level
      if (level === risk.level) return []
      const editReason = input.levelReasons[risk.id]?.trim()
      if (!editReason) return []
      return [
        { target_id: risk.id, target_version: risk.version ?? 1, field: 'level', new_value: level, reason: editReason },
      ]
    })
    const missing = view.material.risks.find(
      (risk) => (input.levels[risk.id] ?? risk.level) !== risk.level && !input.levelReasons[risk.id]?.trim(),
    )
    if (missing) return `Give a reason for changing ${missing.id}`
    return { ...base, materiality: input.choice, edits }
  }
  if (view.material.kind === 'FIN-01' && decision === 'approve') return { ...base, disposition: input.disposition }
  if (view.material.kind === 'RPT-01' && decision === 'reject') {
    const annotations = input.notes
      .map((note) => ({ section: note.section, quote: note.quote?.trim() || null, comment: note.comment.trim() }))
      .filter((note) => note.comment)
    if (!annotations.length) return 'Add at least one comment'
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

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 text-sm">
      <span className="font-medium">{label}</span>
      {children}
    </div>
  )
}
