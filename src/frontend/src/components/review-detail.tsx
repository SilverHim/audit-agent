import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { reviewsSubmitReviewRecordMutation } from '@/client/@tanstack/react-query.gen'
import type {
  Assertion,
  AuditException,
  Decision,
  Direction,
  Judgment,
  MisstatementType,
  ProcedureRun,
  ReviewRecordIn,
  ReviewTaskView,
} from '@/client/types.gen'
import { PointerList } from '@/components/evidence-link'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { audToCents, formatAud } from '@/lib/money'

const ASSERTIONS: Assertion[] = ['Occurrence', 'Completeness', 'Accuracy', 'Cutoff', 'Existence', 'Valuation', 'Rights']
const MISSTATEMENT_TYPES: MisstatementType[] = ['factual', 'judgmental', 'projected']
const DIRECTIONS: Direction[] = ['overstated', 'understated']

export function isJudgment(target: ReviewTaskView['target']): target is Judgment {
  return 'conclusion' in target
}

export function isRun(target: ReviewTaskView['target']): target is ProcedureRun {
  return 'run_no' in target && !('conclusion' in target)
}

export function ReviewDetail({ eid, item, disabled }: { eid: string; item: ReviewTaskView; disabled: boolean }) {
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>
            {item.procedure_name} · {item.task.kind}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          <TargetBody eid={eid} item={item} />
        </CardContent>
      </Card>
      <ReviewForm eid={eid} item={item} disabled={disabled} />
    </div>
  )
}

function TargetBody({ eid, item }: { eid: string; item: ReviewTaskView }) {
  const target = item.target
  if (isJudgment(target)) {
    return (
      <>
        <p className="whitespace-pre-wrap">{target.conclusion}</p>
        <p>Confidence {target.confidence.toFixed(2)}</p>
        <p className="text-muted-foreground">
          evidence {target.confidence_parts.evidence ?? '—'} · rule {target.confidence_parts.rule ?? '—'} · llm{' '}
          {target.confidence_parts.llm ?? '—'}
        </p>
        <p className="whitespace-pre-wrap">{target.counter_argument}</p>
        <ol className="space-y-3">
          {target.reasoning_chain.map((step) => (
            <li key={`${step.kind}:${step.text}`}>
              <p className="font-medium">
                {step.kind}
                {step.status ? ` · ${step.status}` : ''}
              </p>
              <p className="whitespace-pre-wrap">{step.text}</p>
              {step.from_steps?.length ? (
                <p className="text-muted-foreground">From steps {step.from_steps.join(', ')}</p>
              ) : null}
              <PointerList eid={eid} pointers={step.evidence} />
            </li>
          ))}
        </ol>
        <PointerList eid={eid} pointers={target.evidence} />
        {target.misstatement_draft ? (
          <p>
            Suggested misstatement: {target.misstatement_draft.account} {target.misstatement_draft.assertion}{' '}
            {formatAud(target.misstatement_draft.amount_cents)} {target.misstatement_draft.direction}
          </p>
        ) : null}
      </>
    )
  }
  if (isRun(target)) {
    return (
      <>
        <p>
          Run {target.run_no} · {target.status}
        </p>
        <p>{target.failure_reason || 'No failure reason recorded.'}</p>
      </>
    )
  }
  const exception = target as AuditException
  return (
    <>
      <p>
        {exception.rule_id} · {exception.severity} · {formatAud(exception.amount_cents)}
      </p>
      <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">{JSON.stringify(exception.detail, null, 2)}</pre>
      <PointerList eid={eid} pointers={exception.evidence} />
    </>
  )
}

