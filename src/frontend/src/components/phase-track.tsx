import type { Phase, ProcedureStatus } from '@/client/types.gen'
import { Badge } from '@/components/ui/badge'

const PHASES = ['P0_Acceptance', 'P1_Planning', 'P2_Executing', 'P3_Completion', 'P4_Reporting'] as const

const PHASE_LABEL: Record<Phase, string> = {
  P0_Acceptance: 'P0 Acceptance',
  P1_Planning: 'P1 Planning',
  P2_Executing: 'P2 Execution',
  P3_Completion: 'P3 Completion',
  P4_Reporting: 'P4 Reporting',
  Archived: 'Archived',
  Declined: 'Declined',
}

export function phaseLabel(phase: Phase): string {
  return PHASE_LABEL[phase]
}

export function PhaseTrack({ phase }: { phase: Phase }) {
  const active = phase === 'Archived' ? PHASES.length : PHASES.indexOf(phase as (typeof PHASES)[number])
  return (
    <ol className="grid gap-2 sm:grid-cols-5">
      {PHASES.map((item, index) => {
        const state =
          phase === 'Declined'
            ? 'muted'
            : index < active || phase === 'Archived'
              ? 'done'
              : index === active
                ? 'current'
                : 'muted'
        return (
          <li
            key={item}
            className={
              state === 'current'
                ? 'rounded-md border border-foreground px-2 py-1 text-sm font-medium'
                : state === 'done'
                  ? 'rounded-md bg-muted px-2 py-1 text-sm'
                  : 'rounded-md px-2 py-1 text-muted-foreground text-sm'
            }
          >
            {PHASE_LABEL[item]}
          </li>
        )
      })}
    </ol>
  )
}

export function StatusBadge({ status }: { status: ProcedureStatus | string }) {
  return <Badge variant={status === 'completed' ? 'secondary' : 'outline'}>{status}</Badge>
}
