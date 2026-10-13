import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { reportsGetMisstatementsOptions } from '@/client/@tanstack/react-query.gen'
import { QueryState } from '@/components/query-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { formatAud } from '@/lib/money'

export const Route = createFileRoute('/engagements/$eid/misstatements')({ component: Misstatements })

function Misstatements() {
  const { eid } = Route.useParams()
  const report = useQuery(reportsGetMisstatementsOptions({ path: { eid } }))
  return (
    <div className="space-y-4">
      <h1 className="font-heading text-2xl font-semibold">Misstatements</h1>
      <QueryState isPending={report.isPending} error={report.error}>
        {report.data ? (
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>Schedule</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1 text-sm">
                <p>Overall materiality: {formatAud(report.data.materiality_cents)}</p>
                <p>Confirmed opinion: {report.data.opinion_route ?? '—'}</p>
                <p>Candidate opinion: {report.data.schedule?.candidate_opinion ?? '—'}</p>
                <p>Uncorrected total: {formatAud(report.data.schedule?.total_cents)}</p>
              </CardContent>
            </Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Assertion</TableHead>
                  <TableHead>Amount</TableHead>
                  <TableHead>Direction</TableHead>
                  <TableHead>Type</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.data.items.map((item) => (
                  <TableRow key={item.id}>
                    <TableCell>{item.account}</TableCell>
                    <TableCell>{item.assertion}</TableCell>
                    <TableCell>{formatAud(item.amount_cents)}</TableCell>
                    <TableCell>{item.direction}</TableCell>
                    <TableCell>{item.type}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : null}
      </QueryState>
    </div>
  )
}
