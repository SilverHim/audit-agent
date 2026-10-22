import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { MessageCircle } from 'lucide-react'
import { useMemo } from 'react'
import { toast } from 'sonner'
import { reviewsListExceptionsOptions, reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { JudgmentCard, ReviewDecisionCard } from '@/components/concierge'
import { QueryState } from '@/components/query-state'
import { buttonVariants } from '@/components/ui/button'
import { WithdrawDecision } from '@/components/withdraw-decision'
import { decisionsDisabled } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { deferralKey, useDeferrals, useSetDeferral } from '@/lib/new-api'
import { procedureName, rankTasks } from '@/lib/review'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/reviews/$taskId')({ component: ReviewTaskPage })

// 表单视图: 单个复核事项, 与对话里呈现的是同一张判断卡片和决定卡片
function ReviewTaskPage() {
  const { eid, taskId } = Route.useParams()
  const { t } = useI18n()
  const navigate = useNavigate()
  const overview = useEngagement(eid).data
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid } }))
  const exceptions = useQuery(reviewsListExceptionsOptions({ path: { eid } }))
  const deferrals = useDeferrals(eid)
  const setDeferral = useSetDeferral(eid)
  const ranked = useMemo(() => {
    const view = tasks.data?.find((task) => task.task.id === taskId)
    if (!view) return null
    const byId = new Map((exceptions.data ?? []).map((item) => [item.id, item]))
    return rankTasks([view], byId, overview)[0] ?? null
  }, [tasks.data, exceptions.data, overview, taskId])
  const deferred = (deferrals.data ?? []).some((item) => deferralKey(item) === `task:${taskId}`)
  const pending = ranked?.view.task.status === 'pending'
  const awaiting = ranked?.view.task.status === 'awaiting_evidence'

  return (
    <>
      <PageHeader
        title={
          ranked ? (
            <>
              <Code>{ranked.subject}</Code> ·{' '}
              {procedureName(t, ranked.view.task.procedure_id, ranked.view.procedure_name)}
            </>
          ) : (
            taskId
          )
        }
        subtitle={t.workspace.formView}
        actions={
          <Link
            to="/engagements/$eid"
            params={{ eid }}
            search={pending ? { focus: `task:${taskId}` } : {}}
            className={buttonVariants({ variant: 'outline', size: 'lg' })}
          >
            <MessageCircle className="size-4" aria-hidden="true" />
            {t.formView.backToConcierge}
          </Link>
        }
      />
      <div className="mx-auto flex w-full max-w-[900px] flex-col gap-5 px-4 py-6 sm:px-7">
        <QueryState isPending={tasks.isPending} error={tasks.error}>
          {ranked ? (
            <>
              <JudgmentCard eid={eid} ranked={ranked} />
              {pending ? (
                <ReviewDecisionCard
                  eid={eid}
                  ranked={ranked}
                  disabled={decisionsDisabled(overview)}
                  deferred={deferred}
                  deferring={setDeferral.isPending}
                  onDefer={() =>
                    setDeferral.mutate(
                      { target_type: 'review_task', target_id: taskId, deferred: true },
                      {
                        onSuccess: () => {
                          toast.success(t.toasts.deferred)
                          void navigate({ to: '/engagements/$eid/decisions', params: { eid } })
                        },
                        onError: (error) => toast.error(apiMessage(error)),
                      },
                    )
                  }
                />
              ) : awaiting ? (
                <div className="flex flex-col gap-3">
                  {/* 等补充证据 (需求 R369): 不算待处理, 不阻塞流程; 补充证据请求在数据管理页 */}
                  <StatusPill tone="accent" className="self-start">
                    {t.status.awaitingEvidence}
                  </StatusPill>
                  <span className="text-[13px] text-muted-foreground">{t.evidenceRequests.awaitingHint}</span>
                  <Link
                    to="/engagements/$eid/data"
                    params={{ eid }}
                    className={cn(buttonVariants({ variant: 'outline', size: 'lg' }), 'self-start')}
                  >
                    {t.evidenceRequests.openBlock}
                  </Link>
                </div>
              ) : (
                <div className="flex flex-col gap-3">
                  <StatusPill tone="ok" className="self-start">
                    {t.formView.alreadyDecided}
                  </StatusPill>
                  {/* FIN-01 通过前可撤回这条任务最近的决定 (需求 R364); 撤回后任务回到待复核, 本页换成决定卡 */}
                  {ranked.view.withdrawable_record_id ? (
                    <WithdrawDecision
                      eid={eid}
                      recordId={ranked.view.withdrawable_record_id}
                      disabled={decisionsDisabled(overview)}
                    />
                  ) : null}
                </div>
              )}
            </>
          ) : (
            <p className="text-muted-foreground text-sm">{t.decisionsPage.none}</p>
          )}
        </QueryState>
      </div>
    </>
  )
}