function ReviewForm({ eid, item, disabled }: { eid: string; item: ReviewTaskView; disabled: boolean }) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [conclusion, setConclusion] = useState('')
  const [account, setAccount] = useState(isJudgment(item.target) ? (item.target.misstatement_draft?.account ?? '') : '')
  const [assertion, setAssertion] = useState<Assertion>(
    isJudgment(item.target) ? (item.target.misstatement_draft?.assertion ?? 'Cutoff') : 'Cutoff',
  )
  const [amount, setAmount] = useState(
    isJudgment(item.target) && item.target.misstatement_draft
      ? String(item.target.misstatement_draft.amount_cents / 100)
      : '',
  )
  const [direction, setDirection] = useState<Direction>(
    isJudgment(item.target) ? (item.target.misstatement_draft?.direction ?? 'overstated') : 'overstated',
  )
  const [kind, setKind] = useState<MisstatementType>(
    isJudgment(item.target) ? (item.target.misstatement_draft?.type ?? 'factual') : 'factual',
  )
  const [params, setParams] = useState('')
  const submit = useMutation({
    ...reviewsSubmitReviewRecordMutation(),
    onSuccess: async () => {
      toast.success('Review decision submitted')
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  const allowed = new Set(item.allowed_decisions)
  const busy = disabled || item.task.status !== 'pending' || submit.isPending

  const send = (decision: Decision) => {
    const body = buildRecord(item, decision, {
      reason,
      conclusion,
      account,
      assertion,
      amount,
      direction,
      kind,
      params,
    })
    if (typeof body === 'string') {
      toast.error(body)
      return
    }
    submit.mutate({ path: { eid }, body })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Decision</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        {item.task.status !== 'pending' ? (
          <p className="text-muted-foreground text-sm">This task is already decided.</p>
        ) : null}
        <div className="grid gap-1 text-sm">
          <span className="font-medium">Reason</span>
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} />
        </div>
        {allowed.has('override') || allowed.has('manual_result') ? (
          <div className="grid gap-1 text-sm">
            <span className="font-medium">Conclusion</span>
            <Textarea value={conclusion} onChange={(event) => setConclusion(event.target.value)} />
          </div>
        ) : null}
        {allowed.has('misstatement') ? (
          <div className="grid gap-2">
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Account</span>
              <Input value={account} onChange={(event) => setAccount(event.target.value)} />
            </div>
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Type</span>
              <select
                className="h-8 rounded-lg border px-2 text-sm"
                value={kind}
                onChange={(event) => setKind(event.target.value as MisstatementType)}
              >
                {MISSTATEMENT_TYPES.map((itemType) => (
                  <option key={itemType} value={itemType}>
                    {itemType}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Assertion</span>
              <select
                className="h-8 rounded-lg border px-2 text-sm"
                value={assertion}
                onChange={(event) => setAssertion(event.target.value as Assertion)}
              >
                {ASSERTIONS.map((itemAssertion) => (
                  <option key={itemAssertion} value={itemAssertion}>
                    {itemAssertion}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Amount (AUD)</span>
              <Input value={amount} onChange={(event) => setAmount(event.target.value)} />
            </div>
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Direction</span>
              <select
                className="h-8 rounded-lg border px-2 text-sm"
                value={direction}
                onChange={(event) => setDirection(event.target.value as Direction)}
              >
                {DIRECTIONS.map((itemDirection) => (
                  <option key={itemDirection} value={itemDirection}>
                    {itemDirection}
                  </option>
                ))}
              </select>
            </div>
          </div>
        ) : null}
        {allowed.has('rerun') ? (
          <div className="grid gap-1 text-sm">
            <span className="font-medium">Rerun parameters (JSON, optional)</span>
            <Textarea value={params} onChange={(event) => setParams(event.target.value)} />
          </div>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {item.allowed_decisions.map((decision) => (
            <Button
              key={decision}
              type="button"
              variant={decision === 'confirm' ? 'default' : 'outline'}
              disabled={busy}
              onClick={() => send(decision)}
            >
              {decision}
            </Button>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

function buildRecord(
  item: ReviewTaskView,
  decision: Decision,
  input: {
    reason: string
    conclusion: string
    account: string
    assertion: Assertion
    amount: string
    direction: Direction
    kind: MisstatementType
    params: string
  },
): ReviewRecordIn | string {
  const reason = input.reason.trim()
  const base: ReviewRecordIn = {
    task_id: item.task.id,
    target_version: item.task.target_version,
    decision,
    reason: reason || null,
  }
  if (decision !== 'confirm' && !reason) return 'A reason is required'
  if (decision === 'override') {
    if (!input.conclusion.trim()) return 'A conclusion is required'
    return { ...base, override_conclusion: input.conclusion.trim() }
  }
  if (decision === 'manual_result') {
    if (!input.conclusion.trim()) return 'A conclusion is required'
    return { ...base, manual_result: { conclusion: input.conclusion.trim(), evidence: [] } }
  }
  if (decision === 'misstatement') {
    const cents = audToCents(input.amount)
    if (cents == null || !input.account.trim()) return 'Account and amount are required'
    return {
      ...base,
      misstatement_draft: {
        type: input.kind,
        account: input.account.trim(),
        assertion: input.assertion,
        amount_cents: cents,
        direction: input.direction,
      },
    }
  }
  if (decision === 'rerun' && input.params.trim()) {
    try {
      const parsed: unknown = JSON.parse(input.params)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
        return 'Rerun parameters must be a JSON object'
      return { ...base, rerun_params: parsed as Record<string, unknown> }
    } catch {
      return 'Rerun parameters are not valid JSON'
    }
  }
  return base
}
