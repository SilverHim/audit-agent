import { createFileRoute } from '@tanstack/react-router'
import { GatePanel } from '@/components/gate-form'
import { decisionsDisabled } from '@/lib/engagement'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/gate')({ component: Gate })

function Gate() {
  const { eid } = Route.useParams()
  const overview = useEngagement(eid)
  return (
    <div className="space-y-4">
      <h1 className="font-heading text-2xl font-semibold">Gate</h1>
      <GatePanel eid={eid} disabled={decisionsDisabled(overview.data)} />
    </div>
  )
}
