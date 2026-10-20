import { createFileRoute, Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { useI18n } from '@/lib/i18n'
import { useManualTasks } from '@/lib/manual-api'
import { procedureName } from '@/lib/review'

export const Route = createFileRoute('/engagements/$eid/manual/')({ component: ManualQueue })

const COLS = 'grid grid-cols-[minmax(0,1.4fr)_minmax(0,2.4fr)_minmax(0,0.8fr)_minmax(0,1fr)] gap-3'

// 人工队列 (后端需求 R94): 人工程序派给人的任务, 待执行的在前 (后端排好), 点进去执行并记录结果
function ManualQueue() {
  const { eid } = Route.useParams()
  const { t, formatDateTime } = useI18n()
  const tasks = useManualTasks(eid)
  return (
    <>
      <PageHeader title={t.manualPage.title} subtitle={t.manualPage.subtitle} />
      <div className="flex flex-col gap-4 px-4 py-5 sm:px-7">
        <QueryState isPending={tasks.isPending} error={tasks.error}>
          {(tasks.data ?? []).length ? (
            <div className="overflow-x-auto rounded-xl border bg-card">
              <div className="min-w-[640px]">
                <div
                  className={cn(
                    COLS,
                    'border-b bg-[#fafbfc] px-4 py-2.5 font-semibold text-[12px] text-muted-foreground',
                  )}
                >
                  <span>{t.manualPage.cols.procedure}</span>
                  <span>{t.manualPage.cols.instructions}</span>
                  <span>{t.manualPage.cols.status}</span>
                  <span>{t.manualPage.cols.assigned}</span>
                </div>
                {(tasks.data ?? []).map((view) => {
                  const pending = view.task.status === 'pending'
                  return (
                    <div
                      key={view.task.id}
                      className={cn(
                        COLS,
                        'min-h-[52px] items-center border-line-soft border-b px-4 py-2.5 last:border-b-0',
                      )}
                    >
                      <Link
                        to="/engagements/$eid/manual/$taskId"
                        params={{ eid, taskId: view.task.id }}
                        className="flex min-w-0 flex-col"
                      >
                        <Code className="font-semibold">{view.task.procedure_id}</Code>
                        <span className="truncate text-[12.5px] text-[#4a5262]">
                          {procedureName(t, view.task.procedure_id, view.procedure_name)}
                        </span>
                      </Link>
                      <span className="line-clamp-2 text-[13px]" lang="en">
                        {view.request.instructions}
                      </span>
                      <span>
                        <StatusPill tone={pending ? 'warn' : 'ok'}>
                          {pending ? t.manualPage.pending : t.manualPage.decided}
                        </StatusPill>
                      </span>
                      <span className="text-[13px] text-muted-foreground">{formatDateTime(view.task.created_at)}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          ) : (
            <p className="text-muted-foreground text-sm">{t.manualPage.empty}</p>
          )}
        </QueryState>
      </div>
    </>
  )
}
