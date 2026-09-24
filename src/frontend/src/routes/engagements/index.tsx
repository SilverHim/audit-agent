import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/')({ component: Engagements })

function Engagements() {
  return (
    <PagePlaceholder
      title="Engagements"
      description="Engagement list and new engagement form: client name, ABN, period start and end; dataset is always demo."
      endpoints={['GET /api/v1/engagements', 'POST /api/v1/engagements']}
    />
  )
}
