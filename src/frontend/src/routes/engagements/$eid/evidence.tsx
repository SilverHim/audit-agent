import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/evidence')({ component: Evidence })

function Evidence() {
  return (
    <PagePlaceholder
      title="Evidence"
      description="Open an evidence pointer by source_type: PDF page, ledger row, standard fragment or trail entry."
      endpoints={[
        'GET /api/v1/engagements/{eid}/documents',
        'GET /api/v1/engagements/{eid}/evidence/{document_id}',
        'GET /api/v1/engagements/{eid}/records/{source_type}/{source_id}',
        'GET /api/v1/standards/{fragment_id}',
        'GET /api/v1/engagements/{eid}/trail/{trail_id}',
      ]}
    />
  )
}
