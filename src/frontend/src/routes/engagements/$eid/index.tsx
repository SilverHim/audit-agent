import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { ArrowUp, ChevronUp, LayoutList, Loader2 } from 'lucide-react'
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  reportsListTrailInfiniteOptions,
  reviewsListExceptionsOptions,
  reviewsListReviewTasksOptions,
} from '@/client/@tanstack/react-query.gen'
import type { ConciergeCitation, EngagementOverview, GateView, XeroProfileCheck } from '@/client/types.gen'
import { Code, StatusPill } from '@/components/chrome'
import {
  ConciergeMessage,
  ConciergeText,
  EventLine,
  JudgmentCard,
  QuestionBubble,
  ReviewDecisionCard,
  UserBubble,
} from '@/components/concierge'
import { DataRequestCard } from '@/components/data-requests'
import { EvidenceLink } from '@/components/evidence-link'
import { GateDecisionCard, GateMaterial, useGate } from '@/components/gate-form'
import { StaleNotice } from '@/components/rerun'
import { Button, buttonVariants } from '@/components/ui/button'
import { XeroProfileCard } from '@/components/xero-profile'
import { type DataRequest, useDataRequests, useDataSummary } from '@/lib/data-api'
import { decisionsDisabled } from '@/lib/engagement'
import { errorText, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import { deferralKey, NotAvailable, streamConcierge, useDeferrals, useSetDeferral } from '@/lib/new-api'
import { type RankedTask, rankTasks } from '@/lib/review'
import {
  buildTimeline,
  earlierCursor,
  newestTrailId,
  TIMELINE_KINDS,
  type TimelineItem,
  TRAIL_PAGE,
  trailInOrder,
} from '@/lib/timeline'
import { useEngagement } from '@/lib/use-engagement'
import { pickCurrent, workspaceReady } from '@/lib/workspace'
import { skippedByReason } from '@/lib/xero'
import { pendingCheck } from '@/lib/xero-profile'
import type { Messages } from '@/locales/en'

type Search = { focus?: string }

export const Route = createFileRoute('/engagements/$eid/')({
  validateSearch: (search: Record<string, unknown>): Search => ({
    focus: typeof search.focus === 'string' ? search.focus : undefined,
  }),
  component: Workspace,
})

type Current =
  | { kind: 'gate'; key: string; view: GateView; deferred: boolean }
  | {
      kind: 'requests'
      key: string
      requests: DataRequest[]
      marked: DataRequest[]
      awaiting: boolean
      deferred: boolean
    }
  | { kind: 'xero'; key: string; check: XeroProfileCheck; deferred: boolean }
  | { kind: 'task'; key: string; ranked: RankedTask; deferred: boolean }

type Turn = {
  id: string
  question: string
  answer: string
  citations: ConciergeCitation[]
  status: 'streaming' | 'done' | 'failed'
  tool?: string | null
  trailId?: string | null
}

function Workspace() {
  const { eid } = Route.useParams()
  const { focus } = Route.useSearch()
  const navigate = useNavigate()
  const { t, lang } = useI18n()
  const overview = useEngagement(eid).data
  const gate = useGate(eid, Boolean(overview?.current_gate))
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid }, query: { status: 'pending' } }))
  const exceptions = useQuery(reviewsListExceptionsOptions({ path: { eid } }))
  // 时间线先取最近的一页留痕, "更早" 往前翻 (见 lib/timeline 的 earlierCursor)
  const trail = useInfiniteQuery({
    ...reportsListTrailInfiniteOptions({
      path: { eid },
      query: { newest: true, limit: TRAIL_PAGE, kind: TIMELINE_KINDS },
    }),
    initialPageParam: { path: { eid }, query: {} },
    getNextPageParam: (page) => {
      const before = earlierCursor(page)
      return before ? { path: { eid }, query: { before } } : undefined
    },
  })
  const deferrals = useDeferrals(eid)
  const setDeferral = useSetDeferral(eid)
  const requests = useDataRequests(eid)
  const summary = useDataSummary(eid)
  const [turns, setTurns] = useState<Turn[]>([])
  const bottom = useRef<HTMLDivElement>(null)
  const currentRef = useRef<HTMLDivElement>(null)

  const deferredKeys = useMemo(() => new Set((deferrals.data ?? []).map(deferralKey)), [deferrals.data])
  const ranked = useMemo(() => {
    const byId = new Map((exceptions.data ?? []).map((item) => [item.id, item]))
    return rankTasks(tasks.data ?? [], byId, overview)
  }, [tasks.data, exceptions.data, overview])

  const candidates: Current[] = useMemo(() => {
    const list: Current[] = []
    if (overview?.current_gate && gate.data) {
      const key = `gate:${gate.data.gate}`
      list.push({ kind: 'gate', key, view: gate.data, deferred: deferredKeys.has(key) })
    }
    // 补交资料合成一项, 排在阶段决定之后, 复核任务之前; 标了客户无此类资料的一并列出 (只读)
    const open = (requests.data ?? []).filter((item) => item.status === 'open')
    const marked = (requests.data ?? []).filter((item) => item.status === 'unavailable')
    const awaiting = Boolean(overview?.awaiting_data)
    if (open.length || awaiting) {
      const deferred = open.length > 0 && open.every((item) => deferredKeys.has(`request:${item.id}`))
      list.push({ kind: 'requests', key: 'requests', requests: open, marked, awaiting, deferred })
    }
    // Xero 组织信息与客户画像不一致 (后端 R291): 排在补交资料之后, 复核任务之前; 锁定的项目不提示
    const check = pendingCheck(summary.data?.sources.xero, Boolean(overview?.engagement.locked))
    if (check) {
      const key = `xero:${check.key}`
      list.push({ kind: 'xero', key, check, deferred: deferredKeys.has(key) })
    }
    for (const item of ranked) {
      const key = `task:${item.view.task.id}`
      list.push({ kind: 'task', key, ranked: item, deferred: deferredKeys.has(key) })
    }
    return list
  }, [overview, gate.data, ranked, deferredKeys, requests.data, summary.data])

  // 相关查询都返回之后才确定当前事项 (后端 R152: 免得先闪过一条复核任务, 给审计师并没打开的任务记一次呈递)
  const ready = workspaceReady(overview, { gate, tasks, exceptions, deferrals, requests, summary })
  const current = ready ? pickCurrent(candidates, focus) : null
  const deferredCount = candidates.filter((item) => item.deferred).length

  const timeline = useMemo(
    () => buildTimeline(trailInOrder(trail.data?.pages ?? []), !trail.hasNextPage),
    [trail.data, trail.hasNextPage],
  )
  const localTrailIds = new Set(turns.flatMap((turn) => (turn.trailId ? [turn.trailId] : [])))
  const history = timeline.filter((item) => item.kind !== 'qa' || !localTrailIds.has(item.id))

  // 当前事项变化或有新事件时把它的开头滚到顶部; 判断详情异步加载会往下撑高, 所以不滚到底.
  // 看的是最新一条留痕 (本页刚问的问答除外), 点 "更早" 加在前面的不触发, 不把人从刚翻出的记录拉走
  const newest = newestTrailId(trail.data?.pages ?? [], localTrailIds)
  // biome-ignore lint/correctness/useExhaustiveDependencies: 当前事项或最新一条留痕变化时重新定位
  useEffect(() => {
    currentRef.current?.scrollIntoView({ block: 'start' })
  }, [current?.key, newest])

  // 有新问答或回答在输出时滚到底部
  const lastAnswer = turns.at(-1)?.answer.length ?? 0
  // biome-ignore lint/correctness/useExhaustiveDependencies: 问答变化时滚到底部
  useEffect(() => {
    if (turns.length) bottom.current?.scrollIntoView({ block: 'end' })
  }, [turns.length, lastAnswer])

  const disabled = decisionsDisabled(overview)

  const defer = (item: Current) => {
    const targets =
      item.kind === 'gate'
        ? [{ target_type: 'gate' as const, target_id: item.view.gate }]
        : item.kind === 'requests'
          ? item.requests.map((request) => ({ target_type: 'data_request' as const, target_id: request.id }))
          : item.kind === 'xero'
            ? [{ target_type: 'xero_profile' as const, target_id: item.check.key }]
            : [{ target_type: 'review_task' as const, target_id: item.ranked.view.task.id }]
    Promise.all(targets.map((target) => setDeferral.mutateAsync({ ...target, deferred: true }))).then(
      () => {
        toast.success(t.toasts.deferred)
        if (focus) void navigate({ to: '/engagements/$eid', params: { eid }, search: {} })
      },
      (error: unknown) => toast.error(errorText(error, t)),
    )
  }

  const ask = async (question: string, quick?: 'where' | 'what') => {
    const id = crypto.randomUUID()
    const update = (patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) =>
      setTurns((list) =>
        list.map((turn) =>
          turn.id === id ? { ...turn, ...(typeof patch === 'function' ? patch(turn) : patch) } : turn,
        ),
      )
    setTurns((list) => [...list, { id, question, answer: '', citations: [], status: 'streaming' }])
    try {
      const result = await streamConcierge(
        eid,
        { question, lang },
        {
          onTool: (label) => update({ tool: label }),
          onDelta: (text) => update((turn) => ({ answer: turn.answer + text, tool: null })),
          onCitations: (citations) => update({ citations }),
        },
      )
      update({ status: 'done', trailId: result.trail_id })
    } catch (error) {
      const local =
        error instanceof NotAvailable && quick ? localAnswer(quick, t, overview, candidates, deferredCount) : null
      if (local) update({ answer: local, status: 'done' })
      else
        update({
          answer: error instanceof NotAvailable ? t.ask.failed : askFailure(error, t),
          status: 'failed',
        })
    }
  }

  const formView =
    current?.kind === 'gate'
      ? '/engagements/$eid/gate'
      : current?.kind === 'xero'
        ? '/engagements/$eid/data'
        : '/engagements/$eid/decisions'
  const pendingLabel = overview?.current_gate
    ? (t.gates[overview.current_gate] ?? overview.current_gate)
    : overview && overview.pending_tasks > 0
      ? t.workspace.items(overview.pending_tasks)
      : null

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b bg-card px-4 py-3.5 sm:px-7">
        <div className="flex min-w-0 flex-col">
          <h1 className="m-0 font-semibold text-base tracking-tight">{overview?.client_name ?? '…'}</h1>
          <span className="text-[12.5px] text-muted-foreground">
            <Code>{eid}</Code>
            {overview ? ` · ${t.projectsPage.yearEnded(formatYearEnd(overview, lang))}` : ''}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          {overview?.running ? (
            <StatusPill tone="accent">
              <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
              {t.workspace.runningBadge}
            </StatusPill>
          ) : pendingLabel ? (
            <StatusPill tone="warn">
              <span className="size-2 rounded-full bg-warn-strong" />
              {t.workspace.pendingBadge(pendingLabel)}
            </StatusPill>
          ) : null}
          <Link
            to={current?.kind === 'task' ? '/engagements/$eid/reviews/$taskId' : formView}
            params={current?.kind === 'task' ? { eid, taskId: current.ranked.view.task.id } : { eid }}
            className={buttonVariants({ variant: 'outline' })}
          >
            <LayoutList className="size-4" aria-hidden="true" />
            {t.workspace.formView}
          </Link>
        </div>
      </header>

      <div className="flex-1 px-4 pt-7 pb-2 sm:px-7">
        <div className="mx-auto flex max-w-[780px] flex-col gap-5">
          {trail.hasNextPage ? (
            <div className="flex justify-center">
              <Button
                variant="ghost"
                size="sm"
                disabled={trail.isFetchingNextPage}
                onClick={() => void trail.fetchNextPage()}
              >
                {trail.isFetchingNextPage ? (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                ) : (
                  <ChevronUp aria-hidden="true" />
                )}
                {t.workspace.earlier}
              </Button>
            </div>
          ) : null}
          {history.map((item) => (
            <HistoryItem key={item.key} item={item} overview={overview} eid={eid} />
          ))}

          <div ref={currentRef} className="flex scroll-mt-6 flex-col gap-5">
            <StaleNotice eid={eid} overview={overview} />
            <CurrentItem
              eid={eid}
              overview={overview}
              ready={ready}
              current={current}
              reviewTotal={candidates.filter((item) => item.kind === 'task').length}
              deferredCount={deferredCount}
              disabled={disabled}
              onDefer={defer}
              deferring={setDeferral.isPending}
              onOpenDeferred={() => {
                const first = candidates.find((item) => item.deferred)
                if (first) void navigate({ to: '/engagements/$eid', params: { eid }, search: { focus: first.key } })
              }}
            />
          </div>

          {turns.map((turn) => (
            <div key={turn.id} className="flex flex-col gap-4">
              <QuestionBubble>{turn.question}</QuestionBubble>
              <ConciergeMessage>
                {turn.status === 'streaming' && !turn.answer ? (
                  <span className="inline-flex items-center gap-2 text-muted-foreground text-sm">
                    <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                    {turn.tool ? t.ask.looking(turn.tool) : t.ask.answering}
                  </span>
                ) : (
                  <ConciergeText>{turn.answer}</ConciergeText>
                )}
                <Citations eid={eid} citations={turn.citations} />
              </ConciergeMessage>
            </div>
          ))}
          <div ref={bottom} className="scroll-mb-36" />
        </div>
      </div>

      <Composer onAsk={ask} busy={turns.some((turn) => turn.status === 'streaming')} eid={eid} />
    </div>
  )
}

