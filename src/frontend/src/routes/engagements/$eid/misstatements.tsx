import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { reportsGetMisstatementsOptions } from '@/client/@tanstack/react-query.gen'
import { PageHeader } from '@/components/chrome'
import { CorrectionNotices, MisstatementList } from '@/components/misstatement-lifecycle'
import { QueryState } from '@/components/query-state'
import { SubTabs } from '@/components/sub-tabs'
import { useI18n } from '@/lib/i18n'
import { useMoney } from '@/lib/use-money'

export const Route = createFileRoute('/engagements/$eid/misstatements')({ component: Misstatements })

function Misstatements() {
  const { formatMoney } = useMoney()
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
                  <dd className="m-0 font-mono">{formatMoney(data.materiality_cents)}</dd>
                  <dt className="text-muted-foreground">{t.completion.candidate}</dt>
                  <dd className="m-0">{data.schedule ? t.opinions[data.schedule.candidate_opinion] : '—'}</dd>
                  <dt className="text-muted-foreground">{t.reportPage.opinionRoute}</dt>
                  <dd className="m-0 font-semibold">{data.opinion_route ? t.opinions[data.opinion_route] : '—'}</dd>
                  <dt className="text-muted-foreground">{t.completion.total}</dt>
                  <dd className="m-0 font-mono">{formatMoney(data.schedule?.total_cents)}</dd>
                </dl>
              </section>
              <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
                <h2 className="m-0 font-semibold text-base">{t.reportPage.misstatements}</h2>
                {data.items.length ? (
                  <MisstatementList items={data.items} />
                ) : (
                  <span className="text-[13px] text-muted-foreground">{t.reportPage.noMisstatements}</span>
                )}
              </section>
              {data.correction_notices?.length ? (
                <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
                  <h2 className="m-0 font-semibold text-base">{t.lifecycle.notices}</h2>
                  <CorrectionNotices notices={data.correction_notices} />
                </section>
              ) : null}
            </>
          ) : null}
        </QueryState>
      </div>
    </>
  )
}
