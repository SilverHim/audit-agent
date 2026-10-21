import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import { reviewsGetProcedureOptions, reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import type { RuleOutcome } from '@/client/types.gen'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { PointerList, standardLabel } from '@/components/evidence-link'
import { ProcedureIntroView } from '@/components/procedure-intro'
import { QueryState } from '@/components/query-state'
import { RerunButton, StaleBadge } from '@/components/rerun'
import { buttonVariants } from '@/components/ui/button'
import { GeneratedLanguage } from '@/components/working-language'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import { procedureName } from '@/lib/review'
import { notRunRules, outcomeStatuses, outcomeTone } from '@/lib/rule-outcomes'
import { useEngagement } from '@/lib/use-engagement'
import { contentLanguage } from '@/lib/working-language'

export const Route = createFileRoute('/engagements/$eid/procedures/$pid')({ component: ProcedurePage })

// 程序的执行记录: 每次执行 (及其没有执行的规则), 规则发现, 判断; 过期时给重跑入口, 被重跑取代的执行及其结果标为历史
function ProcedurePage() {
  const { eid, pid } = Route.useParams()
  const { t, lang, formatDateTime } = useI18n()
  const overview = useEngagement(eid).data
  // 后台执行 (如重跑) 时随总览一起刷新, 新的执行记录与过期标记及时更新
  const detail = useQuery({
    ...reviewsGetProcedureOptions({ path: { eid, pid } }),
    refetchInterval: overview?.running ? 2000 : false,
  })
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid } }))
  const data = detail.data
  const superseded = new Set(data?.superseded_run_ids ?? [])
  const summary = overview?.procedures.find((item) => item.id === pid)
  const stale = summary?.stale ?? data?.stale ?? false // 总览轮询更勤, 以它为准
  return (
    <>
      <PageHeader
        title={
          <>
            <Code>{pid}</Code> · {procedureName(t, pid, data?.name)}
          </>
        }
        subtitle={
          data
            ? `${t.procStatus[data.procedure.status ?? 'planned']} · ${t.proceduresPage.runsAfter}: ${data.depends_on.join(', ') || t.proceduresPage.noneValue} · ${t.proceduresPage.standards}: ${data.standards.map((code) => standardLabel(code, lang)).join(', ') || t.proceduresPage.noneValue}`
            : undefined
        }
        actions={
          <Link
            to="/engagements/$eid/procedures"
            params={{ eid }}
            search={{ selected: pid }}
            className={buttonVariants({ variant: 'outline', size: 'lg' })}
          >
            {t.nav.procedures}
          </Link>
        }
      />
      <div className="mx-auto flex w-full max-w-[1000px] flex-col gap-5 px-4 py-6 sm:px-7">
        <QueryState isPending={detail.isPending} error={detail.error}>
          {data ? (
            <>
              {stale ? (
                <section className="flex flex-col gap-2.5 rounded-xl bg-warn-soft p-4">
                  <span>
                    <StaleBadge onSoft />
                  </span>
                  <p className="m-0 text-[14px]">{t.rerun.staleHint}</p>
                  <RerunButton
                    eid={eid}
                    overview={overview}
                    procedure={
                      summary ?? { id: pid, phase: data.procedure.phase, status: data.procedure.status ?? 'planned' }
                    }
                  />
                </section>
              ) : null}
              <Block title={t.proceduresPage.intro.title}>
                <ProcedureIntroView procedureId={pid} runs={data.runs} />
              </Block>
              <Block title={t.procedureDetail.runs}>
                {data.runs.length ? (
                  data.runs.map((run) => {
                    const outcomes = notRunRules(run)
                    return (
                      <div key={run.id} className="flex flex-col gap-2 text-[14px]">
                        <div className="flex flex-wrap items-center gap-2.5">
                          <span className="font-semibold">{t.procedureDetail.run(run.run_no)}</span>
                          <StatusPill
                            tone={run.status === 'failed' ? 'risk' : run.status === 'succeeded' ? 'ok' : 'accent'}
                          >
                            {run.status}
                          </StatusPill>
                          <span className="text-muted-foreground">{formatDateTime(run.started_at)}</span>
                          {superseded.has(run.id) ? <StatusPill tone="neutral">{t.rerun.superseded}</StatusPill> : null}
                          {run.failure_reason ? <span lang="en">{run.failure_reason}</span> : null}
                        </div>
                        {outcomes.length ? <NotRunRules outcomes={outcomes} /> : null}
                      </div>
                    )
                  })
                ) : (
                  <Empty />
                )}
              </Block>
              <Block title={t.procedureDetail.exceptions}>
                {data.exceptions.length ? (
                  data.exceptions.map((item) => (
                    <div key={item.id} className="flex flex-col gap-1.5 text-[14px]">
                      <span className="flex flex-wrap items-center gap-2">
                        <Code className="font-semibold">{item.id}</Code>
                        <StatusPill tone={item.severity === 'high' ? 'risk' : 'warn'}>{item.severity}</StatusPill>
                        <Code>{formatAud(item.amount_cents)}</Code>
                        {superseded.has(item.run_id) ? <StatusPill tone="neutral">{t.rerun.history}</StatusPill> : null}
                      </span>
                      <PointerList eid={eid} pointers={item.evidence} />
                    </div>
                  ))
                ) : (
                  <Empty />
                )}
              </Block>
              <Block title={t.procedureDetail.judgments}>
                {data.judgments.length ? (
                  data.judgments.map((judgment) => {
                    const task = tasks.data?.find((item) => item.task.target_id === judgment.id)
                    return (
                      <div key={judgment.id} className="flex flex-col gap-1.5 rounded-[10px] border p-3.5 text-[14px]">
                        <span className="flex flex-wrap items-center gap-2">
                          <Code className="font-semibold">{judgment.subject_id}</Code>
                          <span>{t.proposed[judgment.proposed_action]}</span>
                          <Code className="text-muted-foreground">{judgment.confidence.toFixed(2)}</Code>
                          {superseded.has(judgment.run_id) ? (
                            <StatusPill tone="neutral">{t.rerun.history}</StatusPill>
                          ) : null}
                          <GeneratedLanguage eid={eid} runId={judgment.run_id} />
                          <span className="flex-1" />
                          {task ? (
                            <Link
                              to="/engagements/$eid/reviews/$taskId"
                              params={{ eid, taskId: task.task.id }}
                              className="text-[13px] text-accent-foreground underline underline-offset-2"
                            >
                              {t.workspace.formView}
                            </Link>
                          ) : null}
                        </span>
                        <p className="m-0" lang={contentLanguage(overview, judgment.run_id)}>
                          {judgment.conclusion}
                        </p>
                        <PointerList eid={eid} pointers={judgment.evidence} />
                      </div>
                    )
                  })
                ) : (
                  <Empty />
                )}
              </Block>
            </>
          ) : null}
        </QueryState>
      </div>
    </>
  )
}

// 一次执行没有执行的规则 (需求 R300): 编号, 状态与理由, 下面说明出现过的状态的含义
function NotRunRules({ outcomes }: { outcomes: RuleOutcome[] }) {
  const { t } = useI18n()
  const words = t.procedureDetail.notRun
  return (
    <div className="flex flex-col gap-2 rounded-[10px] border p-3.5">
      <span className="font-medium text-[13px]">{words.title}</span>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
        {outcomes.map((outcome) => (
          <li key={outcome.rule_id} className="flex flex-wrap items-center gap-2">
            <Code className="font-semibold">{outcome.rule_id}</Code>
            <StatusPill tone={outcomeTone(outcome.status)}>{words.status[outcome.status]}</StatusPill>
            <span>{outcome.reason}</span>
          </li>
        ))}
      </ul>
      {outcomeStatuses(outcomes).map((status) => (
        <p key={status} className="m-0 text-[12.5px] text-muted-foreground">
          {words.meaning[status]}
        </p>
      ))}
    </div>
  )
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <h2 className="m-0 font-semibold text-base">{title}</h2>
      {children}
    </section>
  )
}

function Empty() {
  const { t } = useI18n()
  return <span className="text-[13px] text-muted-foreground">{t.procedureDetail.none}</span>
}
