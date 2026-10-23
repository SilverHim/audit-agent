import { Link } from '@tanstack/react-router'
import { CheckCircle2, CircleAlert, Loader2, Radio } from 'lucide-react'
import { useEffect, useState } from 'react'
import { reportsListTrail } from '@/client/sdk.gen'
import type { EngagementOverview } from '@/client/types.gen'
import { Button } from '@/components/ui/button'
import { executionRuns } from '@/lib/execution'
import { useI18n } from '@/lib/i18n'
import { procedureName } from '@/lib/review'
import { useExecution } from '@/lib/use-execution'

export function ExecutionPanel({ eid, overview }: { eid: string; overview?: EngagementOverview }) {
  const { t, formatDate } = useI18n()
  const c = t.workbench
  const { events, connected, prepend } = useExecution(eid)
  const runs = executionRuns(events)
  const [now, setNow] = useState(Date.now())
  const [loading, setLoading] = useState(false)
  const [exhausted, setExhausted] = useState(false)
  const [error, setError] = useState(false)
  useEffect(() => {
    if (!overview?.running) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [overview?.running])
  async function earlier() {
    setLoading(true)
    setError(false)
    try {
      const result = await reportsListTrail({
        path: { eid },
        query: {
          kind: ['execution', 'run', 'assistant_action'],
          newest: true,
          before: events[0]?.id ?? undefined,
          limit: 100,
        },
        throwOnError: true,
      })
      prepend(result.data)
      setExhausted(result.data.length < 100)
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }
  return (
    <section className="overflow-hidden rounded-xl border bg-card" aria-label={c.process}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
        <h2 className="m-0 font-semibold text-sm">{c.process}</h2>
        <span
          className={`inline-flex items-center gap-1.5 text-xs ${connected ? 'text-muted-foreground' : 'text-warn-strong'}`}
        >
          <Radio className="size-3" aria-hidden="true" />
          {connected ? c.live : c.reconnecting}
        </span>
      </div>
      <div className="max-h-[550px] overflow-y-auto px-4 py-3">
        {!runs.length ? <p className="text-muted-foreground text-sm">{c.noEvents}</p> : null}
        {runs.map((run, index) => (
          <details
            key={run.id}
            open={
              index < 2 ||
              Boolean(
                run.job
                  ? overview?.activities?.some((a) => a.kind === run.job)
                  : overview?.progress.running.includes(run.procedure),
              )
            }
            className="group border-line-soft border-b py-2.5 last:border-0"
          >
            <summary className="flex cursor-pointer list-none items-center gap-2 text-sm">
              {run.status === 'failed' ? (
                <CircleAlert className="size-4 shrink-0 text-destructive" />
              ) : run.status === 'succeeded' ? (
                <CheckCircle2 className="size-4 shrink-0 text-success-strong" />
              ) : (
                  run.job
                    ? overview?.activities?.some((a) => a.kind === run.job)
                    : overview?.progress.running.includes(run.procedure)
                ) ? (
                <Loader2 className="size-4 shrink-0 animate-spin text-primary" />
              ) : (
                <CircleAlert className="size-4 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1 font-medium">
                {run.job
                  ? (t.activity.kinds[run.job as keyof typeof t.activity.kinds]?.doing(t.activity.actors.A3, '') ??
                    run.job)
                  : `${run.procedure} ${procedureName(t, run.procedure)}`}
              </span>
              <span className="text-muted-foreground text-xs">
                {run.status === 'succeeded'
                  ? c.completed
                  : run.status === 'failed'
                    ? c.failed
                    : (
                          run.job
                            ? overview?.activities?.some((a) => a.kind === run.job)
                            : overview?.progress.running.includes(run.procedure)
                        )
                      ? c.running
                      : c.interrupted}
              </span>
            </summary>
            <ol className="m-0 mt-2 ml-2 list-none space-y-2 border-l pl-4">
              {run.entries.map((entry) => {
                const data = entry.payload ?? {}
                const running =
                  data.status === 'running' &&
                  run.status === 'running' &&
                  Boolean(
                    run.job
                      ? overview?.activities?.some((a) => a.kind === run.job)
                      : overview?.progress.running.includes(run.procedure),
                  )
                const stage = String(data.stage) as keyof typeof c.steps
                const seconds = running
                  ? Math.max(0, Math.floor((now - Date.parse(String(data.started_at))) / 1000))
                  : Math.floor(Number(data.elapsed_ms ?? 0) / 1000)
                return (
                  <li key={String(data.step_id)} className="flex items-start gap-2 text-xs">
                    {running ? (
                      <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-primary" />
                    ) : data.status === 'completed' ? (
                      <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success-strong" />
                    ) : (
                      <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warn-strong" />
                    )}
                    <div className="min-w-0 flex-1">
                      <span>{c.steps[stage] ?? stage}</span>
                      {data.current ? (
                        <span className="ml-2 break-all text-muted-foreground">{String(data.current)}</span>
                      ) : null}
                      {data.total != null ? (
                        <span className="ml-2 text-muted-foreground">
                          {String(data.done ?? 0)}/{String(data.total)}
                        </span>
                      ) : null}
                      {data.subject_id ? (
                        <span
                          title={String(data.subject_id)}
                          className="ml-2 inline-block max-w-36 truncate align-bottom font-mono text-muted-foreground"
                        >
                          {String(data.subject_id)}
                        </span>
                      ) : null}
                      {Number(data.attempt) > 1 ? (
                        <span className="ml-2 text-warn-strong">{c.attempt(Number(data.attempt))}</span>
                      ) : null}
                    </div>
                    <span className="shrink-0 text-muted-foreground">{c.elapsed(seconds)}</span>
                  </li>
                )
              })}
            </ol>
            {run.result ? (
              <p className="mb-0 mt-2 flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground text-xs">
                {Object.entries(c.summary).map(([key, label]) =>
                  typeof run.result?.[key] === 'number' ? (
                    <span key={key}>
                      {label} · {String(run.result[key])}
                    </span>
                  ) : null,
                )}
              </p>
            ) : null}
            {run.failure ? <p className="mb-0 mt-2 text-destructive text-xs">{run.failure}</p> : null}
            <div className="mt-2 flex justify-between gap-2 text-muted-foreground text-xs">
              <span>{run.at ? formatDate(run.at) : ''}</span>
              {run.job ? (
                <Link to="/engagements/$eid/data" params={{ eid }} search={{}} className="text-primary hover:underline">
                  {t.requests.open}
                </Link>
              ) : (
                <Link
                  to="/engagements/$eid/procedures"
                  params={{ eid }}
                  search={{ selected: run.procedure }}
                  className="text-primary hover:underline"
                >
                  {t.workspace.formView}
                </Link>
              )}
            </div>
          </details>
        ))}
      </div>
      {events.length && !exhausted ? (
        <div className="border-t px-4 py-2">
          <Button variant="ghost" size="sm" disabled={loading} onClick={() => void earlier()}>
            {loading ? <Loader2 className="size-3 animate-spin" /> : null}
            {error ? t.activity.retry : c.earlier}
          </Button>
        </div>
      ) : null}
    </section>
  )
}
