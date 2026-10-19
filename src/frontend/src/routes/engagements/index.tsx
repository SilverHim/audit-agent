import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link, redirect, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { ArrowRight, Plus } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import {
  engagementsCreateEngagementMutation,
  engagementsListEngagementsOptions,
} from '@/client/@tanstack/react-query.gen'
import type { EngagementSummary } from '@/client/types.gen'
import { Code, StatusPill, TopBar } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { Button, buttonVariants } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getToken } from '@/lib/auth'
import { invalidateEngagementList } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useDatasets } from '@/lib/new-api'

export const Route = createFileRoute('/engagements/')({
  beforeLoad: () => {
    if (!getToken()) throw redirect({ to: '/login' })
  },
  component: Engagements,
})

const STAGES = ['P0_Acceptance', 'P1_Planning', 'P2_Executing', 'P3_Completion', 'P4_Reporting'] as const
const CLOSED = new Set(['Archived', 'Declined'])

// 后端 R4 上线后列表带审计期间
type Summary = EngagementSummary & { period_start?: string; period_end?: string }

function Engagements() {
  const { t } = useI18n()
  const list = useQuery(engagementsListEngagementsOptions())
  const [creating, setCreating] = useState(false)
  const rows = (list.data ?? []) as Summary[]
  const open = rows.filter((item) => !CLOSED.has(item.phase))
  const closed = rows.filter((item) => CLOSED.has(item.phase))
  return (
    <div className="flex min-h-screen flex-col">
      <TopBar />
      <main className="mx-auto flex w-full max-w-[1120px] flex-1 flex-col gap-7 px-4 py-9 sm:px-8">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h1 className="m-0 font-semibold text-[26px] tracking-tight">{t.projectsPage.title}</h1>
            <p className="m-0 text-[14.5px] text-[#4a5262]">{t.projectsPage.subtitle}</p>
          </div>
          <Button type="button" size="lg" onClick={() => setCreating((value) => !value)}>
            <Plus className="size-4" aria-hidden="true" />
            {t.projectsPage.newEngagement}
          </Button>
        </div>
        {creating ? <CreateForm onCancel={() => setCreating(false)} /> : null}
        <QueryState isPending={list.isPending} error={list.error}>
          <section className="flex flex-col gap-3">
            <h2 className="m-0 font-semibold text-[13px] text-muted-foreground tracking-wide">
              {t.projectsPage.inProgress} · {open.length}
            </h2>
            {open.length ? (
              open.map((item) => <EngagementCard key={item.id} item={item} />)
            ) : (
              <div className="rounded-[14px] border border-input border-dashed px-6 py-5 text-[14px] text-[#4a5262]">
                {t.projectsPage.empty}
              </div>
            )}
          </section>
          <section className="flex flex-col gap-3">
            <h2 className="m-0 font-semibold text-[13px] text-muted-foreground tracking-wide">
              {t.projectsPage.closed} · {closed.length}
            </h2>
            {closed.length ? (
              closed.map((item) => <EngagementCard key={item.id} item={item} />)
            ) : (
              <div className="rounded-[14px] border border-input border-dashed px-6 py-5 text-[14px] text-[#4a5262]">
                {t.projectsPage.closedEmpty}
              </div>
            )}
          </section>
        </QueryState>
      </main>
    </div>
  )
}

function EngagementCard({ item }: { item: Summary }) {
  const { t, formatDate } = useI18n()
  const index = item.phase === 'Archived' ? STAGES.length : STAGES.indexOf(item.phase as (typeof STAGES)[number])
  const badge = item.current_gate
    ? t.projectsPage.gate(t.gates[item.current_gate] ?? item.current_gate)
    : item.pending_tasks > 0
      ? t.projectsPage.pending(item.pending_tasks)
      : null
  return (
    <article className="flex flex-wrap items-center gap-6 rounded-[14px] border bg-card px-6 py-5">
      <div className="flex min-w-0 flex-[2_1_320px] flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2.5">
          <h3 className="m-0 font-semibold text-lg tracking-tight">{item.client_name}</h3>
          {badge ? <StatusPill tone="warn">{badge}</StatusPill> : null}
          {item.phase === 'Declined' ? <StatusPill tone="neutral">{t.phases.Declined}</StatusPill> : null}
        </div>
        <span className="text-[12.5px] text-muted-foreground">
          <Code>{item.id}</Code>
          {item.period_end ? ` · ${t.projectsPage.yearEnded(formatDate(item.period_end))}` : ''}
        </span>
      </div>
      <div className="flex flex-[1_1_300px] flex-col gap-2">
        <div className="flex gap-1">
          {STAGES.map((stage, position) => (
            <span
              key={stage}
              className={cn(
                'h-1.5 flex-1 rounded-full',
                position < index ? 'bg-[#12a27a]' : position === index ? 'bg-primary' : 'bg-border',
              )}
            />
          ))}
        </div>
        <span className="text-[12.5px] font-medium text-accent-foreground">{t.phases[item.phase]}</span>
      </div>
      <Link
        to="/engagements/$eid"
        params={{ eid: item.id }}
        search={{}}
        className={buttonVariants({ variant: 'outline', size: 'lg' })}
      >
        {t.projectsPage.open}
        <ArrowRight className="size-4" aria-hidden="true" />
      </Link>
    </article>
  )
}

