import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { MessageCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { reviewsListExceptionsOptions, reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import { reviewsSubmitReviewRecord } from '@/client/sdk.gen'
import { BusyNote } from '@/components/activity-bar'
import { Code, PageHeader, SectionLabel, StatusPill } from '@/components/chrome'
import { PointerList } from '@/components/evidence-link'
import { QueryState } from '@/components/query-state'
import { Button, buttonVariants } from '@/components/ui/button'
import { VerdictBanner, VerdictCell } from '@/components/verdict'
import { GeneratedLanguage, useContentLanguage } from '@/components/working-language'
import { decisionsDisabled, invalidateEngagement } from '@/lib/engagement'
import { apiMessage, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import {
  isJudgment,
  procedureName,
  type RankedTask,
  type ReviewTier,
  rankTasks,
  splitManual,
  TIER_ORDER,
} from '@/lib/review'
import { useEngagement } from '@/lib/use-engagement'
import { useMoney } from '@/lib/use-money'

export const Route = createFileRoute('/engagements/$eid/reviews/')({
  component: ReviewQueue,
})

type Filter = 'all' | 'pending' | 'decided'
const COLS = 'grid grid-cols-[28px_minmax(0,2.4fr)_minmax(0,1.2fr)_minmax(0,1fr)_minmax(0,0.7fr)_minmax(0,1.1fr)] gap-3'
const TIER_DOT: Record<ReviewTier, string> = {
  full_review: 'bg-risk',
  check_evidence: 'bg-warn-strong',
  read_reasoning: 'bg-primary',
  batch_eligible: 'bg-[#12a27a]',
}

function ReviewQueue() {
  const { formatMoney } = useMoney()
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const overview = useEngagement(eid).data
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid } }))
  const exceptions = useQuery(reviewsListExceptionsOptions({ path: { eid } }))
  const [filter, setFilter] = useState<Filter>('all')
  const [selected, setSelected] = useState<string | null>(null)
  const [batch, setBatch] = useState<Set<string>>(new Set())
  const [batching, setBatching] = useState(false)

  const ranked = useMemo(() => {
    const byId = new Map((exceptions.data ?? []).map((item) => [item.id, item]))
    // 人工程序的任务 (manual) 在人工队列里办 (后端需求 R94), 不进复核队列
    const [reviewable] = splitManual(tasks.data ?? [])
    return rankTasks(reviewable, byId, overview)
  }, [tasks.data, exceptions.data, overview])
  // 等补充证据的任务 (需求 R369): 显示 "等待证据（不阻塞）", 不算待处理, 也不算已决定
  const isPending = (item: RankedTask) => item.view.task.status === 'pending'
  const isAwaiting = (item: RankedTask) => item.view.task.status === 'awaiting_evidence'
  const shown = ranked.filter((item) =>
    filter === 'all' ? true : filter === 'pending' ? isPending(item) : item.view.task.status === 'decided',
  )
  const counts = {
    all: ranked.length,
    pending: ranked.filter(isPending).length,
    decided: ranked.filter((item) => item.view.task.status === 'decided').length,
  }
  const current =
    ranked.find((item) => item.view.task.id === selected) ??
    ranked.find(isPending) ??
    ranked.find(isAwaiting) ??
    ranked[0] ??
    null
  const disabled = decisionsDisabled(overview)

  const confirmBatch = async () => {
    setBatching(true)
    try {
      for (const id of batch) {
        const item = ranked.find((row) => row.view.task.id === id)
        if (!item) continue
        for (let attempt = 0; attempt < 30; attempt++) {
          const result = await reviewsSubmitReviewRecord({
            path: { eid },
            body: { task_id: id, target_version: item.view.task.target_version, decision: 'confirm', reason: null },
          })
          if (!result.error) break
          if (!isApiCode(result.error, 'ENGAGEMENT_BUSY')) throw result.error
          await new Promise((resolve) => setTimeout(resolve, 1000))
        }
      }
      toast.success(t.toasts.decisionRecorded)
      setBatch(new Set())
    } catch (error) {
      toast.error(apiMessage(error))
    } finally {
      setBatching(false)
      await invalidateEngagement(queryClient, eid)
    }
  }

  return (
    <>
      <PageHeader
        title={t.queuePage.title}
        actions={
          <Link to="/engagements/$eid" params={{ eid }} search={{}} className={buttonVariants({ size: 'lg' })}>
            <MessageCircle className="size-4" aria-hidden="true" />
            {t.queuePage.next}
          </Link>
        }
      />
      <div className="flex flex-1 flex-wrap">
        <section className="flex min-w-0 flex-[999_1_560px] flex-col gap-4 px-4 py-5 sm:px-7">
          <fieldset
            aria-label={t.queuePage.title}
            className="inline-flex self-start rounded-[10px] bg-[#e9ecf1] p-[3px]"
          >
            {(['all', 'pending', 'decided'] as const).map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={filter === item}
                onClick={() => setFilter(item)}
                className={cn(
                  'min-h-8 rounded-lg px-3 text-[13px]',
                  filter === item ? 'bg-card font-semibold shadow-xs' : 'font-medium',
                )}
              >
                {t.queuePage[item]} · {counts[item]}
              </button>
            ))}
          </fieldset>
          <QueryState isPending={tasks.isPending} error={tasks.error}>
            <div className="overflow-x-auto rounded-xl border bg-card">
              <div className="min-w-[640px]">
                <div
                  className={cn(
                    COLS,
                    'border-b bg-[#fafbfc] px-4 py-2.5 font-semibold text-[12px] text-muted-foreground',
                  )}
                >
                  <span />
                  <span>{t.queuePage.cols.item}</span>
                  <span className="text-right">{t.queuePage.cols.amount}</span>
                  <span>{t.queuePage.cols.verdict}</span>
                  <span className="text-right">{t.queuePage.cols.confidence}</span>
                  <span>{t.queuePage.cols.status}</span>
                </div>
                {TIER_ORDER.map((tier) => {
                  const rows = shown.filter((item) => item.tier === tier)
                  const isBatch = tier === 'batch_eligible'
                  return (
                    <div key={tier} className="flex flex-col">
                      <div className="flex flex-wrap items-baseline gap-2 border-line-soft border-b px-4 pt-3 pb-2">
                        <span className={cn('size-2.5 self-center rounded-full', TIER_DOT[tier])} />
                        <span className="font-semibold text-[13.5px]">{t.tiers[tier]}</span>
                        <span className="text-[12.5px] text-muted-foreground">{t.tierRules[tier]}</span>
                        <span className="flex-1" />
                        <Code className="text-[12px] text-muted-foreground">{rows.length}</Code>
                      </div>
                      {rows.map((item) => {
                        const judgment = isJudgment(item.view.target) ? item.view.target : null
                        const on = current?.view.task.id === item.view.task.id
                        const pending = isPending(item)
                        const awaiting = isAwaiting(item)
                        return (
                          <div
                            key={item.view.task.id}
                            className={cn(
                              COLS,
                              'min-h-[52px] items-center border-line-soft border-b px-4 py-2.5',
                              on ? 'bg-accent shadow-[inset_3px_0_0_var(--primary)]' : 'bg-card',
                            )}
                          >
                            {isBatch && pending ? (
                              <input
                                type="checkbox"
                                aria-label={item.subject}
                                className="size-4 accent-primary"
                                checked={batch.has(item.view.task.id)}
                                onChange={(event) =>
                                  setBatch((current) => {
                                    const next = new Set(current)
                                    if (event.target.checked) next.add(item.view.task.id)
                                    else next.delete(item.view.task.id)
                                    return next
                                  })
                                }
                              />
                            ) : (
                              <span />
                            )}
                            <button
                              type="button"
                              onClick={() => setSelected(item.view.task.id)}
                              className="flex min-w-0 flex-col border-0 bg-transparent p-0 text-left"
                            >
                              <Code className="font-semibold">{item.subject}</Code>
                              <span className="truncate text-[12.5px] text-[#4a5262]">
                                {item.view.task.procedure_id}{' '}
                                {procedureName(t, item.view.task.procedure_id, item.view.procedure_name)}
                                {t.taskKinds[item.view.task.kind] ? ` · ${t.taskKinds[item.view.task.kind]}` : ''}
                              </span>
                            </button>
                            <Code className="text-right text-[13px]">{formatMoney(item.amount)}</Code>
                            {/* 定论取代原来的"建议处理"列 (需求 R368): 颜色与判断卡一致, 是错报的带金额与方向 */}
                            <VerdictCell judgment={judgment} />
                            <Code className="text-right text-[13px]">
                              {judgment ? judgment.confidence.toFixed(2) : '—'}
                            </Code>
                            <span>
                              <StatusPill tone={pending ? 'warn' : awaiting ? 'accent' : 'ok'}>
                                {pending
                                  ? t.queuePage.pending
                                  : awaiting
                                    ? t.status.awaitingEvidence
                                    : t.queuePage.decided}
                              </StatusPill>
                            </span>
                          </div>
                        )
                      })}
                      {!rows.length || (isBatch && rows.some(isPending)) ? (
                        <div className="flex flex-wrap items-center gap-3 border-line-soft border-b px-4 py-3">
                          <span className="flex-1 text-[13px] text-[#4a5262]">
                            {!rows.length ? (isBatch ? t.queuePage.batchEmpty : t.queuePage.empty) : ''}
                          </span>
                          {isBatch ? (
                            <Button
                              type="button"
                              variant="outline"
                              disabled={disabled || batching || batch.size === 0}
                              onClick={confirmBatch}
                            >
                              {t.queuePage.confirmSelected(batch.size)}
                            </Button>
                          ) : null}
                          {isBatch ? <BusyNote eid={eid} className="basis-full text-right" /> : null}
                        </div>
                      ) : null}
                    </div>
                  )
                })}
              </div>
            </div>
          </QueryState>
        </section>

        <aside className="flex flex-[1_1_360px] flex-col gap-4 border-l bg-card p-5">
          {current ? (
            <SideBySide eid={eid} item={current} />
          ) : (
            <p className="text-muted-foreground text-sm">{t.queuePage.selectHint}</p>
          )}
        </aside>
      </div>
    </>
  )
}

