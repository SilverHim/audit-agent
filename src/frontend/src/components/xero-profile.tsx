import { cn } from 'cn'
import { Link2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import type { XeroProfileCheck, XeroProfileCheckIn } from '@/client/types.gen'
import { OptionCard } from '@/components/concierge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useXeroProfileCheck } from '@/lib/data-api'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { profileChoice, profileRows } from '@/lib/xero-profile'

type Choice = XeroProfileCheckIn['decision']

// Xero 组织信息与客户画像不一致 (后端 R291): 两边的法定名称与 ABN 并列 (对不上的标红), 审计师选按 Xero 更新画像或
// 保持原样 (理由必填). 只有停在 ACC-01 时更新由本项目采用并重新做承接核查; 其余时候 (未到 ACC-01 或已过) 只给客户写
// 新版本. 写出的版本 (next_version) 与本项目所处的位置 (stage) 都由后端给, 前端只按它们选文案. 工作台与数据管理页
// 共用; inset 为工作台里与对话对齐的缩进
export function XeroProfileCard({
  eid,
  check,
  disabled,
  inset = false,
  deferred = false,
  onDefer,
  deferring = false,
}: {
  eid: string
  check: XeroProfileCheck
  disabled: boolean
  inset?: boolean
  deferred?: boolean
  onDefer?: () => void
  deferring?: boolean
}) {
  const { t, formatDateTime } = useI18n()
  const submit = useXeroProfileCheck(eid)
  const [choice, setChoice] = useState<Choice | null>(null)
  const [reason, setReason] = useState('')
  const busy = disabled || submit.isPending
  const ready = choice === 'update' || (choice === 'keep' && reason.trim().length > 0)
  const name = `xero-profile-${check.key}`
  const next = check.next_version

  const record = () => {
    if (!choice) return
    const body = profileChoice(check, choice, reason, t)
    if (typeof body === 'string') return toast.error(body)
    submit.mutate(body, {
      onSuccess: () => toast.success(choice === 'keep' ? t.xeroProfile.kept : t.xeroProfile.updated),
      onError: (error) => toast.error(errorText(error, t)),
    })
  }

  return (
    <section
      aria-label={t.xeroProfile.title}
      className={cn(
        'flex flex-col gap-3.5 rounded-[14px] border-[1.5px] border-primary bg-card px-5 py-4.5 shadow-xs',
        inset && 'sm:ml-[42px]',
      )}
      data-testid="xero-profile-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <span className="font-semibold text-[12px] text-accent-foreground tracking-wide">
            {t.xeroProfile.eyebrow}
          </span>
          <h2 className="m-0 font-semibold text-[17px] tracking-tight">{t.xeroProfile.title}</h2>
          <span className="text-[13px] text-muted-foreground">
            {t.xeroProfile.intro(formatDateTime(check.checked_at), check.profile_version)}
          </span>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-0.5 text-[12px] text-secondary-foreground">
          <Link2 className="size-3.5" aria-hidden="true" />
          {check.xero_name}
        </span>
      </div>
      <table className="w-full border-collapse text-[13.5px]">
        <thead>
          <tr className="text-left text-[12px] text-muted-foreground">
            <th className="py-1.5 pr-3 font-normal" />
            <th className="py-1.5 pr-3 font-normal">{t.xeroProfile.xero}</th>
            <th className="py-1.5 font-normal">{t.xeroProfile.profile(check.profile_version)}</th>
          </tr>
        </thead>
        <tbody>
          {profileRows(check, t).map((row) => (
            <tr key={row.field} className="border-line-soft border-t" data-differs={row.differs}>
              <th scope="row" className="py-2 pr-3 text-left font-normal text-muted-foreground">
                {t.xeroProfile.fields[row.field]}
              </th>
              <td className={cn('py-2 pr-3', row.differs && 'font-semibold text-risk')}>{row.xero}</td>
              <td className={cn('py-2', row.differs && 'font-semibold')}>{row.profile}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
        <legend className="sr-only">{t.xeroProfile.title}</legend>
        <OptionCard
          name={name}
          value="update"
          checked={choice === 'update'}
          onSelect={() => setChoice('update')}
          label={t.xeroProfile.update}
          hint={
            check.stage === 'at'
              ? t.xeroProfile.updateHere(next)
              : check.stage === 'before'
                ? t.xeroProfile.updateBefore(next, check.profile_version)
                : t.xeroProfile.updateAfter(next, check.profile_version)
          }
          disabled={busy}
        />
        <OptionCard
          name={name}
          value="keep"
          checked={choice === 'keep'}
          onSelect={() => setChoice('keep')}
          label={t.xeroProfile.keep}
          hint={t.xeroProfile.keepHint}
          disabled={busy}
        />
      </fieldset>
      {choice === 'keep' ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${name}-reason`} className="font-semibold text-[13px]">
            {t.card.reason} <span className="font-normal text-muted-foreground">{t.card.reasonRequired}</span>
          </label>
          <Textarea id={`${name}-reason`} rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2.5">
        {onDefer && !deferred ? (
          <Button type="button" variant="outline" size="lg" disabled={busy || deferring} onClick={onDefer}>
            {t.card.decideLater}
          </Button>
        ) : (
          <span />
        )}
        <Button type="button" size="lg" disabled={busy || !ready} onClick={record}>
          {t.card.record}
        </Button>
      </div>
    </section>
  )
}