const DEFAULT_PERIOD = { start: '2025-07-01', end: '2026-06-30' } // 不自带期间的数据集 (Numbat) 的缺省财年

function CreateForm({ onCancel }: { onCancel: () => void }) {
  const { t } = useI18n()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const datasets = useDatasets()
  const [dataset, setDataset] = useState('')
  const [periodStart, setPeriodStart] = useState(DEFAULT_PERIOD.start)
  const [periodEnd, setPeriodEnd] = useState(DEFAULT_PERIOD.end)
  const create = useMutation({
    ...engagementsCreateEngagementMutation(),
    onSuccess: async (overview) => {
      toast.success(t.toasts.created)
      await invalidateEngagementList(queryClient)
      void navigate({ to: '/engagements/$eid', params: { eid: overview.engagement.id }, search: {} })
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  const options = datasets.data ?? []
  // 默认选 default 为真的数据集 (配置项 DEMO_DATASET), 没有时选第一个. 清单加载中, 取数失败或为空时不能创建,
  // 免得在默认数据集出来之前建成别的项目
  const chosen = dataset || options.find((item) => item.default)?.dataset || options[0]?.dataset || ''
  const selected = options.find((item) => item.dataset === chosen)
  const fixedStart = selected?.period_start ?? null
  const fixedEnd = selected?.period_end ?? null
  // 所选数据集自带期间时自动填起止日 (建项目时须与之相同); 不带时用 Numbat 的财年
  useEffect(() => {
    setPeriodStart(fixedStart ?? DEFAULT_PERIOD.start)
    setPeriodEnd(fixedEnd ?? DEFAULT_PERIOD.end)
  }, [fixedStart, fixedEnd])
  return (
    <section className="flex flex-col gap-4 rounded-[14px] border-[1.5px] border-primary bg-card px-6 py-5">
      <h2 className="m-0 font-semibold text-[17px]">{t.projectsPage.newEngagement}</h2>
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault()
          create.mutate({ body: { period_start: periodStart, period_end: periodEnd, dataset: chosen } })
        }}
      >
        <div className="flex flex-wrap gap-4">
          {options.length ? (
            <div className="flex flex-[2_1_320px] flex-col gap-1.5">
              <label htmlFor="new-client" className="font-semibold text-[13px]">
                {t.projectsPage.client}
              </label>
              <select
                id="new-client"
                className="h-11 rounded-[10px] border border-input bg-card px-3 text-[14px]"
                value={chosen}
                onChange={(event) => setDataset(event.target.value)}
              >
                {options.map((item) => (
                  <option key={item.dataset} value={item.dataset}>
                    {item.legal_name} · ABN {item.abn}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          {datasets.isError ? (
            <p role="alert" className="m-0 basis-full text-[13px] text-destructive">
              {t.projectsPage.datasetsFailed}
            </p>
          ) : null}
          <div className="flex flex-[1_1_160px] flex-col gap-1.5">
            <label htmlFor="new-start" className="font-semibold text-[13px]">
              {t.projectsPage.periodStart}
            </label>
            <Input
              id="new-start"
              type="date"
              className="h-11 font-mono"
              value={periodStart}
              onChange={(event) => setPeriodStart(event.target.value)}
              required
            />
          </div>
          <div className="flex flex-[1_1_160px] flex-col gap-1.5">
            <label htmlFor="new-end" className="font-semibold text-[13px]">
              {t.projectsPage.periodEnd}
            </label>
            <Input
              id="new-end"
              type="date"
              className="h-11 font-mono"
              value={periodEnd}
              onChange={(event) => setPeriodEnd(event.target.value)}
              required
            />
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-2.5">
          <Button type="button" variant="outline" size="lg" onClick={onCancel}>
            {t.projectsPage.cancel}
          </Button>
          <Button type="submit" size="lg" disabled={create.isPending || !chosen}>
            {t.projectsPage.create}
          </Button>
        </div>
      </form>
    </section>
  )
}
