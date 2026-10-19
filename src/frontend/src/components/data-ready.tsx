import { AlertTriangle } from 'lucide-react'
import { type ComponentProps, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { useDataReady, useDataSummary } from '@/lib/data-api'
import { checkDetail } from '@/lib/data-checks'
import { errorText, failedChecks, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

// "数据已齐" 按钮: 最近一次体检有不通过的项时先列出各项并要理由, 否则直接确认;
// 后端报 DATA_CHECKS_FAILED (概览没跟上最新体检) 时同样改为要理由, 概览刷新前先按错误体里的编号列出标题
export function DataReadyButton({
  eid,
  disabled = false,
  size,
}: {
  eid: string
  disabled?: boolean
  size?: ComponentProps<typeof Button>['size']
}) {
  const { t, lang } = useI18n()
  const ready = useDataReady(eid)
  const checks = useDataSummary(eid).data?.checks ?? []
  const failed = checks.filter((check) => check.status === 'failed')
  const [asking, setAsking] = useState(false)
  const [rejected, setRejected] = useState<string[]>([]) // 后端报 DATA_CHECKS_FAILED 时附带的编号
  const [reason, setReason] = useState('')
  const items: { id: string; detail: string }[] = failed.length
    ? failed.map((check) => ({ id: check.id, detail: checkDetail(check, t, lang) }))
    : rejected.map((id) => ({ id, detail: '' }))

  const confirm = (text?: string) =>
    ready.mutate(text, {
      onSuccess: () => {
        setAsking(false)
        setReason('')
      },
      onError: (error) => {
        if (isApiCode(error, 'DATA_CHECKS_FAILED')) {
          setRejected(failedChecks(error))
          setAsking(true)
        } else toast.error(errorText(error, t))
      },
    })

  return (
    <>
      <Button
        type="button"
        size={size}
        disabled={disabled || ready.isPending}
        onClick={() => (failed.length ? setAsking(true) : confirm())}
      >
        {t.data.ready}
      </Button>
      <Dialog open={asking} onOpenChange={setAsking}>
        <DialogContent className="sm:max-w-[520px]">
          <DialogHeader>
            <DialogTitle className="font-semibold text-base">{t.data.readyCheck.title}</DialogTitle>
            <DialogDescription>{t.data.readyCheck.intro}</DialogDescription>
          </DialogHeader>
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {items.map(({ id, detail }) => (
              <li key={id} className="flex gap-2.5 text-[13.5px]">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn-strong" aria-hidden="true" />
                <span className="flex min-w-0 flex-col">
                  <span className="font-medium">{t.data.checkTitles[id] ?? id}</span>
                  {detail ? <span className="text-[#4a5262] text-[13px]">{detail}</span> : null}
                </span>
              </li>
            ))}
          </ul>
          <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
            <label htmlFor="data-ready-reason">{t.data.readyCheck.reason}</label>
            <Textarea
              id="data-ready-reason"
              rows={3}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>
          <div className="flex justify-end gap-2.5">
            <Button type="button" variant="outline" onClick={() => setAsking(false)}>
              {t.dataTable.cancel}
            </Button>
            <Button type="button" disabled={!reason.trim() || ready.isPending} onClick={() => confirm(reason.trim())}>
              {t.data.readyCheck.confirm}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
