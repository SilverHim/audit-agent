import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/reviews/')({ component: Reviews })

function Reviews() {
  return (
    <PagePlaceholder
      title="Review queue"
      description="Pending and decided review tasks."
      endpoints={['GET /api/v1/engagements/{eid}/review-tasks']}
    />
  )
}
