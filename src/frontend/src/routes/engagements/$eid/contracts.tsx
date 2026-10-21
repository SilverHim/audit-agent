import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { evidenceListContractsOptions } from '@/client/@tanstack/react-query.gen'
import { Code, PageHeader } from '@/components/chrome'
import { EvidenceLink } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { SubTabs } from '@/components/sub-tabs'
import { useI18n } from '@/lib/i18n'
import { useMoney } from '@/lib/use-money'

export const Route = createFileRoute('/engagements/$eid/contracts')({ component: Contracts })

function Contracts() {
  const { formatMoney } = useMoney()
  const { eid } = Route.useParams()
  const { t, formatDate } = useI18n()
  const contracts = useQuery(evidenceListContractsOptions({ path: { eid } }))
  return (
    <>
      <PageHeader title={t.evidencePage.title} />
      <SubTabs
        eid={eid}
        tabs={[
          { to: '/engagements/$eid/evidence', label: t.evidencePage.documents },
          { to: '/engagements/$eid/contracts', label: t.evidencePage.contracts },
        ]}
      />
      <div className="mx-auto flex w-full max-w-[1000px] flex-col gap-5 px-4 py-6 sm:px-7">
        <QueryState isPending={contracts.isPending} error={contracts.error}>
          {(contracts.data ?? []).map((contract) => (
            <section key={contract.id} className="flex flex-col gap-3 rounded-xl border bg-card p-5">
              <div className="flex flex-wrap items-baseline gap-2.5">
                <Code className="font-semibold">{contract.id}</Code>
                <span className="font-semibold">{contract.counterparty}</span>
                <span className="flex-1" />
                <Code>{formatMoney(contract.amount_cents)}</Code>
              </div>
              <span className="text-[13px] text-muted-foreground">
                {contract.sign_date ? t.evidencePage.signedOn(formatDate(contract.sign_date)) : ''}
                {contract.counterparty_abn ? ` · ABN ${contract.counterparty_abn}` : ''}
              </span>
              <div className="flex flex-col gap-2.5">
                <span className="font-semibold text-[12px] text-muted-foreground">{t.evidencePage.terms}</span>
                {contract.terms.map((term) => (
                  <article key={term.id} className="flex flex-col gap-1.5 rounded-[10px] border p-3.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-[14px]">
                        {t.evidencePage.termTypes[term.term_type] ?? term.term_type}
                      </span>
                      <span className="flex-1" />
                      <EvidenceLink
                        eid={eid}
                        label={`${term.id} · ${t.evidence.page(term.page)}`}
                        pointer={{
                          type: 'contract',
                          source_type: 'document',
                          source_id: contract.document_id,
                          locator: term.id,
                          page: term.page,
                        }}
                      />
                    </div>
                    <p className="m-0 whitespace-pre-wrap font-serif text-[14px] leading-relaxed" lang="en">
                      {term.text}
                    </p>
                  </article>
                ))}
              </div>
            </section>
          ))}
        </QueryState>
      </div>
    </>
  )
}
