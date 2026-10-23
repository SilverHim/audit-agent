import { describe, expect, test } from 'bun:test'
import type { EngagementOverview, TrailEntry } from '../src/client/types.gen'
import { executionRuns, mergeEvents, workbenchState } from '../src/lib/execution'

function overview(patch: Partial<EngagementOverview> = {}): EngagementOverview {
  return {
    engagement: { locked: false },
    activities: [],
    running: false,
    run_error: null,
    pending_tasks: 0,
    ...patch,
  } as EngagementOverview
}

describe('工作台业务状态', () => {
  test('区分系统运行、人工决定、外部资料与空闲', () => {
    expect(workbenchState(overview({ running: true }))).toBe('running')
    expect(workbenchState(overview({ current_gate: 'RPT-01' }), 4)).toBe('human')
    expect(workbenchState(overview({ pending_tasks: 1 }), 2)).toBe('human')
    expect(workbenchState(overview({ awaiting_data: true }))).toBe('external')
    expect(workbenchState(overview(), 2)).toBe('external')
    expect(workbenchState(overview())).toBe('idle')
    expect(workbenchState(overview({ run_error: 'Failed' }))).toBe('failed')
  })
})

function event(
  id: string,
  run: string,
  payload: Record<string, unknown>,
  kind: TrailEntry['kind'] = 'execution',
): TrailEntry {
  return {
    id,
    kind,
    engagement_id: 'E',
    actor: 'A6',
    summary: 'Synthetic event',
    run_id: run,
    procedure_id: 'REV-02',
    created_at: `2026-10-24T00:00:${id.padStart(2, '0')}Z`,
    payload,
  }
}

test('事件重连去重, 步骤以同一编号更新, 并行执行分别归组', () => {
  const start = event('01', 'R1', { step_id: 'S1', stage: 'model', status: 'running' })
  const other = event('02', 'R2', { step_id: 'S2', stage: 'model', status: 'running' })
  const end = event('03', 'R1', { step_id: 'S1', stage: 'model', status: 'completed' })
  const merged = mergeEvents([start, other], [start, end])
  expect(merged.map((item) => item.id)).toEqual(['01', '02', '03'])
  const runs = executionRuns(merged)
  expect(runs).toHaveLength(2)
  expect(runs.find((run) => run.id === 'R1')?.entries).toEqual([end])
  expect(runs.find((run) => run.id === 'R2')?.entries).toEqual([other])
})

test('程序结束状态与单步结果不同, 数据作业保留完成记录', () => {
  const steps = [
    event('01', 'R1', { step_id: 'S1', status: 'completed' }),
    event('02', 'R1', { action: 'finished', status: 'failed' }, 'run'),
  ]
  expect(executionRuns(steps)[0]?.status).toBe('failed')
  const data = [
    event('03', 'ACT-1', { step_id: 'A1', stage: 'data', job: 'demo_import', status: 'completed', done: 2, total: 2 }),
  ]
  expect(executionRuns(data)[0]?.status).toBe('succeeded')
  expect(executionRuns(data)[0]?.job).toBe('demo_import')
})
