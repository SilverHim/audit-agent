import { Link } from '@tanstack/react-router'
import { Check, Circle, Loader2 } from 'lucide-react'
import type { EngagementOverview } from '@/client/types.gen'
import { StatusPill } from '@/components/chrome'
import { activityText, currentActivities } from '@/lib/activity'
import { STAGES, workbenchState } from '@/lib/execution'
import { useI18n } from '@/lib/i18n'

export function WorkbenchStatus({
  eid,
  overview,
  openEvidence,
}: {
  eid: string
  overview: EngagementOverview
  openEvidence: number
}) {
  const { t } = useI18n()
  const c = t.workbench
  const state = workbenchState(overview, openEvidence)
  const current =
    overview.engagement.phase === 'Archived'
      ? STAGES.length
      : STAGES.indexOf(overview.engagement.phase as (typeof STAGES)[number])
  const defaultNext = {
    running: c.nextRunning,
    human: c.nextHuman,
    external: c.nextExternal,
    failed: c.nextFailed,
    idle: c.nextIdle,
    done: c.nextDone,
  }[state]
  const next =
    state === 'human' && overview.current_gate && overview.current_gate in c.gateNext
      ? c.gateNext[overview.current_gate as keyof typeof c.gateNext]
      : defaultNext
  const activity = currentActivities(overview)[0]
  return (
    <section className="border-b bg-card px-4 py-4 sm:px-7" aria-label={c.stages}>
      <ol className="m-0 mb-4 grid list-none grid-cols-5 gap-1 p-0">
        {STAGES.map((stage, index) => (
          <li key={stage}>
            <Link
              to={
                index === 4
                  ? '/engagements/$eid/report'
                  : index === 2
                    ? '/engagements/$eid/procedures'
                    : index === 1
                      ? '/engagements/$eid/plan'
                      : '/engagements/$eid/decisions'
              }
              params={{ eid }}
              aria-current={index === current ? 'step' : undefined}
              className={`flex min-h-11 flex-col items-center justify-center gap-1 rounded-lg px-1 py-2 text-center text-xs sm:flex-row sm:gap-2 sm:text-sm ${index === current ? 'bg-accent font-semibold text-primary' : index < current ? 'text-success-strong' : 'text-muted-foreground hover:bg-muted'}`}
            >
              {index < current ? (
                <Check className="size-4 shrink-0" aria-hidden="true" />
              ) : (
                <Circle className="size-3 shrink-0" aria-hidden="true" />
              )}
              {c.stagesNames[index]}
            </Link>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap items-center gap-2.5" role="status">
        <StatusPill tone={state === 'failed' ? 'risk' : state === 'human' || state === 'external' ? 'warn' : 'accent'}>
          {state === 'running' ? <Loader2 className="size-3.5 animate-spin" aria-hidden="true" /> : null}
          {c.status[state]}
        </StatusPill>
        <span className="text-sm">
          {activity
            ? activityText(activity, t)
            : overview.current_gate
              ? t.gates[overview.current_gate]
              : t.phases[overview.engagement.phase]}
        </span>
        {overview.progress.total > 0 ? (
          <span className="text-muted-foreground text-xs">
            {c.progress(overview.progress.done, overview.progress.total)}
          </span>
        ) : null}
      </div>
      <p className="m-0 mt-2 text-muted-foreground text-sm">
        <span className="font-medium text-foreground">{c.next}：</span>
        {next}
      </p>
      {openEvidence > 0 ? (
        <Link
          to="/engagements/$eid/data"
          params={{ eid }}
          search={{}}
          className="mt-2 inline-block text-primary text-xs hover:underline"
        >
          {c.openEvidence(openEvidence)}
        </Link>
      ) : null}
    </section>
  )
}