function SideBySide({ eid, item }: { eid: string; item: RankedTask }) {
  const { formatMoney } = useMoney()
  const { t } = useI18n()
  const judgment = isJudgment(item.view.target) ? item.view.target : null
  const lang = useContentLanguage(eid, judgment?.run_id)
  const pending = item.view.task.status === 'pending'
  const awaiting = item.view.task.status === 'awaiting_evidence'
  return (
    <>
      <div className="flex flex-col gap-1">
        <SectionLabel>{t.queuePage.sideBySide}</SectionLabel>
        <h2 className="m-0 font-semibold text-[17px]">
          <Code>{item.subject}</Code> · {procedureName(t, item.view.task.procedure_id, item.view.procedure_name)}
        </h2>
        <span className="text-[13px] text-[#4a5262]">
          {t.tiers[item.tier]} · {formatMoney(item.amount)}
        </span>
      </div>
      {judgment ? (
        <>
          <VerdictBanner judgment={judgment} />
          <div className="flex flex-col gap-1.5">
            <span className="flex flex-wrap items-center gap-2">
              <SectionLabel>{t.judgment.reason}</SectionLabel>
              <GeneratedLanguage eid={eid} runId={judgment.run_id} />
            </span>
            <p className="m-0 text-[14px] leading-[1.65]" lang={lang}>
              {judgment.conclusion}
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <SectionLabel>{t.queuePage.keyEvidence}</SectionLabel>
            <PointerList eid={eid} pointers={judgment.evidence.slice(0, 6)} />
          </div>
        </>
      ) : null}
      <div className="flex flex-wrap gap-2 pt-1">
        {pending ? (
          <Link
            to="/engagements/$eid"
            params={{ eid }}
            search={{ focus: `task:${item.view.task.id}` }}
            className={buttonVariants({ size: 'lg' })}
          >
            {t.queuePage.review}
          </Link>
        ) : null}
        {awaiting ? (
          <Link
            to="/engagements/$eid/data"
            params={{ eid }}
            className={buttonVariants({ variant: 'outline', size: 'lg' })}
          >
            {t.evidenceRequests.openBlock}
          </Link>
        ) : null}
        <Link
          to="/engagements/$eid/reviews/$taskId"
          params={{ eid, taskId: item.view.task.id }}
          className={buttonVariants({ variant: 'outline', size: 'lg' })}
        >
          {t.queuePage.viewRecord}
        </Link>
      </div>
    </>
  )
}
