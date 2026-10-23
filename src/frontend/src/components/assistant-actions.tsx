import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { CheckCircle2, Loader2, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import {
  assistantActionsListAssistantPlansOptions,
  reviewsListReviewTasksOptions,
  usersMeOptions,
} from '@/client/@tanstack/react-query.gen'
import {
  assistantActionsCancelAssistantAction,
  assistantActionsExecuteAssistantAction,
  assistantActionsProposeAssistantAction,
} from '@/client/sdk.gen'
import type { AssistantPlan } from '@/client/types.gen'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { decisionsDisabled, invalidateEngagement } from '@/lib/engagement'
import { errorText } from '@/lib/errors'
import { useEvidenceRequests } from '@/lib/evidence-api'
import { useI18n } from '@/lib/i18n'
import { useOverview } from '@/lib/use-engagement'

export function AssistantActions({ eid }: { eid: string }) {
  const { t, formatDate } = useI18n()
  const c = t.workbench
  const queryClient = useQueryClient()
  const plans = useQuery(assistantActionsListAssistantPlansOptions({ path: { eid } }))
  const reviewTasks = useQuery(reviewsListReviewTasksOptions({ path: { eid }, query: { status: 'pending' } }))
  const evidence = useEvidenceRequests(eid)
  const overview = useOverview(eid)
  const disabled = decisionsDisabled(overview)
  const open = (evidence.data ?? []).filter((item) => item.status === 'open')
  const [selected, setSelected] = useState<string[]>([])
  const propose = useMutation({
    mutationFn: async (body?: {
      action: AssistantPlan['action']
      target_kind: 'review_task' | 'procedure'
      target_ids?: string[]
    }) =>
      (
        await assistantActionsProposeAssistantAction({
          path: { eid },
          body: body ?? {
            action: 'evidence_unavailable',
            target_kind: 'evidence_request',
            target_ids: selected.length ? selected : open.map((r) => r.id),
          },
          throwOnError: true,
        })
      ).data,
    onSuccess: () => {
      setSelected([])
      void invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(errorText(error, t)),
  })
  const confirmIds = (reviewTasks.data ?? [])
    .filter(
      (item) =>
        item.task.kind === 'normal' &&
        'proposed_action' in item.target &&
        item.target.proposed_action === 'accept' &&
        item.allowed_decisions.includes('confirm'),
    )
    .map((item) => item.task.id)
  const misstatementIds = (reviewTasks.data ?? [])
    .filter(
      (item) =>
        item.task.kind === 'normal' &&
        'proposed_action' in item.target &&
        item.target.proposed_action === 'misstatement' &&
        item.allowed_decisions.includes('misstatement'),
    )
    .map((item) => item.task.id)
  const pending = (plans.data ?? []).filter((plan) => plan.status === 'proposed')
  const completed = (plans.data ?? []).filter((plan) => plan.status === 'completed')
  return (
    <section className="rounded-xl border bg-card p-4" aria-label={c.batch}>
      <h2 className="m-0 flex items-center gap-2 font-semibold text-sm">
        <Sparkles className="size-4 text-primary" aria-hidden="true" />
        {c.batch}
      </h2>
      {open.length ? (
        <div className="mt-3 rounded-lg bg-muted/40 p-3">
          <p className="m-0 mb-2 text-sm">{c.openEvidence(open.length)}</p>
          <label className="mb-2 flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={selected.length === open.length}
              disabled={disabled}
              onChange={(event) => setSelected(event.target.checked ? open.map((r) => r.id) : [])}
            />
            {c.selectAll}
          </label>
          <ul className="m-0 max-h-48 list-none space-y-2 overflow-auto p-0">
            {open.map((item) => (
              <li key={item.id}>
                <label className="flex items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    disabled={disabled}
                    checked={selected.includes(item.id)}
                    onChange={(event) =>
                      setSelected((previous) =>
                        event.target.checked ? [...previous, item.id] : previous.filter((id) => id !== item.id),
                      )
                    }
                  />
                  <span>
                    <span className="font-medium">{item.procedure_id}</span> · {item.description}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            disabled={disabled || propose.isPending}
            onClick={() => propose.mutate()}
          >
            {propose.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {c.prepare}
            {selected.length ? ` · ${c.selected(selected.length)}` : ''}
          </Button>
        </div>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        {overview?.can_rerun && (overview.stale_procedure_ids?.length ?? 0) > 0 ? (
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || propose.isPending}
            onClick={() => propose.mutate({ action: 'rerun_stale', target_kind: 'procedure' })}
          >
            {c.actions.rerun_stale}
          </Button>
        ) : null}
        {confirmIds.length ? (
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || propose.isPending}
            onClick={() =>
              propose.mutate({ action: 'confirm_reviews', target_kind: 'review_task', target_ids: confirmIds })
            }
          >
            {c.actions.confirm_reviews} · {confirmIds.length}
          </Button>
        ) : null}
        {misstatementIds.length ? (
          <Button
            variant="outline"
            size="sm"
            disabled={disabled || propose.isPending}
            onClick={() =>
              propose.mutate({
                action: 'recognize_misstatements',
                target_kind: 'review_task',
                target_ids: misstatementIds,
              })
            }
          >
            {c.actions.recognize_misstatements} · {misstatementIds.length}
          </Button>
        ) : null}
      </div>
      {plans.error ? (
        <p role="alert" className="text-destructive text-sm">
          {errorText(plans.error, t)}
        </p>
      ) : null}
      {!open.length && !pending.length ? <p className="mb-0 text-muted-foreground text-sm">{c.empty}</p> : null}
      {pending.map((plan) => (
        <ActionCard key={plan.id} eid={eid} plan={plan} disabled={disabled} />
      ))}
      {completed.length ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-muted-foreground text-xs">
            {c.receipts} · {completed.length}
          </summary>
          {completed
            .slice()
            .reverse()
            .map((plan, index) => (
              <details key={plan.id} className="mt-2" open={index === 0}>
                <summary className="cursor-pointer text-xs">
                  {c.actions[plan.action]} · {plan.targets.length} · {formatDate(plan.completed_at ?? plan.created_at)}
                </summary>
                <ActionCard eid={eid} plan={plan} disabled={disabled} />
              </details>
            ))}
        </details>
      ) : null}
    </section>
  )
}

function ActionCard({ eid, plan, disabled }: { eid: string; plan: AssistantPlan; disabled: boolean }) {
  const { t, formatDate } = useI18n()
  const c = t.workbench
  const queryClient = useQueryClient()
  const user = useQuery(usersMeOptions())
  const [reason, setReason] = useState(plan.reason ?? '')
  const [receipt, setReceipt] = useState(false)
  const execute = useMutation({
    mutationFn: () =>
      assistantActionsExecuteAssistantAction({
        path: { eid, plan_id: plan.id },
        body: { reason: reason.trim() || null },
        throwOnError: true,
      }),
    onSuccess: () => {
      setReceipt(true)
      toast.success(c.updated)
    },
    onError: (error) => toast.error(errorText(error, t)),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
  const cancel = useMutation({
    mutationFn: () => assistantActionsCancelAssistantAction({ path: { eid, plan_id: plan.id }, throwOnError: true }),
    onError: (error) => toast.error(errorText(error, t)),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
  const done = receipt || plan.status === 'completed'
  return (
    <article className="mt-3 rounded-lg border p-3" aria-label={c.actions[plan.action]}>
      <div className="flex items-center gap-2 font-medium text-sm">
        {done ? <CheckCircle2 className="size-4 text-success-strong" /> : null}
        {done ? c.receipt : c.actions[plan.action]} · {plan.targets.length}
      </div>
      <p className="my-2 text-muted-foreground text-xs">{c.scope}</p>
      <ul className="m-0 max-h-44 list-none space-y-1 overflow-y-auto p-0">
        {plan.targets.map((target) => (
          <li key={target.id} className="text-xs">
            <span className="font-medium">{target.procedure_id ?? c.kinds[target.kind]}</span> ·{' '}
            {target.label === target.id ? c.kinds[target.kind] : target.label}
            <span className="block break-all font-mono text-muted-foreground">
              {target.id}
              {target.version ? ` · v${target.version}` : ''}
            </span>
            {target.conclusion ? <p className="whitespace-pre-wrap">{target.conclusion}</p> : null}
            {target.kind === 'review_task' ? (
              <Link
                to="/engagements/$eid/reviews/$taskId"
                params={{ eid, taskId: target.id }}
                className="text-primary hover:underline"
              >
                {t.workspace.formView}
              </Link>
            ) : null}
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground text-xs">
        {plan.action === 'evidence_unavailable'
          ? c.impact
          : plan.action === 'rerun_stale'
            ? c.rerunImpact
            : ['confirm_reviews', 'recognize_misstatements'].includes(plan.action)
              ? c.reviewImpact
              : c.deferImpact}
      </p>
      {done ? (
        <p className="mb-0 text-muted-foreground text-xs">
          <span className="block">
            {c.authorizer} · {user.data?.id === plan.proposed_for ? user.data.full_name : plan.proposed_for}
          </span>
          {formatDate(plan.completed_at ?? plan.created_at)} · <span className="font-mono">{plan.id}</span>
          {plan.reason ? ` · ${plan.reason}` : ''}
        </p>
      ) : (
        <>
          <label className="mb-1 block text-xs" htmlFor={`reason-${plan.id}`}>
            {c.reason}
          </label>
          <Textarea
            id={`reason-${plan.id}`}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={2000}
            disabled={execute.isPending || cancel.isPending}
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={disabled || execute.isPending || cancel.isPending}
              onClick={() => execute.mutate()}
            >
              {execute.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
              {execute.isPending ? c.executing : c.authorize}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={execute.isPending || cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              {c.cancel}
            </Button>
          </div>
        </>
      )}
    </article>
  )
}
