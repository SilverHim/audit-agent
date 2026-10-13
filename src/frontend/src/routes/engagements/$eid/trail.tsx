import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { useState } from 'react'
import { reportsListTrailOptions } from '@/client/@tanstack/react-query.gen'
import type { TrailKind } from '@/client/types.gen'
import { QueryState } from '@/components/query-state'
import { Button } from '@/components/ui/button'

const KINDS: Array<TrailKind | 'all'> = ['all', 'llm_call', 'decision', 'transition', 'external_call']

export const Route = createFileRoute('/engagements/$eid/trail')({ component: Trail })

function Trail() {
  const { eid } = Route.useParams()
  const [kind, setKind] = useState<TrailKind | 'all'>('all')
  const [offset, setOffset] = useState(0)
  const limit = 100
  const trail = useQuery(
    reportsListTrailOptions({ path: { eid }, query: { kind: kind === 'all' ? undefined : kind, limit, offset } }),
  )
  const rows = trail.data ?? []
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-heading text-2xl font-semibold">Audit trail</h1>
        <div className="flex flex-wrap gap-2">
          {KINDS.map((item) => (
            <Button
              key={item}
              type="button"
              variant={kind === item ? 'default' : 'outline'}
              onClick={() => {
                setKind(item)
                setOffset(0)
              }}
            >
              {item}
            </Button>
          ))}
        </div>
      </div>
      <QueryState isPending={trail.isPending} error={trail.error}>
        <div className="space-y-3">
          {rows.map((entry) => (
            <details key={entry.id ?? entry.summary} className="rounded-md border p-3 text-sm">
              <summary>
                {entry.created_at ?? ''} · {entry.kind} · {entry.actor} · {entry.summary}
              </summary>
              <pre className="mt-2 overflow-x-auto text-xs">{JSON.stringify(entry.payload, null, 2)}</pre>
            </details>
          ))}
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              Previous
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={rows.length < limit}
              onClick={() => setOffset(offset + limit)}
            >
              Next
            </Button>
          </div>
        </div>
      </QueryState>
    </div>
  )
}
