import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import {
  correctionsListAdjustmentEntriesOptions,
  correctionsSubmitCorrectionNoticeMutation,
  reportsGetMisstatementsOptions,
} from '@/client/@tanstack/react-query.gen'
import type { CorrectionNoticeIn } from '@/client/types.gen'
import { Code, SectionLabel } from '@/components/chrome'
import { DecisionFrame, OptionCard, ReasonField } from '@/components/concierge'
import { QueryState } from '@/components/query-state'
import { Button } from '@/components/ui/button'
import { invalidateEngagement } from '@/lib/engagement'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { correctionNoticeBody } from '@/lib/misstatements'
import { useMoney } from '@/lib/use-money'

type Kind = CorrectionNoticeIn['kind']
const KINDS: Kind[] = ['adjustments_posted', 'stop_waiting']

function toggle(list: string[], value: string, on: boolean): string[] {
  return on ? [...list.filter((item) => item !== value), value] : list.filter((item) => item !== value)
}

// 等待客户更正 (总览 awaiting_correction, 需求 R163): 列待更正的错报, 两个操作 "客户已过账调整分录" (勾选错报与更正
// 分录; 已载入的照样可勾, 只作标注) 与 "停止等待" (理由都选填, 需求 R360); 提交更正通知后刷新. 不在等更正 (NOT_AWAITING_CORRECTION)
// 按文案提示. 待更正的错报变了由调用方换 key 重新挂载, 勾选与理由不带进下一份通知
export function CorrectionCard({ eid, ids, disabled }: { eid: string; ids: string[]; disabled: boolean }) {
  const { formatMoney } = useMoney()
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const report = useQuery(reportsGetMisstatementsOptions({ path: { eid } }))
  const entries = useQuery(correctionsListAdjustmentEntriesOptions({ path: { eid } }))
  const [kind, setKind] = useState<Kind | null>(null)
  const [picked, setPicked] = useState<string[]>([])
  const [entryNos, setEntryNos] = useState<string[]>([])
  const [reason, setReason] = useState('')
  const submit = useMutation({
    ...correctionsSubmitCorrectionNoticeMutation(),
    onSuccess: async () => {
      toast.success(t.correction.submitted)
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(errorText(error, t)),
  })
  const byId = new Map((report.data?.items ?? []).map((item) => [item.id, item]))
  const busy = disabled || submit.isPending

  const send = () => {
    if (!kind) return
    const body = correctionNoticeBody({ kind, misstatementIds: picked, entryNos, reason }, t)
    if (typeof body === 'string') return toast.error(body)
    submit.mutate({ path: { eid }, body })
  }

  return (
    <DecisionFrame
      eid={eid}
      title={t.correction.title}
      footer={
        <>
          <span />
          <Button type="button" size="lg" disabled={busy || !kind} onClick={send}>
            {t.correction.submit}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-1.5">
        <SectionLabel>{t.correction.misstatements}</SectionLabel>
        <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[14px]">
          {ids.map((id) => {
            const item = byId.get(id)
            return (
              <li key={id} className="flex flex-wrap items-center gap-2">
                <input
                  id={`correction-ms-${id}`}
                  type="checkbox"
                  className="size-4 accent-primary"
                  checked={picked.includes(id)}
                  disabled={busy}
                  onChange={(event) => setPicked((list) => toggle(list, id, event.target.checked))}
                />
                <label htmlFor={`correction-ms-${id}`} className="flex flex-wrap gap-2">
                  <Code className="font-semibold">{id}</Code>
                  {item ? (
                    <span>
                      {item.account} · {t.directions[item.direction]} · <Code>{formatMoney(item.amount_cents)}</Code>
                    </span>
                  ) : null}
                </label>
              </li>
            )
          })}
        </ul>
      </div>
      <fieldset className="m-0 flex flex-wrap gap-2.5 border-0 p-0">
        <legend className="sr-only">{t.card.decision}</legend>
        {KINDS.map((item) => (
          <OptionCard
            key={item}
            name="correction-kind"
            value={item}
            checked={kind === item}
            onSelect={() => setKind(item)}
            label={item === 'adjustments_posted' ? t.correction.posted : t.correction.stop}
            hint={item === 'adjustments_posted' ? t.correction.postedHint : t.correction.stopHint}
            disabled={busy}
          />
        ))}
      </fieldset>
      {kind === 'adjustments_posted' ? (
        <div className="flex flex-col gap-1.5">
          <SectionLabel>{t.correction.entries}</SectionLabel>
          <QueryState isPending={entries.isPending} error={entries.error}>
            {entries.data?.length ? (
              <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[14px]">
                {entries.data.map((entry) => (
                  <li key={entry.entry_no} className="flex items-start gap-2">
                    <input
                      id={`correction-entry-${entry.entry_no}`}
                      type="checkbox"
                      className="mt-0.5 size-4 shrink-0 accent-primary"
                      checked={entryNos.includes(entry.entry_no)}
                      disabled={busy}
                      onChange={(event) => setEntryNos((list) => toggle(list, entry.entry_no, event.target.checked))}
                    />
                    <label htmlFor={`correction-entry-${entry.entry_no}`} className="flex min-w-0 flex-col gap-0.5">
                      <span className="flex flex-wrap gap-2">
                        <Code className="font-semibold">{entry.entry_no}</Code>
                        <span>
                          {entry.posting_date} · {t.correction.lines(entry.line_count)}
                          {entry.loaded ? ` · ${t.correction.loaded}` : ''}
                        </span>
                      </span>
                      {entry.description ? (
                        <span lang="en" className="text-[13px] text-muted-foreground">
                          {entry.description}
                        </span>
                      ) : null}
                    </label>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-[13px] text-muted-foreground">{t.correction.noEntries}</span>
            )}
          </QueryState>
        </div>
      ) : null}
      <ReasonField id="correction-reason" value={reason} onChange={setReason} />
    </DecisionFrame>
  )
}
