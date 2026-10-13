import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { evidenceListContractsOptions } from '@/client/@tanstack/react-query.gen'
import { EvidenceLink, PointerList } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatAud } from '@/lib/money'

export const Route = createFileRoute('/engagements/$eid/contracts')({ component: Contracts })

function Contracts() {
  const { eid } = Route.useParams()
  const contracts = useQuery(evidenceListContractsOptions({ path: { eid } }))
  return (
    <div className="space-y-4">
      <h1 className="font-heading text-2xl font-semibold">Contracts</h1>
      <QueryState isPending={contracts.isPending} error={contracts.error}>
        <div className="space-y-4">
          {(contracts.data ?? []).map((contract) => (
            <Card key={contract.id}>
              <CardHeader>
                <CardTitle>
                  {contract.id} · {contract.counterparty}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <p>
                  {formatAud(contract.amount_cents)} · signed {contract.sign_date ?? '—'} · ABN{' '}
                  {contract.counterparty_abn ?? '—'}
                </p>
                <PointerList eid={eid} pointers={contract.evidence} />
                {contract.terms.map((term) => (
                  <div key={term.id}>
                    <p className="font-medium">{term.term_type}</p>
                    <p className="whitespace-pre-wrap">{term.text}</p>
                    <EvidenceLink
                      eid={eid}
                      label={`Source page ${term.page}`}
                      pointer={{
                        type: 'contract',
                        source_type: 'document',
                        source_id: contract.document_id,
                        locator: term.id,
                        page: term.page,
                      }}
                    />
                  </div>
                ))}
              </CardContent>
            </Card>
          ))}
        </div>
      </QueryState>
    </div>
  )
}
