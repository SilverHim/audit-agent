import { Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import {
  type ActivityFailure,
  activityCurrent,
  activityKey,
  activityText,
  busyText,
  currentActivities,
  failureKey,
  failureText,
  timingText,
} from '@/lib/activity'
import { useDemoImport, useRetryBatch, useXeroSync } from '@/lib/data-api'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useOverview } from '@/lib/use-engagement'

// 系统忙时的说明 (需求 R362): 项目各页顶部常驻的状态条, 与因忙变灰的按钮旁的说明 (BusyNote). 都按总览的当前活动与
// 最近一次失败 (lib/activity); 总览在有活动时每 2 秒重取, 活动结束后状态条消失, 本项目的查询由布局
// (routes/engagements/$eid/route) 失效重取

// 关掉的失败 ("知道了") 记在本浏览器 (后端只在内存里存最近一次失败, 同种作业再开始时清除); 存不了时只在本页记
const DISMISSED = 'activity-failure-dismissed'

function readDismissed(): string | null {
  try {
    return localStorage.getItem(DISMISSED)
  } catch {
    return null
  }
}

function writeDismissed(key: string) {
  try {
    localStorage.setItem(DISMISSED, key)
  } catch {
    // 存不了 (隐私模式等) 时只在本页记
  }
}

// 有活动时每秒走一次, 已用时间与停滞提示随之更新
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  return now
}

export function ActivityBar({ eid }: { eid: string }) {
  const { t } = useI18n()
  const overview = useOverview(eid)
  const activities = currentActivities(overview)
  const failure = overview?.last_failure ?? null
  const now = useNow(activities.length > 0)
  const [dismissed, setDismissed] = useState(readDismissed)

  if (activities.length) {
    return (
      <section
        aria-label={t.activity.label}
        className="sticky top-0 z-20 border-b bg-accent px-4 py-2 text-[13.5px] sm:px-7"
        data-testid="activity-bar"
      >
        <ul role="status" className="m-0 flex list-none flex-col gap-1 p-0">
          {activities.map((activity) => {
            const current = activityCurrent(activity)
            const timing = timingText(activity, now, t)
            return (
              <li key={activityKey(activity)} className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5">
                <Loader2 className="size-4 shrink-0 animate-spin text-primary" aria-hidden="true" />
                <span className="font-medium text-accent-foreground">{activityText(activity, t)}</span>
                {current ? (
                  <span className="min-w-0 break-all text-[#4a5262]">{t.activity.current(current)}</span>
                ) : null}
                {timing ? <span className="text-muted-foreground">{timing}</span> : null}
              </li>
            )
          })}
        </ul>
      </section>
    )
  }

  if (!failure || failureKey(failure) === dismissed) return null
  const dismiss = () => {
    const key = failureKey(failure)
    writeDismissed(key)
    setDismissed(key)
  }
  return (
    <section
      aria-label={t.activity.label}
      className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b bg-risk-soft px-4 py-2 text-[13.5px] sm:px-7"
      data-testid="activity-failure"
    >
      <AlertTriangle className="size-4 shrink-0 text-destructive" aria-hidden="true" />
      <span role="alert" className="min-w-0 flex-1 break-words text-destructive">
        {failureText(failure, t)}
      </span>
      <RetryEntry eid={eid} failure={failure} />
      <Button type="button" variant="ghost" size="sm" onClick={dismiss}>
        {t.activity.dismiss}
      </Button>
    </section>
  )
}

// 失败的重试入口, 按后端给的 retry (R361): 重新载入演示资料的那一阶段, 重新识别批次, 重新同步 Xero; 没有现成入口的
// 数据作业 (如文件识别) 指向数据管理页, 程序执行的失败不在这里 (走复核队列与 run_error 的继续执行)
function RetryEntry({ eid, failure }: { eid: string; failure: ActivityFailure }) {
  const { t } = useI18n()
  const subject = failure.subject_id ?? null
  if (failure.retry === 'demo_import' && subject) return <RetryDemo eid={eid} stage={subject} />
  if (failure.retry === 'import_batch' && subject) return <RetryBatch eid={eid} batchId={subject} />
  if (failure.retry === 'xero_sync') return <RetryXero eid={eid} />
  if (failure.kind === 'procedure') return null
  return (
    <Link to="/engagements/$eid/data" params={{ eid }} search={{}} className="text-primary hover:underline">
      {t.requests.open}
    </Link>
  )
}

function RetryButton({ pending, start }: { pending: boolean; start: () => void }) {
  const { t } = useI18n()
  return (
    <Button type="button" variant="outline" size="sm" disabled={pending} onClick={start}>
      {pending ? t.activity.retrying : t.activity.retry}
    </Button>
  )
}

function useRetryHandlers() {
  const { t } = useI18n()
  return {
    onSuccess: () => toast.success(t.activity.retried),
    onError: (error: unknown) => toast.error(errorText(error, t)),
  }
}

function RetryDemo({ eid, stage }: { eid: string; stage: string }) {
  const run = useDemoImport(eid)
  const handlers = useRetryHandlers()
  return <RetryButton pending={run.isPending} start={() => run.mutate(stage, handlers)} />
}

function RetryBatch({ eid, batchId }: { eid: string; batchId: string }) {
  const run = useRetryBatch(eid, batchId)
  const handlers = useRetryHandlers()
  return <RetryButton pending={run.isPending} start={() => run.mutate(undefined, handlers)} />
}

function RetryXero({ eid }: { eid: string }) {
  const run = useXeroSync(eid)
  const handlers = useRetryHandlers()
  return <RetryButton pending={run.isPending} start={() => run.mutate(undefined, handlers)} />
}

// 因忙变灰的按钮旁的说明: 正在进行的活动与进度, 完成后可继续; 不忙时不显示
export function BusyNote({ eid, className }: { eid: string; className?: string }) {
  const { t } = useI18n()
  const text = busyText(useOverview(eid), t)
  if (!text) return null
  return (
    <span className={cn('text-[12.5px] text-muted-foreground', className)} data-testid="busy-note">
      {text}
    </span>
  )
}
