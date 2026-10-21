import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { type ReactNode, useEffect, useRef } from 'react'
import { engagementsGetPlanOptions } from '@/client/@tanstack/react-query.gen'
import type { AuditPlan, PlanStatus, PlanView } from '@/client/types.gen'
import { Code, PageHeader, StatusPill, type Tone } from '@/components/chrome'
import { GateDecisionCard, PlanNarrative, useGate } from '@/components/gate-form'
import { QueryState } from '@/components/query-state'
import { StaleBadge } from '@/components/rerun'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { GeneratedLanguage } from '@/components/working-language'
import { useActorName } from '@/lib/actor'
import { decisionCardKey, decisionsDisabled } from '@/lib/engagement'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import { currentPlan, extentLabel, methodLabel, planTable, revisionHistory } from '@/lib/plan'
import { procedureName } from '@/lib/review'
import { useEngagement } from '@/lib/use-engagement'
import { contentLanguage, type WorkingLanguage } from '@/lib/working-language'

export const Route = createFileRoute('/engagements/$eid/plan')({ component: Plan })

const PLAN_TONE: Record<PlanStatus, Tone> = { draft: 'warn', approved: 'ok', superseded: 'neutral' }

function Panel({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <h2 className="m-0 font-semibold text-base">{title}</h2>
      {children}
    </section>
  )
}

// 审计计划页 (需求 R315): 计划状态与修订历史, 总体策略与各段, 重要性, 特别风险, 时间表, 项目质量复核与专家,
// 程序表; 停在 PLN-15 且后台不在执行时出现决定卡片 (与工作区当前事项同一张)
function Plan() {
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const overview = useEngagement(eid).data
  const plan = useQuery(engagementsGetPlanOptions({ path: { eid } }))
  // 计划在闸门决定与 AI 重拟 (退回后出下一修订) 时变化, 而总览只在执行中轮询: 后台执行起止或闸门变化时重新取计划
  const signal = `${overview?.running}:${overview?.current_gate}`
  const seen = useRef(signal)
  const { refetch } = plan
  useEffect(() => {
    if (seen.current === signal) return
    seen.current = signal
    void refetch()
  }, [signal, refetch])
  const atGate = overview?.current_gate === 'PLN-15' && !overview.running
  const gate = useGate(eid, atGate)
  const view = plan.data
  const current = view ? currentPlan(view) : undefined
  const lang = contentLanguage(overview, current?.run_id) // 计划正文的语言: 起草它的那次执行的 (需求 R340)
  return (
    <>
      <PageHeader
        title={t.planPage.title}
        subtitle={
          current ? (
            <span className="inline-flex items-center gap-2">
              <StatusPill tone={PLAN_TONE[current.status ?? 'draft']}>
                {t.planPage.status[current.status ?? 'draft']}
              </StatusPill>
              {t.planPage.revision(current.revision)}
              <GeneratedLanguage eid={eid} runId={current.run_id} />
            </span>
          ) : undefined
        }
      />
      <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-5 px-4 py-6 sm:px-7">
        <QueryState isPending={plan.isPending} error={plan.error}>
          {view && current ? (
            <>
              {overview?.stale_procedure_ids?.includes('PLN-15') && current.status === 'draft' ? (
                <Alert>
                  <AlertDescription>{t.planPage.stale}</AlertDescription>
                </Alert>
              ) : null}
              {atGate && gate.data?.material.kind === 'PLN-15' ? (
                <GateDecisionCard
                  key={decisionCardKey(gate.data)}
                  eid={eid}
                  view={gate.data}
                  disabled={decisionsDisabled(overview)}
                />
              ) : null}
              <Revisions view={view} />
              <Panel title={t.planPage.strategy}>
                <PlanNarrative eid={eid} plan={current} />
              </Panel>
              <Facts plan={current} view={view} lang={lang} />
              <ProcedureTable eid={eid} view={view} lang={lang} />
            </>
          ) : (
            // 功能 planning 关闭的项目永远不会起草计划 (导航里也没有这一项, 只有直接打开地址才到这里)
            <p className="text-muted-foreground text-sm">
              {overview && !overview.features?.includes('planning') ? t.planPage.disabled : t.planPage.none}
            </p>
          )}
        </QueryState>
      </div>
    </>
  )
}

function Revisions({ view }: { view: PlanView }) {
  const { t, formatDateTime } = useI18n()
  const actor = useActorName()
  return (
    <Panel title={t.planPage.history}>
      <ol className="m-0 flex list-none flex-col gap-2 p-0 text-[14px]">
        {revisionHistory(view).map(({ plan, status, decisions }) => (
          <li key={plan.id} className="flex flex-col gap-1 rounded-lg border px-3 py-2">
            <span className="flex flex-wrap items-center gap-2">
              <Code className="font-semibold">{plan.id}</Code>
              <StatusPill tone={PLAN_TONE[status]}>{t.planPage.status[status]}</StatusPill>
              {plan.approved_at ? (
                <span className="text-[13px] text-muted-foreground">
                  {t.planPage.approvedBy(actor(plan.approved_by) ?? '—', formatDateTime(plan.approved_at))}
                </span>
              ) : null}
            </span>
            {decisions
              .filter((decision) => decision.decision !== 'approve' && decision.reason)
              .map((decision) => (
                <span key={decision.id} className="text-[13px] text-[#4a5262]">
                  {t.verdicts['PLN-15'][decision.decision]}: <span lang="en">{decision.reason}</span>
                </span>
              ))}
          </li>
        ))}
      </ol>
    </Panel>
  )
}