// 问答失败的提示: 正在回答上一个问题, 模型设置有问题, 其余 (模型不可用, 上下文超长等) 一律请稍后重试
function askFailure(error: unknown, t: Messages): string {
  if (isApiCode(error, 'CONVERSATION_BUSY')) return t.ask.busy
  if (isApiCode(error, 'SETTINGS_KEY_MISSING') || isApiCode(error, 'SETTINGS_KEY_INVALID')) return t.ask.settings
  return t.ask.failed
}

function formatYearEnd(overview: EngagementOverview, lang: 'zh' | 'en'): string {
  const date = new Date(`${overview.engagement.period_end}T00:00:00`)
  return new Intl.DateTimeFormat(lang === 'zh' ? 'zh-CN' : 'en-AU', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  }).format(date)
}

type DataItem = Extract<TimelineItem, { kind: 'data' }>

// Xero 同步没写的表, 同一原因的合成一句 (如 "银行账户、银行流水（对账单接口无权限）"); 分组同卡片的上次结果
// (lib/xero.skippedByReason); 实际只有银行这一对
function skippedText(skipped: Record<string, string>, t: Messages, lang: 'zh' | 'en'): string {
  return skippedByReason(skipped, t, lang)
    .map((item) => t.data.events.xero_synced(item.tables, item.reason))
    .join(' · ')
}

