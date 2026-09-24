import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/misstatements')({ component: Misstatements })

function Misstatements() {
  return (
    <PagePlaceholder
      title="Misstatements"
      description="Misstatement list, uncorrected schedule and candidate opinion."
      endpoints={['GET /api/v1/engagements/{eid}/misstatements']}
    />
  )
}
