import { useMutation, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link, Outlet, redirect } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { engagementsResumeEngagementMutation } from '@/client/@tanstack/react-query.gen'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { getToken } from '@/lib/auth'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid')({
  beforeLoad: () => {
    if (!getToken()) throw redirect({ to: '/login' })
  },
  component: EngagementLayout,
})

const NAV = [
  { to: '/engagements/$eid', label: 'Overview', exact: true },
  { to: '/engagements/$eid/gate', label: 'Gate' },
  { to: '/engagements/$eid/reviews', label: 'Review queue' },
  { to: '/engagements/$eid/evidence', label: 'Evidence' },
  { to: '/engagements/$eid/contracts', label: 'Contracts' },
  { to: '/engagements/$eid/misstatements', label: 'Misstatements' },
  { to: '/engagements/$eid/report', label: 'Report' },
  { to: '/engagements/$eid/trail', label: 'Audit trail' },
] as const

function EngagementLayout() {
  const { eid } = Route.useParams()
  const queryClient = useQueryClient()
  const overview = useEngagement(eid)
  const wasRunning = useRef(false)
  const resume = useMutation({
    ...engagementsResumeEngagementMutation(),
    onSuccess: async () => {
      toast.success('Resume started')
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  useEffect(() => {
    if (!overview.data) return
    if (wasRunning.current && !overview.data.running) void invalidateEngagement(queryClient, eid)
    wasRunning.current = overview.data.running
  }, [eid, overview.data, queryClient])

  const data = overview.data
  return (
    <div className="space-y-6">
      <div>
        <p className="text-muted-foreground text-sm">{eid}</p>
        <h1 className="font-heading font-semibold text-xl">{data?.client_name ?? 'Engagement'}</h1>
      </div>
      {data?.running ? (
        <p className="text-sm">The engagement is running. Decisions stay disabled until it stops.</p>
      ) : null}
      {data?.run_error ? (
        <Alert variant="destructive">
          <AlertTitle>Run error</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-3">
            <span>{data.run_error}</span>
            <Button
              type="button"
              variant="outline"
              disabled={resume.isPending}
              onClick={() => resume.mutate({ path: { eid } })}
            >
              Resume
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {data?.engagement.locked ? <p className="text-sm">This engagement is read-only.</p> : null}
      <nav className="flex flex-wrap gap-4 border-b pb-2 text-sm">
        {NAV.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            params={{ eid }}
            activeOptions={{ exact: 'exact' in item }}
            className="text-muted-foreground hover:text-foreground"
            activeProps={{ className: 'text-foreground font-medium' }}
          >
            {item.label}
            {item.to.endsWith('/reviews') && data ? ` (${data.pending_tasks})` : ''}
          </Link>
        ))}
      </nav>
      <Outlet />
    </div>
  )
}
