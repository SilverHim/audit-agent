import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/')({ component: Overview })

function Overview() {
  return (
    <PagePlaceholder
      title="Overview"
      description="P0–P4 progress, current gate, procedure status table and pending review count; poll every 2 s while running."
      endpoints={['GET /api/v1/engagements/{eid}', 'POST /api/v1/engagements/{eid}/resume']}
    />
  )
}
