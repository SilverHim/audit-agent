import { Link } from '@tanstack/react-router'
import { cn } from 'cn'
import {
  Building2,
  ChevronsUpDown,
  ClipboardCheck,
  Database,
  FileText,
  History,
  Inbox,
  ListChecks,
  MessageCircle,
  ScrollText,
} from 'lucide-react'
import type { ReactNode } from 'react'
import type { EngagementOverview, Phase } from '@/client/types.gen'
import { BrandMark, CurrentUser, LanguageToggle } from '@/components/chrome'
import { useDataSummary } from '@/lib/data-api'
import { useI18n } from '@/lib/i18n'
import { formatAudShort } from '@/lib/money'

const STAGES = ['P0_Acceptance', 'P1_Planning', 'P2_Executing', 'P3_Completion', 'P4_Reporting'] as const
type Stage = (typeof STAGES)[number]

function stageIndex(phase: Phase): number {
  if (phase === 'Archived') return STAGES.length
  if (phase === 'Declined') return 0
  return STAGES.indexOf(phase as Stage)
}

function StageDot({ state }: { state: 'done' | 'current' | 'todo' }) {
  if (state === 'done') {
    return (
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
        <circle cx="9" cy="9" r="8" fill="#12A27A" />
        <path
          d="M5.5 9.3l2.3 2.3 4.7-5"
          fill="none"
          stroke="#FFFFFF"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    )
  }
  if (state === 'current') {
    return (
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
        <circle cx="9" cy="9" r="8" fill="none" stroke="#6B83FF" strokeWidth="2" />
        <circle cx="9" cy="9" r="3.5" fill="#6B83FF" />
      </svg>
    )
  }
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <circle cx="9" cy="9" r="7.5" fill="none" stroke="#4A5366" strokeWidth="1.5" />
    </svg>
  )
}

function StageList({ overview }: { overview: EngagementOverview | undefined }) {
  const { t } = useI18n()
  const phase = overview?.engagement.phase ?? 'P0_Acceptance'
  const current = stageIndex(phase)
  const declined = phase === 'Declined'

  const subline = (stage: Stage, index: number): string => {
    if (declined && index === 0) return t.stageStatus.declined
    if (index < current) {
      if (stage === 'P0_Acceptance') return t.stageStatus.accepted
      if (stage === 'P1_Planning' && overview?.materiality_cents != null)
        return t.stageStatus.materiality(formatAudShort(overview.materiality_cents))
      if (stage === 'P4_Reporting') return t.stageStatus.signed
      return t.stageStatus.completed
    }
    if (index === current && overview) {
      if (overview.running) return t.stageStatus.running
      if ((overview as { awaiting_data?: boolean }).awaiting_data) return t.data.awaiting
      if (overview.current_gate) return t.stageStatus.gate(t.gates[overview.current_gate] ?? overview.current_gate)
      if (overview.pending_tasks > 0) return t.stageStatus.reviews(overview.pending_tasks)
    }
    return t.phaseHints[stage]
  }

  return (
    <ol className="flex flex-col px-2">
      {STAGES.map((stage, index) => {
        const state = declined
          ? index === 0
            ? 'current'
            : 'todo'
          : index < current
            ? 'done'
            : index === current
              ? 'current'
              : 'todo'
        return (
          <li key={stage} className="flex gap-3">
            <div className="flex flex-col items-center">
              <StageDot state={state} />
              {index < STAGES.length - 1 ? (
                <span
                  className={cn('min-h-[22px] w-0.5 flex-1', state === 'done' ? 'bg-[#12A27A]' : 'bg-sidebar-border')}
                />
              ) : null}
            </div>
            <div className={cn(index < STAGES.length - 1 && 'pb-3.5')}>
              <div className={cn('text-[13.5px]', state === 'current' ? 'font-semibold text-white' : 'text-[#d4d9e2]')}>
                {t.phases[stage]}
              </div>
              <div className={cn('text-[12.5px]', state === 'current' ? 'text-[#c7ceff]' : 'text-sidebar-muted')}>
                {subline(stage, index)}
              </div>
            </div>
          </li>
        )
      })}
    </ol>
  )
}

type NavItem = {
  to:
    | '/engagements/$eid'
    | '/engagements/$eid/decisions'
    | '/engagements/$eid/reviews'
    | '/engagements/$eid/procedures'
    | '/engagements/$eid/client'
    | '/engagements/$eid/data'
    | '/engagements/$eid/evidence'
    | '/engagements/$eid/report'
    | '/engagements/$eid/trail'
  label: string
  icon: ReactNode
  count?: number
  urgent?: boolean
  exact?: boolean
}

