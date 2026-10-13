import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { reviewsGetProcedureOptions, reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import { PointerList } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatAud } from '@/lib/money'

export const Route = createFileRoute('/engagements/$eid/procedures/$pid')({ component: ProcedurePage })

function ProcedurePage() {
  const { eid, pid } = Route.useParams()
  const detail = useQuery(reviewsGetProcedureOptions({ path: { eid, pid } }))
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid } }))
  return (
    <QueryState isPending={detail.isPending} error={detail.error}>
      {detail.data ? (
        <div className="space-y-6">
          <div>
            <h1 className="font-heading text-2xl font-semibold">
              {detail.data.procedure.id} · {detail.data.name}
            </h1>
            <p className="text-muted-foreground text-sm">
              {detail.data.procedure.status} · depends on {detail.data.depends_on.join(', ') || 'nothing'} · standards{' '}
              {detail.data.standards.join(', ') || 'none'}
            </p>
          </div>
          <Card>
            <CardHeader>
              <CardTitle>Runs</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {detail.data.runs.map((run) => (
                <p key={run.id}>
                  {run.id} · {run.trigger} · {run.status} · {run.failure_reason ?? 'ok'}
                </p>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Exceptions</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {detail.data.exceptions.map((item) => (
                <div key={item.id}>
                  <p>
                    {item.id} · {item.severity} · {item.disposition} · {formatAud(item.amount_cents)}
                  </p>
                  <PointerList eid={eid} pointers={item.evidence} />
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Judgments</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {detail.data.judgments.map((judgment) => {
                const task = tasks.data?.find((item) => item.task.target_id === judgment.id)
                return (
                  <div key={judgment.id}>
                    <p className="font-medium">{judgment.conclusion}</p>
                    <p>
                      {judgment.review_status} · {judgment.proposed_action} · {judgment.subject_id}
                    </p>
                    {task ? (
                      <Link
                        to="/engagements/$eid/reviews/$taskId"
                        params={{ eid, taskId: task.task.id }}
                        className="underline"
                      >
                        Open review task
                      </Link>
                    ) : null}
                    <PointerList eid={eid} pointers={judgment.evidence} />
                  </div>
                )
              })}
            </CardContent>
          </Card>
        </div>
      ) : null}
    </QueryState>
  )
}
