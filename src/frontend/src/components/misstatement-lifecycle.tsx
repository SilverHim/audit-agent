import type { CorrectionNotice, Misstatement, MisstatementStatus } from '@/client/types.gen'
import { Code, StatusPill, type Tone } from '@/components/chrome'
import { CorrectionList } from '@/components/verdict'
import { useActorName } from '@/lib/actor'
import { useI18n } from '@/lib/i18n'
import { remainingCents, separateCode, verifiedEntries } from '@/lib/misstatements'
import { reasonText } from '@/lib/reason'
import { useMoney } from '@/lib/use-money'
import { correctionEntries } from '@/lib/verdict'

// 错报的生命周期 (需求 R163): 错报页与 FIN-01 闸门材料共用. 每条显示状态, 类型, 科目代码, 管理层回复原文, 已更正额与
// 剩余额, 已验证的更正凭证号; 有舞弊迹象的加醒目标记与理由; 未更正的写依据 (拒绝或停止等待)

const STATUS_TONES: Partial<Record<MisstatementStatus, Tone>> = {
  identified: 'neutral',
  communicated: 'warn',
  verifying: 'accent',
  corrected: 'ok',
  partially_corrected: 'warn',
  uncorrected: 'risk',
}

function MisstatementItem({ item }: { item: Misstatement }) {
  const { formatMoney } = useMoney()
  const { t, formatDateTime } = useI18n()
  const labels = t.lifecycle
  const status = item.status ?? 'identified'
  const verified = verifiedEntries(item)
  const code = separateCode(item)
  // AI 建议的更正分录 (需求 R367, R368): 随错报显示, 每行科目代码与名称, 借方或贷方金额, 摘要
  const corrections = correctionEntries(item)
  return (
    <li
      className="flex flex-col gap-2 rounded-lg border px-3.5 py-2.5 text-[14px]"
      data-testid={`misstatement-${item.id}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Code className="font-semibold">{item.id}</Code>
        <StatusPill tone={STATUS_TONES[status] ?? 'neutral'}>{t.misstatementStatuses[status] ?? status}</StatusPill>
        {item.fraud_indicative ? <StatusPill tone="risk">{labels.fraud}</StatusPill> : null}
        <span>
          {item.account}
          {code ? <Code className="ml-1.5">{code}</Code> : null} · {t.assertions[item.assertion]} ·{' '}
          {t.directions[item.direction]} · {t.misstatementTypes[item.type]}
        </span>
        <span className="flex-1" />
        <Code>{formatMoney(item.amount_cents)}</Code>
      </div>
      <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-[13px]">
        {item.fraud_indicative ? (
          <>
            <dt className="font-semibold text-risk">{labels.fraud}</dt>
            <dd className="m-0 text-risk">{reasonText(item.fraud_reason, t)}</dd>
          </>
        ) : null}
        {item.management_response ? (
          <>
            <dt className="text-muted-foreground">{labels.response}</dt>
            <dd className="m-0">
              <span lang="en">“{item.management_response}”</span>
              {item.responded_at ? (
                <span className="text-muted-foreground"> · {labels.responded(formatDateTime(item.responded_at))}</span>
              ) : null}
            </dd>
          </>
        ) : null}
        <dt className="text-muted-foreground">{labels.corrected}</dt>
        <dd className="m-0 font-mono">
          {formatMoney(item.corrected_cents ?? 0)} · {labels.remaining} {formatMoney(remainingCents(item))}
        </dd>
        {verified.length ? (
          <>
            <dt className="text-muted-foreground">{labels.verifiedEntries}</dt>
            <dd className="m-0 font-mono">{verified.join(', ')}</dd>
          </>
        ) : null}
        {status === 'uncorrected' && item.uncorrected_basis ? (
          <>
            <dt className="text-muted-foreground">{labels.uncorrectedBasis}</dt>
            <dd className="m-0">{labels.basis[item.uncorrected_basis]}</dd>
          </>
        ) : null}
        {item.needs_recommunication ? (
          <>
            <dt className="text-muted-foreground">{labels.status}</dt>
            <dd className="m-0">{labels.recommunicate}</dd>
          </>
        ) : null}
      </dl>
      {corrections.length ? <CorrectionList entries={corrections} className="border-t pt-2" /> : null}
    </li>
  )
}

export function MisstatementList({ items }: { items: Misstatement[] }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
      {items.map((item) => (
        <MisstatementItem key={item.id} item={item} />
      ))}
    </ul>
  )
}

// 全部更正通知: 种类, 所列错报, 凭证号, 理由, 提交人与时刻
export function CorrectionNotices({ notices }: { notices: CorrectionNotice[] }) {
  const { t, formatDateTime } = useI18n()
  const actorName = useActorName()
  if (!notices.length) return null
  return (
    <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[13.5px]">
      {notices.map((notice) => (
        <li key={notice.id} className="flex flex-col gap-0.5 rounded-lg border px-3.5 py-2">
          <span className="flex flex-wrap gap-2">
            <span className="font-semibold">{t.lifecycle.noticeKinds[notice.kind]}</span>
            <Code>{notice.misstatement_ids.join(', ')}</Code>
            {notice.adjustment_entry_nos?.length ? <Code>{notice.adjustment_entry_nos.join(', ')}</Code> : null}
            <span className="flex-1" />
            <span className="text-muted-foreground">
              {[actorName(notice.decided_by), formatDateTime(notice.decided_at)].filter(Boolean).join(' · ')}
            </span>
          </span>
          <span className="text-secondary-foreground">{reasonText(notice.reason, t)}</span>
        </li>
      ))}
    </ul>
  )
}
