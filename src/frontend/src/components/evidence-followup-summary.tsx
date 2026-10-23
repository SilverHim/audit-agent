import type { EvidenceRequest, ScopeLimitation } from '@/client/types.gen'
import { useI18n } from '@/lib/i18n'
import { useMoney } from '@/lib/use-money'

export function EvidenceFollowupSummary({
  requests,
  limitations,
}: {
  requests: EvidenceRequest[]
  limitations: ScopeLimitation[]
}) {
  const { t } = useI18n()
  const { formatMoney } = useMoney()
  if (!requests.length && !limitations.length) return null
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-warn p-4" data-testid="evidence-followup-summary">
      <h3 className="m-0 font-semibold">{t.evidenceRequests.decisionNotice}</h3>
      {requests.map((r) => (
        <div key={r.id} className="flex flex-col gap-1 text-sm">
          <strong>
            {r.subject_id} · {t.evidenceRequests.statuses[r.status ?? 'open']}
          </strong>
          <span>{r.description}</span>
          <span>
            {t.evidenceRequests.sources[r.requested_from ?? 'client']} · {r.id}
          </span>
        </div>
      ))}
      {limitations.length ? <h4 className="m-0 font-semibold">{t.evidenceRequests.scopeTitle}</h4> : null}
      {limitations.map((l) => (
        <div key={l.id} className="flex flex-wrap gap-2 text-sm">
          <strong>{l.subject_id}</strong>
          <span>{l.amount_cents == null ? '—' : formatMoney(l.amount_cents)}</span>
          <span>{l.assertion ? t.assertions[l.assertion] : '—'}</span>
          <span>{l.reason || t.reason.none}</span>
        </div>
      ))}
    </section>
  )
}
