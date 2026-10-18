import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { MessageCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import { reviewsListExceptionsOptions, reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { buttonVariants } from '@/components/ui/button'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import { deferralKey, useDeferrals } from '@/lib/new-api'
import { isJudgment, procedureName, rankTasks } from '@/lib/review'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/decisions')({
  component: Decisions,
})

type Filter = 'all' | 'pending' | 'deferred'
const COLS =
  'grid grid-cols-[minmax(0,2.2fr)_minmax(0,1.3fr)_minmax(0,1.1fr)_minmax(0,1fr)_minmax(0,0.9fr)_minmax(0,1.2fr)_88px] gap-3'

function Decisions() {
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const overview = useEngagement(eid).data
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid }, query: { status: 'pending' } }))
  const exceptions = useQuery(reviewsListExceptionsOptions({ path: { eid } }))
  const deferrals = useDeferrals(eid)
  const [filter, setFilter] = useState<Filter>('all')

  const deferred = useMemo(() => new Set((deferrals.data ?? []).map(deferralKey)), [deferrals.data])
  const ranked = useMemo(() => {
    const byId = new Map((exceptions.data ?? []).map((item) => [item.id, item]))
    return rankTasks(tasks.data ?? [], byId, overview)
  }, [tasks.data, exceptions.data, overview])

  const gateKey = overview?.current_gate ? `gate:${overview.current_gate}` : null
  const gateState = gateKey ? (deferred.has(gateKey) ? 'deferred' : 'pending') : null
  const rows = ranked.map((item) => ({
    item,
    state: deferred.has(`task:${item.view.task.id}`) ? 'deferred' : 'pending',
  }))
  const visible = (state: string) => filter === 'all' || filter === state
  const counts = {
    all: rows.length + (gateState ? 1 : 0),
    pending: rows.filter((row) => row.state === 'pending').length + (gateState === 'pending' ? 1 : 0),
    deferred: rows.filter((row) => row.state === 'deferred').length + (gateState === 'deferred' ? 1 : 0),
  }
  const shownRows = rows.filter((row) => visible(row.state))
  const statusPill = (state: string) => (
    <StatusPill tone={state === 'deferred' ? 'neutral' : 'warn'}>
      {state === 'deferred' ? t.status.deferred : t.status.pending}
    </StatusPill>
  )

  return (
    <>
      <PageHeader
        title={t.decisionsPage.title}
        actions={
          <Link to="/engagements/$eid" params={{ eid }} search={{}} className={buttonVariants({ size: 'lg' })}>
            <MessageCircle className="size-4" aria-hidden="true" />
            {t.decisionsPage.workThrough}
          </Link>
        }
      />
      <section className="flex flex-col gap-4 px-4 py-5 sm:px-7">
        <fieldset
          aria-label={t.decisionsPage.title}
          className="inline-flex self-start rounded-[10px] bg-[#e9ecf1] p-[3px]"
        >
          {(['all', 'pending', 'deferred'] as const).map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={filter === item}
              onClick={() => setFilter(item)}
              className={cn(
                'min-h-8 rounded-lg px-3 text-[13px]',
                filter === item ? 'bg-card font-semibold shadow-xs' : 'font-medium',
              )}
            >
              {t.decisionsPage[item]} · {counts[item]}
            </button>
          ))}
        </fieldset>

        <QueryState isPending={tasks.isPending} error={tasks.error}>
          <div className="overflow-x-auto rounded-xl border bg-card">
            <div className="min-w-[880px]">
              <div
                className={cn(
                  COLS,
                  'border-b bg-[#fafbfc] px-4 py-2.5 font-semibold text-[12px] text-muted-foreground',
                )}
              >
                <span>{t.decisionsPage.cols.item}</span>
                <span>{t.decisionsPage.cols.procedure}</span>
                <span className="text-right">{t.decisionsPage.cols.amount}</span>
                <span>{t.decisionsPage.cols.proposed}</span>
                <span>{t.decisionsPage.cols.tier}</span>
                <span>{t.decisionsPage.cols.status}</span>
                <span />
              </div>

              <GroupHeader title={t.decisionsPage.stage} count={gateState && visible(gateState) ? 1 : 0} />
              {overview?.current_gate && gateState && visible(gateState) ? (
                <div className={cn(COLS, 'min-h-14 items-center border-line-soft border-b px-4 py-3')}>
                  <span className="flex min-w-0 flex-col">
                    <Code className="font-semibold">{overview.current_gate}</Code>
                    <span className="truncate text-[12.5px] text-[#4a5262]">{t.gates[overview.current_gate]}</span>
                  </span>
                  <span className="text-[13px]">{t.phases[overview.engagement.phase]}</span>
                  <span className="text-right">—</span>
                  <span>—</span>
                  <span>—</span>
                  <span>{statusPill(gateState)}</span>
                  <OpenLink eid={eid} focus={gateKey ?? undefined} label={t.decisionsPage.open} />
                </div>
              ) : (
                <EmptyRow text={t.decisionsPage.noStage} />
              )}

              <GroupHeader title={t.decisionsPage.review} count={shownRows.length} />
              {shownRows.length ? (
                shownRows.map(({ item, state }) => {
                  const judgment = isJudgment(item.view.target) ? item.view.target : null
                  return (
                    <div
                      key={item.view.task.id}
                      className={cn(COLS, 'min-h-14 items-center border-line-soft border-b px-4 py-3')}
                    >
                      <span className="flex min-w-0 flex-col">
                        <Code className="font-semibold">{item.subject}</Code>
                        {t.taskKinds[item.view.task.kind] ? (
                          <span className="truncate text-[12.5px] text-[#4a5262]">
                            {t.taskKinds[item.view.task.kind]}
                          </span>
                        ) : null}
                      </span>
                      <span className="text-[13px]">
                        <Code>{item.view.task.procedure_id}</Code>{' '}
                        {procedureName(t, item.view.task.procedure_id, item.view.procedure_name)}
                      </span>
                      <Code className="text-right text-[13px]">{formatAud(item.amount)}</Code>
                      <span
                        className={cn(
                          'text-[13px]',
                          judgment?.proposed_action === 'misstatement' && 'font-semibold text-risk',
                        )}
                      >
                        {judgment?.proposed_action ? t.proposed[judgment.proposed_action] : '—'}
                      </span>
                      <span className="text-[13px]">{t.tiers[item.tier]}</span>
                      <span>{statusPill(state)}</span>
                      <OpenLink eid={eid} focus={`task:${item.view.task.id}`} label={t.decisionsPage.open} />
                    </div>
                  )
                })
              ) : (
                <EmptyRow text={t.decisionsPage.none} />
              )}
            </div>
          </div>
        </QueryState>
      </section>
    </>
  )
}

function GroupHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-baseline gap-2 border-line-soft border-b px-4 pt-3 pb-2">
      <span className="font-semibold text-[13.5px]">{title}</span>
      <span className="flex-1" />
      <Code className="text-[12px] text-muted-foreground">{count}</Code>
    </div>
  )
}

function EmptyRow({ text }: { text: string }) {
  return <div className="border-line-soft border-b px-4 py-3.5 text-[13px] text-[#4a5262]">{text}</div>
}

function OpenLink({ eid, focus, label }: { eid: string; focus?: string; label: string }) {
  return (
    <Link
      to="/engagements/$eid"
      params={{ eid }}
      search={{ focus }}
      className="inline-flex min-h-9 items-center justify-self-end rounded-lg border border-input px-3 text-[13px] hover:bg-muted"
    >
      {label}
    </Link>
  )
}
