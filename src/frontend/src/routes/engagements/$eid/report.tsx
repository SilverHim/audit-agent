import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { reportsGetReportsOptions } from '@/client/@tanstack/react-query.gen'
import { ReportPreview, RptSignForm } from '@/components/gate-form'
import { QueryState } from '@/components/query-state'
import { decisionsDisabled } from '@/lib/engagement'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/report')({ component: Report })

function Report() {
  const { eid } = Route.useParams()
  const overview = useEngagement(eid)
  const reports = useQuery(reportsGetReportsOptions({ path: { eid } }))
  const current = reports.data?.versions.at(-1)
  return (
    <div className="space-y-6">
      <h1 className="font-heading text-2xl font-semibold">Report</h1>
      <QueryState isPending={reports.isPending} error={reports.error}>
        {reports.data ? (
          <div className="space-y-6">
            {current ? (
              <ReportPreview
                report={current}
                decisions={reports.data.decisions.filter((item) => item.decision === 'reject')}
              />
            ) : (
              <p className="text-muted-foreground text-sm">No report yet.</p>
            )}
            <div className="space-y-2 text-sm">
              <h2 className="font-medium">Versions</h2>
              {reports.data.versions.map((version) => (
                <p key={version.id}>
                  Revision {version.revision} · {version.opinion_type} · {version.status}
                  {version.report_date ? ` · ${version.report_date}` : ''}
                </p>
              ))}
            </div>
          </div>
        ) : null}
      </QueryState>
      {overview.data?.current_gate === 'RPT-01' ? (
        <RptSignForm eid={eid} disabled={decisionsDisabled(overview.data)} />
      ) : null}
    </div>
  )
}