// 数据管理留痕的一行文案; 不认识的 action 不显示
function dataEventText(item: DataItem, t: Messages, lang: 'zh' | 'en'): string | null {
  const table = item.table ? (t.data.tables[item.table] ?? item.table) : ''
  const subject = table || (item.document ? (t.data.docTypes[item.document] ?? item.document) : (item.file ?? ''))
  const events = t.data.events
  switch (item.action) {
    case 'import':
      return events.import(table, item.records ?? 0)
    case 'sync':
      return events.sync(table, item.records ?? 0)
    case 'xero_connect':
      return events.xero_connect(item.organisation)
    case 'xero_tenant':
      return events.xero_tenant(item.organisation ?? '')
    case 'xero_sync_failed':
      return events.xero_sync_failed(events.syncFailure[item.reason ?? ''] ?? item.reason ?? '')
    case 'xero_synced':
      return item.skipped ? skippedText(item.skipped, t, lang) : null // 写了的表各有一条 sync, 这里只说没写的
    case 'xero_disconnect':
      return events.xero_disconnect(item.revoked !== false)
    case 'xero_profile_kept':
      return events.xero_profile_kept(item.profileVersion ?? 0, item.reason ?? '')
    case 'xero_profile_updated':
      return events.xero_profile_updated(item.version ?? 0, item.adopted === true)
    case 'upload':
      return events.upload(item.file ?? '')
    case 'edit':
      return events.edit(subject)
    case 'request_raised':
      return events.request_raised(subject)
    case 'request_received':
      return events.request_received(subject)
    case 'ready':
      return events.ready
    case 'not_imported':
      return events.not_imported(item.file ?? '')
    default:
      return null
  }
}