function Facts({ plan, view, lang }: { plan: AuditPlan; view: PlanView; lang: WorkingLanguage }) {
  const { t } = useI18n()
  return (
    <div className="grid gap-5 md:grid-cols-2">
      <Panel title={t.planPage.materiality}>
        <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[14px]">
          <dt className="text-muted-foreground">{t.planPage.overall}</dt>
          <dd className="m-0 font-mono">{formatAud(plan.materiality_cents)}</dd>
          <dt className="text-muted-foreground">{t.planPage.performance}</dt>
          <dd className="m-0 font-mono">{formatAud(plan.performance_materiality_cents)}</dd>
          <dt className="text-muted-foreground">{t.planPage.trivial}</dt>
          <dd className="m-0 font-mono">{formatAud(plan.clearly_trivial_cents)}</dd>
        </dl>
      </Panel>
      <Panel title={t.planPage.risks}>
        <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[14px]">
          {view.risks.map((risk) => (
            <li key={risk.id} className="flex flex-col gap-0.5">
              <Code className="font-semibold">{risk.id}</Code>
              <span lang="en">{risk.description}</span>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title={t.planPage.timetable}>
        <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[14px]" lang={lang}>
          {plan.timetable.map((item) => (
            <div key={`${item.due}:${item.milestone}`} className="contents">
              <dt className="font-mono">{item.due}</dt>
              <dd className="m-0">{item.milestone}</dd>
            </div>
          ))}
        </dl>
      </Panel>
      <Panel title={t.planPage.eqr}>
        <p className="m-0 text-[14px]">{t.planPage.eqrSuggested(plan.eqr_recommendation.required)}</p>
        <p className="m-0 text-[14px]" lang={lang}>
          {plan.eqr_recommendation.basis}
        </p>
        <h3 className="m-0 font-semibold text-[13px] text-muted-foreground">{t.planPage.expert}</h3>
        <p className="m-0 text-[14px]" lang={lang}>
          {plan.expert_assessment}
        </p>
        {plan.status === 'approved' ? (
          <>
            <p className="m-0 text-[13px] text-muted-foreground">
              {t.planPage.confirmed(Boolean(plan.eqr_required), Boolean(plan.auditor_expert_not_required))}
            </p>
            <p className="m-0 text-[13px] text-muted-foreground">
              {t.planPage.confirmedBasis}
              <span lang="en">{plan.eqr_basis}</span>
            </p>
          </>
        ) : null}
      </Panel>
    </div>
  )
}

function ProcedureTable({ eid, view, lang }: { eid: string; view: PlanView; lang: WorkingLanguage }) {
  const { t } = useI18n()
  const overview = useEngagement(eid).data
  const rows = planTable(view, overview?.procedures ?? [])
  const cols = t.planPage.cols
  return (
    <section className="overflow-x-auto rounded-xl border bg-card">
      <table className="w-full min-w-[860px] border-collapse text-[14px]">
        <thead>
          <tr className="border-b bg-[#fafbfc] text-left font-semibold text-[12px] text-muted-foreground">
            <th className="px-4 py-2.5">{cols.objective}</th>
            <th className="px-4 py-2.5">{cols.procedure}</th>
            <th className="px-4 py-2.5">{cols.method}</th>
            <th className="px-4 py-2.5">{cols.extent}</th>
            <th className="px-4 py-2.5">{cols.owner}</th>
            <th className="px-4 py-2.5">{cols.status}</th>
            <th className="px-4 py-2.5">{cols.workpaper}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-line-soft border-b last:border-b-0">
              <td className="px-4 py-2.5">
                {row.objective ? (
                  <span className="flex flex-col">
                    <span>
                      {row.objective.account} · {t.assertions[row.objective.assertion]}
                    </span>
                    <span className="text-[12.5px] text-muted-foreground">
                      {t.riskLevels[row.objective.risk_level]}
                    </span>
                  </span>
                ) : (
                  <span className="text-muted-foreground">{t.planPage.general}</span>
                )}
              </td>
              <td className="px-4 py-2.5">
                <Link
                  to="/engagements/$eid/procedures/$pid"
                  params={{ eid, pid: row.procedureId }}
                  className="flex flex-col hover:underline"
                >
                  <Code className="font-semibold">{row.procedureId}</Code>
                  <span>{procedureName(t, row.procedureId, row.name ?? row.procedureId)}</span>
                </Link>
                {row.note ? (
                  <details className="mt-1 max-w-[28rem] text-[13px]">
                    <summary className="cursor-pointer text-muted-foreground">{t.planPage.note}</summary>
                    <p className="m-0 mt-1 whitespace-pre-wrap" lang={lang}>
                      {row.note}
                    </p>
                  </details>
                ) : null}
              </td>
              <td className="px-4 py-2.5">{methodLabel(row.engine, t)}</td>
              <td className="px-4 py-2.5">{extentLabel(row, t)}</td>
              <td className="px-4 py-2.5">{row.owner ? t.agents[row.owner] : '—'}</td>
              <td className="px-4 py-2.5">
                <span className="flex flex-wrap items-center gap-1.5">
                  {row.status ? <StatusPill tone="neutral">{t.procStatus[row.status]}</StatusPill> : '—'}
                  {row.stale ? <StaleBadge short /> : null}
                </span>
              </td>
              <td className="px-4 py-2.5 text-muted-foreground">—</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
