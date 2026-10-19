// 工作台当前事项 (lib/workspace) 的测试, bun test 运行. 数据是合成的: 总览只填用到的字段.
import { describe, expect, test } from 'bun:test'
import type { EngagementOverview } from '../src/client/types.gen'
import { pickCurrent, type WorkspaceQueries, workspaceReady } from '../src/lib/workspace'

const settled = { isPending: false }
const pending = { isPending: true }

function queries(patch: Partial<WorkspaceQueries> = {}): WorkspaceQueries {
  return {
    gate: pending, // 不在闸门时闸门材料的查询不启用, 一直是 pending
    tasks: settled,
    exceptions: settled,
    deferrals: settled,
    requests: settled,
    summary: settled,
    ...patch,
  }
}

const overview = (gate: string | null) => ({ current_gate: gate }) as unknown as EngagementOverview

describe('workspaceReady', () => {
  test('总览没到不就绪', () => {
    expect(workspaceReady(undefined, queries())).toBe(false)
  })

  test('不在闸门时不等闸门材料, 其余都返回即就绪', () => {
    expect(workspaceReady(overview(null), queries())).toBe(true)
  })

  test.each(['tasks', 'exceptions', 'deferrals', 'requests', 'summary'] as const)(
    '%s 还没返回不就绪 (如补交资料晚到, 不先显示复核任务)',
    (name) => {
      expect(workspaceReady(overview(null), queries({ [name]: pending }))).toBe(false)
    },
  )

  test('停在闸门时等闸门材料返回 (出错也算返回)', () => {
    expect(workspaceReady(overview('ACC-01'), queries())).toBe(false)
    expect(workspaceReady(overview('ACC-01'), queries({ gate: settled }))).toBe(true)
  })
})

describe('pickCurrent', () => {
  const items = [
    { kind: 'requests', key: 'requests', deferred: true },
    { kind: 'task', key: 'task:T-1', deferred: false },
    { kind: 'task', key: 'task:T-2', deferred: false },
  ]

  test('没指名时取第一项没标稍后处理的', () => {
    expect(pickCurrent(items, undefined)?.key).toBe('task:T-1')
  })

  test('focus 指名的一项优先, 标了稍后处理也取', () => {
    expect(pickCurrent(items, 'task:T-2')?.key).toBe('task:T-2')
    expect(pickCurrent(items, 'requests')?.key).toBe('requests')
  })

  test('focus 为任一补交请求时指补交资料一项', () => {
    expect(pickCurrent(items, 'request:REQ-approval_log')?.key).toBe('requests')
  })

  test('都标了稍后处理或没有事项为 null', () => {
    expect(pickCurrent([{ kind: 'task', key: 'task:T-1', deferred: true }], undefined)).toBeNull()
    expect(pickCurrent([], undefined)).toBeNull()
  })
})
