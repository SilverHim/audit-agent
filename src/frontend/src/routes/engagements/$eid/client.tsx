import { createFileRoute } from '@tanstack/react-router'
import { Pencil } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import { BusyNote } from '@/components/activity-bar'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { EvidenceLink } from '@/components/evidence-link'
import { ProfileForm, useGate } from '@/components/gate-form'
import { CheckIcon, checkTone } from '@/components/profile-fields'
import { Button } from '@/components/ui/button'
import { formatAbn } from '@/lib/abn'
import { decisionsDisabled } from '@/lib/engagement'
import { useI18n } from '@/lib/i18n'
import { type ClientProfileView, useClientProfile } from '@/lib/new-api'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/client')({
  component: Client,
})

function Client() {
  const { eid } = Route.useParams()
  const { t, formatDateTime } = useI18n()
  const overview = useEngagement(eid).data
  const remote = useClientProfile(eid)
  const atAcceptance = overview?.current_gate === 'ACC-01'
  const gate = useGate(eid, atAcceptance && remote.data === null)
  const [editing, setEditing] = useState(false)

  // 客户资料接口 (R2) 上线前, 停在 ACC-01 时用闸门材料顶上
  const fallback: ClientProfileView | null =
    gate.data?.material.kind === 'ACC-01'
      ? {
          profile: gate.data.material.client_profile,
          history: [gate.data.material.client_profile],
          versions: [],
          scorecard: gate.data.material.scorecard,
          exceptions: gate.data.material.exceptions,
          editable: true,
        }
      : null
  const view = remote.data ?? fallback
  const editable = Boolean(view?.editable) && !decisionsDisabled(overview)

  return (
    <>
      <PageHeader
        title={t.clientPage.title}
        subtitle={view?.profile.legal_name ?? overview?.client_name}
        actions={
          editable && !editing ? (
            <Button type="button" variant="outline" size="lg" onClick={() => setEditing(true)}>
              <Pencil className="size-4" aria-hidden="true" />
              {t.clientPage.edit}
            </Button>
          ) : view?.editable && !editing ? (
            <BusyNote eid={eid} className="max-w-[360px] text-right" />
          ) : null
        }
      />
      <div className="flex flex-wrap items-start gap-5 px-4 py-6 sm:px-7">
        {!view ? (
          <p className="text-muted-foreground text-sm">
            {remote.isPending || gate.isLoading ? t.app.loading : t.clientPage.empty}
          </p>
        ) : editing ? (
          <div className="min-w-0 flex-[2_1_460px]">
            <ProfileForm eid={eid} profile={view.profile} disabled={!editable} onDone={() => setEditing(false)} />
          </div>
        ) : (
          <>
            <section className="flex min-w-0 flex-[2_1_460px] flex-col gap-4 rounded-[14px] border bg-card px-6 py-5">
              <div className="flex items-baseline justify-between gap-2">
                <h2 className="m-0 font-semibold text-base">{t.clientPage.details}</h2>
                <span className="text-[12.5px] text-muted-foreground">
                  {t.clientPage.version(view.profile.version ?? 1)}
                </span>
              </div>
              <dl className="m-0 grid grid-cols-[140px_minmax(0,1fr)] gap-x-4 gap-y-3.5 text-[14px]">
                <Row label={t.clientPage.legalName}>{view.profile.legal_name}</Row>
                <Row label={t.clientPage.abn}>
                  <Code>{formatAbn(view.profile.abn)}</Code>
                </Row>
                <Row label={t.clientPage.entityType}>
                  {t.clientPage.entityTypes[view.profile.entity_type] ?? view.profile.entity_type}
                </Row>
                <Row label={t.clientPage.industry}>
                  <Code>{view.profile.industry_anzsic}</Code>
                </Row>
                <Row label={t.clientPage.listed}>{view.profile.listed ? t.clientPage.yes : t.clientPage.no}</Row>
                <Row label={t.clientPage.directors}>
                  <span className="flex flex-col">
                    {view.profile.directors.map((name) => (
                      <span key={name}>{name}</span>
                    ))}
                  </span>
                </Row>
                <Row label={t.clientPage.notes}>
                  <span className="text-secondary-foreground" lang="en">
                    {view.profile.notes || '—'}
                  </span>
                </Row>
              </dl>
              <p className="m-0 border-line-soft border-t pt-3.5 text-[13px] text-[#4a5262]">
                {editable ? t.clientPage.rerunNote : t.clientPage.lockedNote}
              </p>
            </section>

            <div className="flex min-w-0 flex-[1_1_320px] flex-col gap-5">
              {view.scorecard ? (
                <section className="flex flex-col gap-3.5 rounded-[14px] border bg-card px-5 py-5">
                  <div className="flex items-baseline justify-between gap-2">
                    <h2 className="m-0 font-semibold text-base">{t.clientPage.checks}</h2>
                    <Code className="text-[12px] text-muted-foreground">ACC-04</Code>
                  </div>
                  {view.scorecard.registry_checks.map((check) => (
                    <article key={check.check} className="flex flex-col gap-1.5 rounded-[10px] border p-3.5">
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-2 font-semibold">
                          <CheckIcon passed={check.passed} className="size-4" />
                          {t.checks[check.check]}
                        </span>
                        <StatusPill tone={checkTone(check.passed)}>{t.card.registers[check.check]}</StatusPill>
                      </div>
                      {check.passed === null ? (
                        <span className="font-medium text-[12.5px] text-warn">{t.card.manualCheck}</span>
                      ) : null}
                      <span className="text-[13px] text-[#4a5262]" lang="en">
                        {check.detail}
                      </span>
                      <span>
                        <EvidenceLink
                          eid={eid}
                          pointer={check.evidence}
                          label={
                            check.evidence.source_type === 'document'
                              ? t.clientPage.viewOriginal
                              : t.clientPage.viewTrail
                          }
                        />
                      </span>
                    </article>
                  ))}
                </section>
              ) : null}
              <section className="flex flex-col gap-3 rounded-[14px] border bg-card px-5 py-5">
                <h2 className="m-0 font-semibold text-base">{t.clientPage.history}</h2>
                {[...view.history].reverse().map((item) => {
                  const info = view.versions.find((entry) => entry.version === (item.version ?? 1))
                  return (
                    <div key={item.version ?? 1} className="flex flex-col gap-0.5 text-[13.5px]">
                      <div className="flex items-baseline justify-between gap-2.5">
                        <span className="font-semibold">{t.clientPage.version(item.version ?? 1)}</span>
                        {item.version === view.profile.version ? (
                          <StatusPill tone="accent">{t.clientPage.current}</StatusPill>
                        ) : null}
                      </div>
                      {info ? (
                        <span className="text-[12.5px] text-muted-foreground">
                          {info.source ? t.clientPage.sources[info.source] : t.clientPage.notRecorded}
                          {' · '}
                          {info.updated_by_name ?? info.updated_by ?? t.clientPage.notRecorded}
                          {' · '}
                          {formatDateTime(info.updated_at)}
                        </span>
                      ) : null}
                    </div>
                  )
                })}
              </section>
            </div>
          </>
        )}
      </div>
    </>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="m-0">{children}</dd>
    </>
  )
}
