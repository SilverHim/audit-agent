import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link, redirect, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { toast } from 'sonner'
import {
  engagementsCreateEngagementMutation,
  engagementsListEngagementsOptions,
} from '@/client/@tanstack/react-query.gen'
import { phaseLabel } from '@/components/phase-track'
import { QueryState } from '@/components/query-state'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { getToken } from '@/lib/auth'
import { invalidateEngagementList } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'

export const Route = createFileRoute('/engagements/')({
  beforeLoad: () => {
    if (!getToken()) throw redirect({ to: '/login' })
  },
  component: Engagements,
})

function Engagements() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const list = useQuery(engagementsListEngagementsOptions())
  const [periodStart, setPeriodStart] = useState('2025-07-01')
  const [periodEnd, setPeriodEnd] = useState('2026-06-30')
  const create = useMutation({
    ...engagementsCreateEngagementMutation(),
    onSuccess: async (overview) => {
      await invalidateEngagementList(queryClient)
      void navigate({ to: '/engagements/$eid', params: { eid: overview.engagement.id } })
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-heading text-2xl font-semibold">Engagements</h1>
        <p className="text-muted-foreground text-sm">Client name and ABN come from the demo dataset.</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>New engagement</CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault()
              create.mutate({ body: { period_start: periodStart, period_end: periodEnd, dataset: 'demo' } })
            }}
          >
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Period start</span>
              <Input
                type="date"
                value={periodStart}
                onChange={(event) => setPeriodStart(event.target.value)}
                required
              />
            </div>
            <div className="grid gap-1 text-sm">
              <span className="font-medium">Period end</span>
              <Input type="date" value={periodEnd} onChange={(event) => setPeriodEnd(event.target.value)} required />
            </div>
            <Button type="submit" disabled={create.isPending}>
              Create
            </Button>
          </form>
        </CardContent>
      </Card>
      <QueryState isPending={list.isPending} error={list.error}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Client</TableHead>
              <TableHead>Phase</TableHead>
              <TableHead>Gate</TableHead>
              <TableHead>Pending reviews</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(list.data ?? []).map((item) => (
              <TableRow key={item.id}>
                <TableCell>
                  <Link to="/engagements/$eid" params={{ eid: item.id }} className="underline">
                    {item.client_name}
                  </Link>
                  <p className="text-muted-foreground text-xs">{item.id}</p>
                </TableCell>
                <TableCell>{phaseLabel(item.phase)}</TableCell>
                <TableCell>{item.current_gate ?? '—'}</TableCell>
                <TableCell>{item.pending_tasks}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </QueryState>
    </div>
  )
}
