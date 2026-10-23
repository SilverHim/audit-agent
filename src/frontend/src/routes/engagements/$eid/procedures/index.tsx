import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { useMemo, useState } from 'react'
import { reviewsGetProcedureOptions, reviewsListProceduresOptions } from '@/client/@tanstack/react-query.gen'
import type { EngagementOverview, Engine, ProcedureStatus } from '@/client/types.gen'
import { Code, PageHeader, StatusPill, type Tone } from '@/components/chrome'
import { standardLabel } from '@/components/evidence-link'
import { ProcedureIntroView } from '@/components/procedure-intro'
import { QueryState } from '@/components/query-state'
import { RerunButton, StaleBadge } from '@/components/rerun'
import { buttonVariants } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/lib/i18n'
import { type CatalogProcedure, catalogProcedures } from '@/lib/procedures'
import { procedureName } from '@/lib/review'
import { useEngagement } from '@/lib/use-engagement'

type Search = { selected?: string }

export const Route = createFileRoute('/engagements/$eid/procedures/')({
  validateSearch: (search: Record<string, unknown>): Search => ({
    selected: typeof search.selected === 'string' ? search.selected : undefined,
  }),
  component: Procedures,
})

const STAGES = ['P0_Acceptance', 'P1_Planning', 'P2_Executing', 'P3_Completion', 'P4_Reporting'] as const
const ENGINES: Engine[] = ['rule', 'llm', 'hybrid', 'manual']
const COLS = 'grid grid-cols-[80px_minmax(0,2fr)_minmax(0,1fr)_minmax(0,1.4fr)_minmax(0,1.5fr)] gap-3'
const STATUS_TONE: Record<ProcedureStatus, Tone> = {
  planned: 'neutral',
  executing: 'accent',
  awaiting_human: 'warn',
  in_dispute: 'warn',
  completed: 'ok',
  awaiting_data: 'accent',
  no_data: 'neutral',
}

// 等人处理的状态: 有待决复核任务, 其中有待决的争议任务时为争议中 (后端 R151); 程序页默认选中它, 给 "去处理"
const WAITING = new Set<ProcedureStatus>(['awaiting_human', 'in_dispute'])

