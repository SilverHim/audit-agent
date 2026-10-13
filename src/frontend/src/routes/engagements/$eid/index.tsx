import { createFileRoute, Link } from '@tanstack/react-router'
import { PhaseTrack, phaseLabel, StatusBadge } from '@/components/phase-track'
import { QueryState } from '@/components/query-state'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { decisionsDisabled } from '@/lib/engagement'
import { formatAud } from '@/lib/money'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/')({ component: Overview })

function Overview() {
  const { eid } = Route.useParams()
  const overview = useEngagement(eid)
  const data = overview.data
  return (
    <QueryState isPending={overview.isPending} error={overview.error}>
      {data ? (
        <div className="space-y-6">
          <PhaseTrack phase={data.engagement.phase} />
          <div className="grid gap-2 text-sm sm:grid-cols-2">
            <p>Phase: {phaseLabel(data.engagement.phase)}</p>
            <p>
              Gate:{' '}
              {data.current_gate ? (
                <Link to="/engagements/$eid/gate" params={{ eid }} className="underline">
                  {data.current_gate}
                </Link>
              ) : (
                '—'
              )}
            </p>
            <p>Overall materiality: {formatAud(data.materiality_cents)}</p>
            <p>Performance materiality: {formatAud(data.performance_materiality_cents)}</p>
            <p>Clearly trivial: {formatAud(data.clearly_trivial_cents)}</p>
            <p>Opinion: {data.opinion_route ?? '—'}</p>
            <p>
              Pending reviews:{' '}
              <Link to="/engagements/$eid/reviews" params={{ eid }} className="underline">
                {data.pending_tasks}
              </Link>
            </p>
            <p>{data.awaiting_review ? 'Waiting for a review decision.' : 'Not waiting on the review queue.'}</p>
            <p>{decisionsDisabled(data) ? 'Decisions are disabled.' : 'Decisions are open.'}</p>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Procedure</TableHead>
                <TableHead>Phase</TableHead>
                <TableHead>Engine</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Pending</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.procedures.map((procedure) => (
                <TableRow key={procedure.id}>
                  <TableCell>
                    <Link
                      to="/engagements/$eid/procedures/$pid"
                      params={{ eid, pid: procedure.id }}
                      className="underline"
                    >
                      {procedure.id}
                    </Link>
                    <p className="text-muted-foreground text-xs">{procedure.name}</p>
                  </TableCell>
                  <TableCell>{phaseLabel(procedure.phase)}</TableCell>
                  <TableCell>
                    {procedure.engine} · {procedure.autonomy_level}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={procedure.status} />
                  </TableCell>
                  <TableCell>{procedure.pending_tasks}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : null}
    </QueryState>
  )
}
