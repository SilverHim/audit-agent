import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import { reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import type { TaskStatus } from '@/client/types.gen'
import { QueryState } from '@/components/query-state'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

export const Route = createFileRoute('/engagements/$eid/reviews/')({ component: Reviews })

function Reviews() {
  const { eid } = Route.useParams()
  const [status, setStatus] = useState<TaskStatus>('pending')
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid }, query: { status } }))
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="font-heading text-2xl font-semibold">Review queue</h1>
        <div className="flex gap-2">
          <Button
            type="button"
            variant={status === 'pending' ? 'default' : 'outline'}
            onClick={() => setStatus('pending')}
          >
            Pending
          </Button>
          <Button
            type="button"
            variant={status === 'decided' ? 'default' : 'outline'}
            onClick={() => setStatus('decided')}
          >
            Decided
          </Button>
        </div>
      </div>
      <QueryState isPending={tasks.isPending} error={tasks.error}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Procedure</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Subject</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(tasks.data ?? []).map((item) => (
              <TableRow key={item.task.id}>
                <TableCell>
                  <Link
                    to="/engagements/$eid/reviews/$taskId"
                    params={{ eid, taskId: item.task.id }}
                    className="underline"
                  >
                    {item.procedure_name}
                  </Link>
                </TableCell>
                <TableCell>{item.task.kind}</TableCell>
                <TableCell>{item.task.subject_id ?? item.task.target_id}</TableCell>
                <TableCell>{item.task.status}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>
    </div>
  )
}
