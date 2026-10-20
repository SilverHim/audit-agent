import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { reportsGetReportsOptions } from '@/client/@tanstack/react-query.gen'
import { PageHeader, StatusPill } from '@/components/chrome'
import { ReportPreview, RptSignForm } from '@/components/gate-form'
import { QueryState } from '@/components/query-state'
import { SubTabs } from '@/components/sub-tabs'
import { GeneratedLanguage } from '@/components/working-language'
import { decisionsDisabled } from '@/lib/engagement'
import { useI18n } from '@/lib/i18n'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/report')({ component: Report })

function Report() {
  const { eid } = Route.useParams()
  const { t, formatDate } = useI18n()
  const overview = useEngagement(eid).data
  const reports = useQuery(reportsGetReportsOptions({ path: { eid } }))
  const current = reports.data?.versions.at(-1)
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
        <QueryState isPending={reports.isPending} error={reports.error}>
          {reports.data ? (
            <>
              {current ? (
                <ReportPreview
                  report={current}
                  decisions={reports.data.decisions.filter((item) => item.decision === 'reject')}
                />
              ) : (
                <p className="text-muted-foreground text-sm">{t.reportPage.none}</p>
              )}
              {reports.data.versions.length ? (
                <section className="flex flex-col gap-2 rounded-xl border bg-card p-5">
                  <h2 className="m-0 font-semibold text-base">{t.reportPage.versions}</h2>
                  {reports.data.versions.map((version) => (
                    <div key={version.id} className="flex flex-wrap items-center gap-2.5 text-[14px]">
                      <span className="font-medium">
                        {t.reportCard.revision(version.revision, t.opinions[version.opinion_type])}
                      </span>
                      {version.status === 'signed' ? <StatusPill tone="ok">{t.stageStatus.signed}</StatusPill> : null}
                      <GeneratedLanguage eid={eid} language={version.language} />
                      {version.report_date ? (
                        <span className="text-muted-foreground">{formatDate(version.report_date)}</span>
                      ) : null}
                    </div>
                  ))}
                </section>
              ) : null}
            </>
          ) : null}
        </QueryState>
        {overview?.current_gate === 'RPT-01' ? <RptSignForm eid={eid} disabled={decisionsDisabled(overview)} /> : null}
      </div>
    </>
  )
}