function HistoryItem({
  item,
  overview,
  eid,
}: {
  item: TimelineItem
  overview: EngagementOverview | undefined
  eid: string
}) {
  const { lang, t, formatDate } = useI18n()
  if (item.kind === 'created') {
    if (!overview) return null
    return (
      <EventLine>
        {t.workspace.created(formatDate(overview.engagement.period_start), formatDate(overview.engagement.period_end))}
      </EventLine>
    )
  }
  if (item.kind === 'phase')
    return <EventLine>{t.workspace.phaseEntered(t.phases[item.phase] ?? item.phase)}</EventLine>
  if (item.kind === 'completed')
    return <EventLine>{t.workspace.completed(item.ids.join(lang === 'zh' ? '、' : ', '))}</EventLine>
  if (item.kind === 'deferral') {
    const target = item.xero
      ? t.xeroProfile.item
      : item.request
        ? (t.data.tables[item.request] ?? t.data.docTypes[item.request] ?? item.target)
        : item.target
    return <EventLine>{item.deferred ? t.workspace.deferredEvent(target) : t.workspace.resumedEvent(target)}</EventLine>
  }
  if (item.kind === 'data') {
    const text = dataEventText(item, t, lang)
    return text ? <EventLine>{text}</EventLine> : null
  }
  if (item.kind === 'decision') {
    const label = item.gate
      ? (t.verdicts[item.gate][item.verdict as 'approve'] ?? item.verdict)
      : (t.decisions[item.verdict] ?? item.verdict)
    return (
      <UserBubble title={t.workspace.decisionRecorded(item.target)}>
        <span className="font-semibold">{label}</span>
        <span className="whitespace-pre-wrap text-[14px] text-secondary-foreground">
          {item.reason ? `“${item.reason}”` : t.workspace.noReason}
        </span>
      </UserBubble>
    )
  }
  return (
    <div className="flex flex-col gap-4">
      <QuestionBubble>{item.question}</QuestionBubble>
      <ConciergeMessage>
        <ConciergeText>{item.answer}</ConciergeText>
        {item.aborted ? <span className="text-[12.5px] text-muted-foreground">{t.ask.aborted}</span> : null}
        <Citations eid={eid} citations={item.citations} />
      </ConciergeMessage>
    </div>
  )
}

