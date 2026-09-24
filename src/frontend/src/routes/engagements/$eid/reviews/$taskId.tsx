import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/reviews/$taskId')({ component: ReviewTaskDetail })

function ReviewTaskDetail() {
  return (
    <PagePlaceholder
      title="Review task"
      description="Conclusion, reasoning chain, evidence, counter-argument and confidence side by side; only the decisions allowed for the task."
      endpoints={[
        'GET /api/v1/engagements/{eid}/review-tasks',
        'GET /api/v1/engagements/{eid}/judgments/{jid}',
        'POST /api/v1/engagements/{eid}/review-records',
      ]}
    />
  )
}
