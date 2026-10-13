import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { evidenceListConfirmationsOptions, evidenceListDocumentsOptions } from '@/client/@tanstack/react-query.gen'
import { EvidenceLink } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatAud } from '@/lib/money'

export const Route = createFileRoute('/engagements/$eid/evidence')({ component: Evidence })

function Evidence() {
  const { eid } = Route.useParams()
  const documents = useQuery(evidenceListDocumentsOptions({ path: { eid } }))
  const confirmations = useQuery(evidenceListConfirmationsOptions({ path: { eid } }))
  return (
    <div className="space-y-6">
      <h1 className="font-heading text-2xl font-semibold">Evidence</h1>
      <QueryState isPending={documents.isPending} error={documents.error}>
        <Card>
          <CardHeader>
            <CardTitle>Documents</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {(documents.data ?? []).map((document) => (
              <div key={document.id}>
                <EvidenceLink
                  eid={eid}
                  label={`${document.file_name} · ${document.type} · ${document.page_count} pages`}
                  pointer={{
                    type: 'contract',
                    source_type: 'document',
                    source_id: document.id,
                    locator: document.file_name,
                    page: 1,
                  }}
                />
              </div>
            ))}
          </CardContent>
        </Card>
      </QueryState>
      <QueryState isPending={confirmations.isPending} error={confirmations.error}>
        <Card>
          <CardHeader>
            <CardTitle>Confirmations</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {(confirmations.data ?? []).map((item) => (
              <div key={item.id}>
                <p>
                  {item.confirming_party} · {item.result} · book {formatAud(item.book_balance_cents)} · confirmed{' '}
                  {formatAud(item.confirmed_balance_cents)} · difference {formatAud(item.difference_cents)}
                </p>
                {item.reply_document_id ? (
                  <EvidenceLink
                    eid={eid}
                    pointer={{
                      type: 'external',
                      source_type: 'document',
                      source_id: item.reply_document_id,
                      locator: 'reply',
                      page: 1,
                    }}
                    label="Open reply"
                  />
                ) : (
                  <p className="text-muted-foreground">No reply PDF yet.</p>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      </QueryState>
    </div>
  )
}
