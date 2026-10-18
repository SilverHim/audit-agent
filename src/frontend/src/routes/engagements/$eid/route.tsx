import { useMutation, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Outlet, redirect } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { engagementsResumeEngagementMutation } from '@/client/@tanstack/react-query.gen'
import { EngagementShell } from '@/components/engagement-shell'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { getToken } from '@/lib/auth'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid')({
  beforeLoad: () => {
    if (!getToken()) throw redirect({ to: '/login' })
  },
  component: EngagementLayout,
})

function EngagementLayout() {
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const overview = useEngagement(eid)
  const wasRunning = useRef(false)
  const resume = useMutation({
    ...engagementsResumeEngagementMutation(),
    onSuccess: async () => {
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })

  // 后台跑完一段 (running 由真变假) 时刷新本项目的全部查询, 新的待处理事项随之出现
  useEffect(() => {
    if (!overview.data) return
    if (wasRunning.current && !overview.data.running) void invalidateEngagement(queryClient, eid)
    wasRunning.current = overview.data.running
  }, [eid, overview.data, queryClient])

  const data = overview.data
  // 功能集与项目冻结的不同 (FEATURES_MISMATCH): 重试也不会推进, 不给继续执行按钮
  const mismatch = data?.run_error?.startsWith('FEATURES_MISMATCH') ?? false
  return (
    <EngagementShell eid={eid} overview={data}>
      {data?.run_error ? (
        <div className="px-4 pt-4 sm:px-7">
          <Alert variant="destructive">
            <AlertTitle>{mismatch ? t.app.featuresMismatch : t.app.runError}</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center gap-3">
              <span className="font-mono text-xs">{data.run_error}</span>
              {mismatch ? null : (
                <Button
                  type="button"
                  variant="outline"
                  disabled={resume.isPending}
                  onClick={() => resume.mutate({ path: { eid } })}
                >
                  {t.app.resume}
                </Button>
              )}
            </AlertDescription>
          </Alert>
        </div>
      ) : null}
      <Outlet />
    </EngagementShell>
  )
}
