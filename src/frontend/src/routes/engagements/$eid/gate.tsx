import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/gate')({ component: Gate })

function Gate() {
  return (
    <PagePlaceholder
      title="Gate"
      description="Material and decision form for ACC-01, PLN-01/03, FIN-01 and RPT-01."
      endpoints={['GET /api/v1/engagements/{eid}/gate', 'POST /api/v1/engagements/{eid}/gate-decisions']}
    />
  )
}
