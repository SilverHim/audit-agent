import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/report')({ component: Report })

function Report() {
  return (
    <PagePlaceholder
      title="Report"
      description="Current report, revision history with annotations, and the RPT-01 sign-off."
      endpoints={['GET /api/v1/engagements/{eid}/reports', 'POST /api/v1/engagements/{eid}/gate-decisions']}
    />
  )
}
