import { createFileRoute } from '@tanstack/react-router'
import { PagePlaceholder } from '@/components/page-placeholder'

export const Route = createFileRoute('/engagements/$eid/procedures/$pid')({ component: ProcedureDetail })

function ProcedureDetail() {
  return (
    <PagePlaceholder
      title="Procedure"
      description="Runs, exceptions and judgments of one procedure."
      endpoints={['GET /api/v1/engagements/{eid}/procedures/{pid}']}
    />
  )
}
