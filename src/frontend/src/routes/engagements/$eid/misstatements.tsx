import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { reportsGetMisstatementsOptions } from '@/client/@tanstack/react-query.gen'
import { Code, PageHeader } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { SubTabs } from '@/components/sub-tabs'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'

export const Route = createFileRoute('/engagements/$eid/misstatements')({ component: Misstatements })

function Misstatements() {
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const report = useQuery(reportsGetMisstatementsOptions({ path: { eid } }))
  const data = report.data
  return (
    <>
      <PageHeader title={t.reportPage.title} />
      <SubTabs
        eid={eid}
        tabs={[
          { to: '/engagements/$eid/report', label: t.reportPage.title },
          { to: '/engagements/$eid/misstatements', label: t.reportPage.misstatements },
        ]}
      />
      <div className="mx-auto flex w-full max-w-[1000px] flex-col gap-5 px-4 py-6 sm:px-7">
        <QueryState isPending={report.isPending} error={report.error}>
          {data ? (
            <>
              <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
                <h2 className="m-0 font-semibold text-base">{t.reportPage.schedule}</h2>
                <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[14px]">
                  <dt className="text-muted-foreground">{t.completion.materiality}</dt>
                  <dd className="m-0 font-mono">{formatAud(data.materiality_cents)}</dd>
                  <dt className="text-muted-foreground">{t.completion.candidate}</dt>
                  <dd className="m-0">{data.schedule ? t.opinions[data.schedule.candidate_opinion] : '—'}</dd>
                  <dt className="text-muted-foreground">{t.reportPage.opinionRoute}</dt>
                  <dd className="m-0 font-semibold">{data.opinion_route ? t.opinions[data.opinion_route] : '—'}</dd>
                  <dt className="text-muted-foreground">{t.completion.total}</dt>
                  <dd className="m-0 font-mono">{formatAud(data.schedule?.total_cents)}</dd>
                </dl>
              </section>
              <section className="overflow-x-auto rounded-xl border bg-card">
                <table className="w-full min-w-[640px] border-collapse text-[14px]">
                  <thead>
                    <tr className="border-b bg-[#fafbfc] text-left font-semibold text-[12px] text-muted-foreground">
                      <th className="px-4 py-2.5">ID</th>
                      <th className="px-4 py-2.5">{t.card.msFields.account}</th>
                      <th className="px-4 py-2.5">{t.card.msFields.assertion}</th>
                      <th className="px-4 py-2.5 text-right">{t.judgment.amount}</th>
                      <th className="px-4 py-2.5">{t.card.msFields.direction}</th>
                      <th className="px-4 py-2.5">{t.card.msFields.type}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.length ? (
                      data.items.map((item) => (
                        <tr key={item.id} className="border-line-soft border-b last:border-b-0">
                          <td className="px-4 py-2.5">
                            <Code className="font-semibold">{item.id}</Code>
                          </td>
                          <td className="px-4 py-2.5">{item.account}</td>
                          <td className="px-4 py-2.5">{t.assertions[item.assertion]}</td>
                          <td className="px-4 py-2.5 text-right font-mono">{formatAud(item.amount_cents)}</td>
                          <td className="px-4 py-2.5">{t.directions[item.direction]}</td>
                          <td className="px-4 py-2.5">{t.misstatementTypes[item.type]}</td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={6} className="px-4 py-4 text-[13px] text-muted-foreground">
                          {t.reportPage.noMisstatements}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </section>
            </>
          ) : null}
        </QueryState>
      </div>
    </>
  )
}
