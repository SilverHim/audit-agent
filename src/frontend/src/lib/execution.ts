import type { EngagementOverview, TrailEntry } from '@/client/types.gen'

export const STAGES = ['P0_Acceptance', 'P1_Planning', 'P2_Executing', 'P3_Completion', 'P4_Reporting'] as const
export type WorkbenchState = 'running' | 'human' | 'external' | 'failed' | 'done' | 'idle'

export function workbenchState(overview: EngagementOverview, openEvidence = 0): WorkbenchState {
  if (overview.run_error) return 'failed'
  if (overview.running || overview.activities?.length) return 'running'
  if (overview.engagement.locked) return 'done'
  if (overview.last_failure) return 'failed'
  if (overview.current_gate || overview.pending_tasks > 0 || overview.awaiting_correction) return 'human'
  if (overview.awaiting_data || openEvidence) return 'external'
  return 'idle'
}

export function mergeEvents(previous: TrailEntry[], incoming: TrailEntry[]): TrailEntry[] {
  const byId = new Map(previous.map((entry) => [entry.id, entry]))
  for (const entry of incoming) byId.set(entry.id, entry)
  return [...byId.values()].sort(
    (a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? '') || (a.id ?? '').localeCompare(b.id ?? ''),
  )
}

export type ExecutionRun = {
  id: string
  procedure: string
  status: string
  job?: string
  entries: TrailEntry[]
  at: string
  result?: Record<string, unknown>
  failure?: string
}

export function executionRuns(entries: TrailEntry[]): ExecutionRun[] {
  const runs = new Map<string, ExecutionRun>()
  for (const entry of entries) {
    if (!entry.run_id || entry.kind === 'assistant_action') continue
    let run = runs.get(entry.run_id)
    if (!run) {
      run = {
        id: entry.run_id,
        procedure: entry.procedure_id ?? '',
        status: 'running',
        entries: [],
        at: entry.created_at ?? '',
      }
      runs.set(entry.run_id, run)
    }
    if (entry.kind === 'run' && entry.payload?.action === 'finished') {
      run.status = String(entry.payload.status ?? 'succeeded')
      if (entry.payload.result && typeof entry.payload.result === 'object')
        run.result = entry.payload.result as Record<string, unknown>
      if (entry.payload.failure_reason) run.failure = String(entry.payload.failure_reason)
    }
    if (entry.kind === 'execution') {
      if (entry.payload?.stage === 'data') {
        run.job = String(entry.payload.job)
        run.status = entry.payload.status === 'completed' ? 'succeeded' : String(entry.payload.status)
      }
      const id = entry.payload?.step_id
      const index = run.entries.findIndex((item) => item.payload?.step_id === id)
      if (index < 0) run.entries.push(entry)
      else run.entries[index] = entry
    }
  }
  return [...runs.values()].reverse()
}
