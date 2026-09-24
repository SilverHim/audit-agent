import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/trail')({ component: Trail })

function Trail() {
  return (
    <PagePlaceholder
      title="Audit trail"
      description="LLM calls, human decisions and state transitions in time order."
      endpoints={['GET /api/v1/engagements/{eid}/trail']}
    />
  )
}
