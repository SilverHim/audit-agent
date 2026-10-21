import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { evidenceListConfirmationsOptions, evidenceListDocumentsOptions } from '@/client/@tanstack/react-query.gen'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { EvidenceLink } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { SubTabs } from '@/components/sub-tabs'
import { useI18n } from '@/lib/i18n'
import { useMoney } from '@/lib/use-money'

export const Route = createFileRoute('/engagements/$eid/evidence')({ component: Evidence })

function Evidence() {
  const { formatMoney } = useMoney()
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const documents = useQuery(evidenceListDocumentsOptions({ path: { eid } }))
  const confirmations = useQuery(evidenceListConfirmationsOptions({ path: { eid } }))
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
        <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
          <h2 className="m-0 font-semibold text-base">{t.evidencePage.documents}</h2>
          <QueryState isPending={documents.isPending} error={documents.error}>
            {(documents.data ?? []).length ? (
              <ul className="m-0 flex list-none flex-col p-0">
                {(documents.data ?? []).map((document) => (
                  <li
                    key={document.id}
                    className="flex flex-wrap items-center gap-3 border-line-soft border-b py-2.5 last:border-b-0"
                  >
                    <span className="min-w-0 flex-1 truncate font-medium">{document.file_name}</span>
                    <StatusPill tone="neutral">{t.evidencePage.docTypes[document.type] ?? document.type}</StatusPill>
                    <span className="text-[13px] text-muted-foreground">
                      {t.evidencePage.pages(document.page_count)}
                    </span>
                    <EvidenceLink
                      eid={eid}
                      label={t.evidencePage.open}
                      pointer={{
                        type: 'contract',
                        source_type: 'document',
                        source_id: document.id,
                        locator: document.file_name,
                        page: 1,
                      }}
                    />
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-[13px] text-muted-foreground">{t.evidencePage.none}</span>
            )}
          </QueryState>
        </section>

        <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
          <h2 className="m-0 font-semibold text-base">{t.evidencePage.confirmations}</h2>
          <QueryState isPending={confirmations.isPending} error={confirmations.error}>
            {(confirmations.data ?? []).map((item) => (
              <article key={item.id} className="flex flex-col gap-2 rounded-[10px] border p-3.5 text-[14px]">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{item.confirming_party}</span>
                  <StatusPill tone={item.difference_cents ? 'warn' : 'ok'}>
                    {t.evidencePage.results[item.result ?? 'pending']}
                  </StatusPill>
                </div>
                <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
                  <dt className="text-muted-foreground">{t.evidencePage.book}</dt>
                  <dd className="m-0 font-mono">{formatMoney(item.book_balance_cents)}</dd>
                  <dt className="text-muted-foreground">{t.evidencePage.confirmed}</dt>
                  <dd className="m-0 font-mono">{formatMoney(item.confirmed_balance_cents)}</dd>
                  <dt className="text-muted-foreground">{t.evidencePage.difference}</dt>
                  <dd className="m-0 font-mono">{formatMoney(item.difference_cents)}</dd>
                  <dt className="text-muted-foreground">{t.evidencePage.accruedInterest}</dt>
                  <dd className="m-0 font-mono">
                    {/* 回函经 DOC-06 抽取过 (后端判定) 而没提应计利息时写明 "未提及"; 没抽取过的同其他各项为 — */}
                    {item.accrued_interest_cents == null && item.reply_extracted
                      ? t.evidencePage.accruedNotStated
                      : formatMoney(item.accrued_interest_cents)}
                  </dd>
                </dl>
                {item.reply_document_id ? (
                  <span>
                    <EvidenceLink
                      eid={eid}
                      label={t.evidencePage.openReply}
                      pointer={{
                        type: 'external',
                        source_type: 'document',
                        source_id: item.reply_document_id,
                        locator: 'reply',
                        page: 1,
                      }}
                    />
                  </span>
                ) : (
                  <span className="text-[13px] text-muted-foreground">{t.evidencePage.noReply}</span>
                )}
                <Code className="text-[12px] text-muted-foreground">{item.id}</Code>
              </article>
            ))}
          </QueryState>
        </section>
      </div>
    </>
  )
}