function Procedures() {
  const { eid } = Route.useParams()
  const { selected } = Route.useSearch()
  const navigate = useNavigate()
  const { t, lang } = useI18n()
  const engagement = useEngagement(eid)
  const overview = engagement.data
  const catalog = useQuery(reviewsListProceduresOptions({ path: { eid } }))
  const [engine, setEngine] = useState<Engine | 'all'>('all')
  const [query, setQuery] = useState('')
  const procedures = useMemo(
    () => catalogProcedures(catalog.data ?? [], overview?.procedures ?? [], overview?.features ?? []),
    [catalog.data, overview?.procedures, overview?.features],
  )

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return procedures.filter(
      (item) =>
        (engine === 'all' || item.engine === engine) &&
        (!needle ||
          item.id.toLowerCase().includes(needle) ||
          procedureName(t, item.id, item.name).toLowerCase().includes(needle)),
    )
  }, [procedures, engine, query, t])

  const current =
    procedures.find((item) => item.id === selected) ??
    procedures.find((item) => item.project && WAITING.has(item.project.status)) ??
    procedures[0]
  const select = (id: string) =>
    void navigate({ to: '/engagements/$eid/procedures', params: { eid }, search: { selected: id }, replace: true })

  return (
    <>
      <PageHeader
        subtitle={t.proceduresPage.catalogHint}
        title={
          <span className="flex items-baseline gap-2.5">
            {t.proceduresPage.title}
            <span className="font-normal text-[13px] text-muted-foreground">
              {t.proceduresPage.total(procedures.length)}
            </span>
          </span>
        }
        actions={
          <>
            {overview?.stale_procedure_ids?.length ? (
              <RerunButton eid={eid} overview={overview} reasonAs="title" />
            ) : null}
            <label htmlFor="proc-search" className="sr-only">
              {t.proceduresPage.search}
            </label>
            <Input
              id="proc-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t.proceduresPage.search}
              className="h-10 w-64"
            />
          </>
        }
      />
      <QueryState isPending={catalog.isPending || engagement.isPending} error={catalog.error || engagement.error}>
        <div className="flex flex-1 flex-wrap">
          <section className="flex min-w-0 flex-[999_1_560px] flex-col gap-4 px-4 py-5 sm:px-7">
            <fieldset
              aria-label={t.proceduresPage.cols.method}
              className="inline-flex flex-wrap self-start rounded-[10px] bg-[#e9ecf1] p-[3px]"
            >
              {(['all', ...ENGINES] as const).map((item) => (
                <button
                  key={item}
                  type="button"
                  aria-pressed={engine === item}
                  onClick={() => setEngine(item)}
                  className={cn(
                    'min-h-8 rounded-lg px-3 text-[13px]',
                    engine === item ? 'bg-card font-semibold shadow-xs' : 'font-medium',
                  )}
                >
                  {item === 'all' ? t.proceduresPage.all : t.engines[item]} ·{' '}
                  {item === 'all' ? procedures.length : procedures.filter((p) => p.engine === item).length}
                </button>
              ))}
            </fieldset>
            <div className="overflow-x-auto rounded-xl border bg-card">
              <div className="min-w-[640px]">
                <div
                  className={cn(
                    COLS,
                    'border-b bg-[#fafbfc] px-4 py-2.5 font-semibold text-[12px] text-muted-foreground',
                  )}
                >
                  <span>{t.proceduresPage.cols.code}</span>
                  <span>{t.proceduresPage.cols.name}</span>
                  <span>{t.proceduresPage.cols.method}</span>
                  <span>{t.proceduresPage.cols.role}</span>
                  <span>{t.proceduresPage.cols.status}</span>
                </div>
                {STAGES.map((stage) => {
                  const rows = filtered.filter((item) => item.phase === stage)
                  return (
                    <div key={stage} className="flex flex-col">
                      <div className="flex items-baseline gap-2 border-line-soft border-b bg-[#fcfcfd] px-4 pt-3 pb-2">
                        <span className="font-semibold text-[13.5px]">{t.phases[stage]}</span>
                        <span className="flex-1" />
                        <Code className="text-[12px] text-muted-foreground">{rows.length}</Code>
                      </div>
                      {rows.length ? (
                        rows.map((item) => (
                          <ProcedureRow
                            key={item.id}
                            item={item}
                            on={current?.id === item.id}
                            onSelect={() => select(item.id)}
                          />
                        ))
                      ) : (
                        <div className="border-line-soft border-b px-4 py-3 text-[13px] text-muted-foreground">
                          {t.proceduresPage.none}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          </section>
          {current ? (
            <ProcedureAside eid={eid} item={current} overview={overview} lang={lang} onSelect={select} />
          ) : null}
        </div>
      </QueryState>
    </>
  )
}

function ProcedureRow({ item, on, onSelect }: { item: CatalogProcedure; on: boolean; onSelect: () => void }) {
  const { t } = useI18n()
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onSelect}
      className={cn(
        COLS,
        'min-h-12 w-full items-center border-0 border-line-soft border-b px-4 py-2.5 text-left',
        on ? 'bg-accent shadow-[inset_3px_0_0_var(--primary)]' : 'bg-card hover:bg-[#fafbfc]',
      )}
    >
      <Code className="font-semibold text-[13px]">{item.id}</Code>
      <span className="text-[13.5px]">{procedureName(t, item.id, item.name)}</span>
      <span className="text-[13px]">{t.engines[item.engine]}</span>
      <span className="text-[13px]">
        <Code className="font-semibold">{item.autonomy_level}</Code> · {t.levels[item.autonomy_level]}
      </span>
      <span className="flex flex-wrap items-center gap-1.5">
        <CatalogStatus item={item} />
        {item.project?.stale ? <StaleBadge short /> : null}
      </span>
    </button>
  )
}

function CatalogStatus({ item }: { item: CatalogProcedure }) {
  const { t } = useI18n()
  return item.project ? (
    <StatusPill tone={STATUS_TONE[item.project.status]}>{t.procStatus[item.project.status]}</StatusPill>
  ) : (
    <StatusPill tone="neutral">{item.enabled ? t.proceduresPage.notIncluded : t.proceduresPage.notEnabled}</StatusPill>
  )
}

function ProcedureAside({
  eid,
  item,
  overview,
  lang,
  onSelect,
}: {
  eid: string
  item: CatalogProcedure
  overview: EngagementOverview | undefined
  lang: 'zh' | 'en'
  onSelect: (id: string) => void
}) {
  const { t } = useI18n()
  const detail = useQuery({
    ...reviewsGetProcedureOptions({ path: { eid, pid: item.id } }),
    enabled: Boolean(item.project),
    refetchInterval: overview?.running ? 2000 : false,
  })
  const deps = item.depends_on
  const standards = item.standards
  return (
    <aside className="flex flex-[1_1_340px] flex-col gap-4 border-l bg-card p-5">
      <div className="flex flex-col gap-1">
        <Code className="text-[13px] text-muted-foreground">{item.id}</Code>
        <h2 className="m-0 font-semibold text-lg">{procedureName(t, item.id, item.name)}</h2>
        <span className="flex flex-wrap items-center gap-1.5">
          <CatalogStatus item={item} />
          {item.project?.stale ? <StaleBadge /> : null}
        </span>
      </div>
      {item.project?.stale ? (
        <div className="flex flex-col gap-2 rounded-[10px] bg-warn-soft p-3">
          <p className="m-0 text-[13px]">{t.rerun.staleHint}</p>
          <RerunButton eid={eid} overview={overview} procedure={item.project} size="default" />
        </div>
      ) : null}
      <ProcedureIntroView procedureId={item.id} runs={detail.data?.runs} />
      <dl className="m-0 grid grid-cols-[100px_minmax(0,1fr)] gap-x-3 gap-y-2.5 border-t pt-4 text-[13.5px]">
        <dt className="text-muted-foreground">{t.proceduresPage.stage}</dt>
        <dd className="m-0">{t.phases[item.phase]}</dd>
        <dt className="text-muted-foreground">{t.proceduresPage.cols.method}</dt>
        <dd className="m-0">{t.engines[item.engine]}</dd>
        <dt className="text-muted-foreground">{t.proceduresPage.cols.role}</dt>
        <dd className="m-0">
          <Code>{item.autonomy_level}</Code> · {t.levels[item.autonomy_level]}
        </dd>
        <dt className="text-muted-foreground">{t.proceduresPage.cols.agent}</dt>
        <dd className="m-0">
          <Code>{item.owner}</Code> {t.agents[item.owner]}
        </dd>
      </dl>
      <div className="flex flex-col gap-2">
        <span className="font-semibold text-[12px] text-muted-foreground">{t.proceduresPage.runsAfter}</span>
        <div className="flex flex-wrap gap-1.5">
          {deps.length ? (
            deps.map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => onSelect(id)}
                className="min-h-8 rounded-md border border-[#dce0e6] bg-[#f8f9fb] px-2.5 font-mono text-[12.5px] text-accent-foreground"
              >
                {id}
              </button>
            ))
          ) : (
            <span className="text-[13px] text-muted-foreground">{t.proceduresPage.noneValue}</span>
          )}
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <span className="font-semibold text-[12px] text-muted-foreground">{t.proceduresPage.standards}</span>
        {standards.length ? (
          standards.map((code) => (
            <span key={code} className="text-[13.5px]">
              {standardLabel(code, lang)}
            </span>
          ))
        ) : (
          <span className="text-[13px] text-muted-foreground">{t.proceduresPage.noneValue}</span>
        )}
      </div>
      <div className="pt-1">
        {item.project && WAITING.has(item.project.status) ? (
          <Link to="/engagements/$eid" params={{ eid }} search={{}} className={buttonVariants({ size: 'lg' })}>
            {t.proceduresPage.workOnIt}
          </Link>
        ) : item.project ? (
          <Link
            to="/engagements/$eid/procedures/$pid"
            params={{ eid, pid: item.id }}
            className={buttonVariants({ variant: 'outline', size: 'lg' })}
          >
            {t.proceduresPage.openDetail}
          </Link>
        ) : null}
        <Link to="/engagements/$eid/plan" params={{ eid }} className="mt-3 block text-[13px] underline">
          {t.planPage.open}
        </Link>
      </div>
    </aside>
  )
}
