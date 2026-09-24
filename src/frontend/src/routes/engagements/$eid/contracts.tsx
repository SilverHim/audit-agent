import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/contracts')({ component: Contracts })

function Contracts() {
  return (
    <PagePlaceholder
      title="Contracts"
      description="Contracts and terms extracted by DOC-04, linked to the source page."
      endpoints={['GET /api/v1/engagements/{eid}/contracts']}
    />
  )
}
