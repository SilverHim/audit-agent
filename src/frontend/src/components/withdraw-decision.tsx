import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { reviewsWithdrawReviewRecordMutation } from '@/client/@tanstack/react-query.gen'
import { ReasonField } from '@/components/concierge'
import { Button } from '@/components/ui/button'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

// 撤回复核决定 (需求 R364, 后端 R363): FIN-01 通过前, 已决定任务上最近的那条决定 (ReviewTaskView.withdrawable_record_id)
// 可以撤回, 理由选填. 撤回后任务与判断回到待复核, 认定的错报作废并重新累计; 原决定与撤回都留痕. onDone 在撤回成功后调
export function WithdrawDecision({
  eid,
  recordId,
  label,
  disabled,
  onDone,
}: {
  eid: string
  recordId: string
  label?: string
  disabled?: boolean
  onDone?: () => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const withdraw = useMutation({
    ...reviewsWithdrawReviewRecordMutation(),
    onSuccess: async () => {
      toast.success(t.withdraw.done)
      setOpen(false)
      await invalidateEngagement(queryClient, eid)
      onDone?.()
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  if (!open) {
    return (
      <Button type="button" variant="outline" disabled={disabled} onClick={() => setOpen(true)}>
        {label ?? t.withdraw.action}
      </Button>
    )
  }
  return (
    <div className="flex flex-col gap-2.5 rounded-[10px] border p-3.5" data-testid={`withdraw-${recordId}`}>
      <span className="text-[13px] text-secondary-foreground">{t.withdraw.intro}</span>
      <ReasonField id={`withdraw-reason-${recordId}`} value={reason} onChange={setReason} />
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={() => setOpen(false)}>
          {t.withdraw.cancel}
        </Button>
        <Button
          type="button"
          disabled={disabled || withdraw.isPending}
          onClick={() =>
            withdraw.mutate({ path: { eid, record_id: recordId }, body: { reason: reason.trim() || null } })
          }
        >
          {t.withdraw.confirm}
        </Button>
      </div>
    </div>
  )
}