function Citations({ eid, citations }: { eid: string; citations: ConciergeCitation[] }) {
  const { t } = useI18n()
  if (!citations.length) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[12px] text-muted-foreground">{t.ask.sources}</span>
      {citations.map((citation) =>
        citation.pointer ? (
          <EvidenceLink
            key={`${citation.kind}:${citation.id}`}
            eid={eid}
            pointer={citation.pointer}
            label={citation.label}
          />
        ) : citation.kind === 'procedure' ? (
          <Link
            key={`${citation.kind}:${citation.id}`}
            to="/engagements/$eid/procedures"
            params={{ eid }}
            search={{ selected: citation.id }}
            className="rounded-md border px-2 py-0.5 font-mono text-[12px] text-accent-foreground hover:bg-accent"
          >
            {citation.label || citation.id}
          </Link>
        ) : (
          <span key={`${citation.kind}:${citation.id}`} className="rounded-md border px-2 py-0.5 font-mono text-[12px]">
            {citation.label || citation.id}
          </span>
        ),
      )}
    </div>
  )
}

function CurrentItem({
  eid,
  overview,
  ready,
  current,
  reviewTotal,
  deferredCount,
  disabled,
  onDefer,
  deferring,
  onOpenDeferred,
}: {
  eid: string
  overview: EngagementOverview | undefined
  ready: boolean
  current: Current | null
  reviewTotal: number
  deferredCount: number
  disabled: boolean
  onDefer: (item: Current) => void
  deferring: boolean
  onOpenDeferred: () => void
}) {
  const { t } = useI18n()
  if (!overview) return null
  if (!ready) {
    return (
      <ConciergeMessage>
        <span className="inline-flex items-center gap-2 text-[15px]" data-testid="workspace-loading">
          <Loader2 className="size-4 animate-spin text-primary" aria-hidden="true" />
          {t.workspace.loadingItems}
        </span>
      </ConciergeMessage>
    )
  }
  if (overview.running && !current) {
    return (
      <ConciergeMessage>
        <span className="inline-flex items-center gap-2 text-[15px]">
          <Loader2 className="size-4 animate-spin text-primary" aria-hidden="true" />
          {t.workspace.running}
        </span>
      </ConciergeMessage>
    )
  }
  if (!current) {
    const text =
      overview.engagement.phase === 'Archived'
        ? t.workspace.archived
        : overview.engagement.phase === 'Declined'
          ? t.workspace.declined
          : deferredCount
            ? t.workspace.deferredWaiting(deferredCount)
            : t.workspace.nothingPending
    return (
      <ConciergeMessage>
        <ConciergeText>{text}</ConciergeText>
        {deferredCount ? (
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={onOpenDeferred}>
              {t.workspace.decideNow}
            </Button>
            <Link to="/engagements/$eid/decisions" params={{ eid }} className={buttonVariants({ variant: 'outline' })}>
              {t.workspace.openDecisions}
            </Link>
          </div>
        ) : null}
      </ConciergeMessage>
    )
  }

  if (current.kind === 'requests') {
    return (
      <div className="flex flex-col gap-4">
        <ConciergeMessage>
          <ConciergeText>
            {current.requests.length ? t.requests.intro(current.requests.length) : t.requests.awaitingIntro}
          </ConciergeText>
        </ConciergeMessage>
        <DataRequestCard
          eid={eid}
          requests={current.requests}
          marked={current.marked}
          awaiting={current.awaiting}
          disabled={disabled}
          deferred={current.deferred}
          onDefer={() => onDefer(current)}
          deferring={deferring}
        />
      </div>
    )
  }

  if (current.kind === 'gate') {
    return (
      <div className="flex flex-col gap-4">
        <ConciergeMessage>
          <ConciergeText>{gateIntro(t, current.view)}</ConciergeText>
          <GateMaterial eid={eid} view={current.view} />
        </ConciergeMessage>
        <GateDecisionCard
          key={`${current.view.gate}:${current.view.target_version}`}
          eid={eid}
          view={current.view}
          disabled={disabled}
          deferred={current.deferred}
          onDefer={() => onDefer(current)}
          deferring={deferring}
        />
      </div>
    )
  }

  if (current.kind === 'xero') {
    return (
      <div className="flex flex-col gap-4">
        <ConciergeMessage>
          <ConciergeText>{t.xeroProfile.workspaceIntro}</ConciergeText>
        </ConciergeMessage>
        <XeroProfileCard
          key={current.check.key}
          eid={eid}
          check={current.check}
          disabled={disabled}
          inset
          deferred={current.deferred}
          onDefer={() => onDefer(current)}
          deferring={deferring}
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <ConciergeMessage>
        <ConciergeText>
          {t.workspace.reviewIntro(reviewTotal, t.tiers[current.ranked.tier])}
          {deferredCount ? ` ${t.workspace.deferredIntro(deferredCount)}` : ''}
        </ConciergeText>
        <JudgmentCard eid={eid} ranked={current.ranked} />
      </ConciergeMessage>
      <ReviewDecisionCard
        key={current.ranked.view.task.id}
        eid={eid}
        ranked={current.ranked}
        disabled={disabled}
        deferred={current.deferred}
        onDefer={() => onDefer(current)}
        deferring={deferring}
      />
    </div>
  )
}

function gateIntro(t: Messages, view: GateView): ReactNode {
  const material = view.material
  if (material.kind === 'ACC-01') {
    const checks = material.scorecard.registry_checks
    return t.workspace.gateIntro['ACC-01'](
      checks.filter((check) => check.passed === true).length,
      checks.length,
      material.exceptions.length,
      checks.filter((check) => check.passed === null).length,
    )
  }
  if (material.kind === 'PLN-01/03') return t.workspace.gateIntro['PLN-01/03']
  if (material.kind === 'FIN-01') {
    return t.workspace.gateIntro['FIN-01'](
      material.misstatements.length,
      formatAud(material.schedule.total_cents),
      material.not_executed?.length ?? 0,
    )
  }
  return t.workspace.gateIntro['RPT-01'](material.report.revision)
}

function localAnswer(
  quick: 'where' | 'what',
  t: Messages,
  overview: EngagementOverview | undefined,
  candidates: Current[],
  deferredCount: number,
): string | null {
  if (!overview) return null
  if (quick === 'where') {
    const count = (status: string) => overview.procedures.filter((item) => item.status === status).length
    const head = [
      t.workspace.answers.where(t.phases[overview.engagement.phase], count('completed'), overview.procedures.length),
      t.workspace.answers.notRun(count('awaiting_data'), count('no_data')),
    ]
      .filter(Boolean)
      .join(' ')
    const materiality =
      overview.materiality_cents != null
        ? ` ${t.workspace.answers.materiality(formatAud(overview.materiality_cents), formatAud(overview.performance_materiality_cents))}`
        : ''
    return head + materiality
  }
  const gate = overview.current_gate ? `${overview.current_gate} ${t.gates[overview.current_gate]}` : null
  const reviews = candidates.filter((item) => item.kind === 'task' && !item.deferred).length
  return t.workspace.answers.what(gate, reviews, deferredCount)
}

function Composer({
  onAsk,
  busy,
  eid,
}: {
  onAsk: (question: string, quick?: 'where' | 'what') => void
  busy: boolean
  eid: string
}) {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const send = () => {
    const question = text.trim()
    if (!question || busy) return
    setText('')
    onAsk(question)
  }
  return (
    <div className="sticky bottom-0 border-t bg-background/95 px-4 pt-3 pb-5 backdrop-blur sm:px-7">
      <div className="mx-auto flex max-w-[780px] flex-col gap-2.5">
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onAsk(t.workspace.quick.where, 'where')}
            className="min-h-8 rounded-full border border-input bg-card px-3 py-1 text-[13px] hover:bg-muted disabled:opacity-50"
          >
            {t.workspace.quick.where}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onAsk(t.workspace.quick.what, 'what')}
            className="min-h-8 rounded-full border border-input bg-card px-3 py-1 text-[13px] hover:bg-muted disabled:opacity-50"
          >
            {t.workspace.quick.what}
          </button>
          <Link
            to="/engagements/$eid/trail"
            params={{ eid }}
            className="inline-flex min-h-8 items-center rounded-full border border-input bg-card px-3 py-1 text-[13px] hover:bg-muted"
          >
            {t.workspace.quick.trail}
          </Link>
        </div>
        <form
          className="flex items-end gap-2.5 rounded-[14px] border border-input bg-card py-2.5 pr-2.5 pl-4 shadow-xs"
          onSubmit={(event) => {
            event.preventDefault()
            send()
          }}
        >
          <label htmlFor="ask" className="sr-only">
            {t.ask.placeholder}
          </label>
          <textarea
            id="ask"
            rows={1}
            value={text}
            maxLength={2000}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                send()
              }
            }}
            placeholder={t.ask.placeholder}
            className="max-h-40 min-h-9 flex-1 resize-none border-0 bg-transparent py-1.5 text-[15px] outline-none"
          />
          <button
            type="submit"
            aria-label={t.ask.send}
            disabled={busy || !text.trim()}
            className="flex size-10 shrink-0 items-center justify-center rounded-[10px] bg-primary text-white disabled:opacity-40"
          >
            <ArrowUp className="size-[18px]" aria-hidden="true" />
          </button>
        </form>
      </div>
    </div>
  )
}