function NavLink({ eid, item }: { eid: string; item: NavItem }) {
  return (
    <Link
      to={item.to}
      params={{ eid }}
      activeOptions={{ exact: Boolean(item.exact) }}
      className="flex min-h-10 items-center gap-2.5 rounded-lg px-2.5 py-2 text-sidebar-foreground hover:bg-sidebar-accent/60 hover:text-white"
      activeProps={{ className: 'bg-sidebar-accent text-white', 'aria-current': 'page' }}
    >
      {item.icon}
      <span className="flex-1">{item.label}</span>
      {item.count != null ? (
        <span
          className={cn(
            'font-mono text-xs',
            item.urgent && item.count > 0
              ? 'rounded-full bg-warn-strong px-[7px] py-px text-white'
              : 'text-sidebar-muted',
          )}
        >
          {item.count}
        </span>
      ) : null}
    </Link>
  )
}

export function EngagementShell({
  eid,
  overview,
  children,
}: {
  eid: string
  overview: EngagementOverview | undefined
  children: ReactNode
}) {
  const { t } = useI18n()
  const dataSummary = useDataSummary(eid).data
  const icon = 'size-[17px] shrink-0'
  const pendingDecisions = (overview?.current_gate ? 1 : 0) + (overview?.pending_tasks ?? 0)
  const items: NavItem[] = [
    {
      to: '/engagements/$eid',
      label: t.nav.concierge,
      icon: <MessageCircle className={icon} aria-hidden="true" />,
      exact: true,
    },
    {
      to: '/engagements/$eid/decisions',
      label: t.nav.decisions,
      icon: <ClipboardCheck className={icon} aria-hidden="true" />,
      count: pendingDecisions,
      urgent: true,
    },
    {
      to: '/engagements/$eid/reviews',
      label: t.nav.reviews,
      icon: <Inbox className={icon} aria-hidden="true" />,
      count: overview?.pending_tasks ?? 0,
      urgent: true,
    },
    {
      to: '/engagements/$eid/procedures',
      label: t.nav.procedures,
      icon: <ListChecks className={icon} aria-hidden="true" />,
      count: overview?.procedures.length,
    },
    { to: '/engagements/$eid/client', label: t.nav.client, icon: <Building2 className={icon} aria-hidden="true" /> },
    {
      to: '/engagements/$eid/data',
      label: t.nav.data,
      icon: <Database className={icon} aria-hidden="true" />,
      count: dataSummary?.open_requests ? dataSummary.open_requests : undefined,
      urgent: true,
    },
    { to: '/engagements/$eid/evidence', label: t.nav.evidence, icon: <FileText className={icon} aria-hidden="true" /> },
    { to: '/engagements/$eid/report', label: t.nav.report, icon: <ScrollText className={icon} aria-hidden="true" /> },
    { to: '/engagements/$eid/trail', label: t.nav.trail, icon: <History className={icon} aria-hidden="true" /> },
  ]
  const fy = overview ? `FY${overview.engagement.period_end.slice(0, 4)}` : ''

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <nav
        aria-label={t.nav.workspace}
        className="flex shrink-0 flex-col gap-5 bg-sidebar px-3.5 py-5 text-sidebar-foreground text-sm lg:sticky lg:top-0 lg:h-screen lg:w-[264px] lg:overflow-y-auto"
      >
        <Link to="/engagements" className="flex items-center gap-2.5 px-1.5">
          <BrandMark />
          <span className="font-semibold text-[15px] text-white tracking-tight">{t.app.name}</span>
        </Link>

        <Link
          to="/engagements"
          title={t.nav.allEngagements}
          className="flex min-h-11 items-center justify-between gap-2 rounded-[10px] border border-sidebar-border bg-sidebar-raised px-3 py-2.5 text-white"
        >
          <span className="flex min-w-0 flex-col">
            <span className="truncate font-semibold text-[13px]">{overview?.client_name ?? '…'}</span>
            <span className="font-mono text-[#9aa3b2] text-[11.5px]">
              {eid}
              {fy ? ` · ${fy}` : ''}
            </span>
          </span>
          <ChevronsUpDown className="size-4 shrink-0 text-[#9aa3b2]" aria-hidden="true" />
        </Link>

        <div className="flex flex-col gap-2">
          <div className="px-2 font-semibold text-sidebar-muted text-xs tracking-wide">{t.nav.stages}</div>
          <StageList overview={overview} />
        </div>

        <div className="flex flex-col gap-0.5">
          <div className="px-2 pb-2 font-semibold text-sidebar-muted text-xs tracking-wide">{t.nav.workspace}</div>
          {items.map((item) => (
            <NavLink key={item.to} eid={eid} item={item} />
          ))}
        </div>

        <div className="flex-1" />
        <div className="px-1.5">
          <LanguageToggle stretch />
        </div>
        <div className="border-sidebar-accent border-t px-1.5 pt-2.5">
          <CurrentUser withRole />
        </div>
      </nav>
      <div className="flex min-w-0 flex-1 flex-col">{children}</div>
    </div>
  )
}
