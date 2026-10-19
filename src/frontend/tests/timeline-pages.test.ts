// 工作台时间线按页取留痕 (lib/timeline 的 earlierCursor, trailInOrder 与 buildTimeline 的 complete) 的测试, bun test 运行.
// 后端 GET /trail?newest=true&before= 的取法由后端测试守 (tests/test_fakes.py 的 trail_pages_hold), 这里用同样语义的
// 内存列表代替, 只看前端怎样接页: 首页含最新一条, 往前翻到最早一条不重不漏, 取全之前不加 "项目已创建".
import { describe, expect, test } from 'bun:test'
import type { TrailEntry } from '../src/client/types.gen'
import { buildTimeline, earlierCursor, trailInOrder } from '../src/lib/timeline'

const SIZE = 3

function entry(n: number): TrailEntry {
  const at = `2026-10-19T00:${String(n).padStart(2, '0')}:00Z`
  return {
    id: `TR-${n}`,
    engagement_id: 'ENG-2026-001',
    kind: 'transition',
    actor: 'A1',
    summary: '',
    created_at: at,
    payload: { object: 'engagement', to: n % 2 ? 'P1' : 'P2' },
  } as TrailEntry
}

// 同 GET /trail?newest=true&limit=SIZE&before=: 写在游标之前的最近 SIZE 条, 按写入顺序
function newestPage(trail: TrailEntry[], before?: string): TrailEntry[] {
  const end = before ? trail.findIndex((item) => item.id === before) : trail.length
  return end < 0 ? [] : trail.slice(Math.max(end - SIZE, 0), end)
}

describe('时间线按页取留痕', () => {
  test('首页含最新一条; 往前翻到最早一条不重不漏, 途中写入的新留痕不混进来; 取全之前不加项目已创建', () => {
    const trail = [1, 2, 3, 4, 5, 6, 7].map(entry)
    const pages = [newestPage(trail)]
    expect(trailInOrder(pages).at(-1)?.id).toBe('TR-7')
    expect(buildTimeline(trailInOrder(pages), false).map((item) => item.kind)).not.toContain('created')
    trail.push(entry(8))
    let before = earlierCursor(pages[0] ?? [], SIZE)
    while (before) {
      const page = newestPage(trail, before)
      pages.push(page)
      before = earlierCursor(page, SIZE)
    }
    expect(pages.map((page) => page.length)).toEqual([3, 3, 1])
    expect(trailInOrder(pages).map((item) => item.id)).toEqual(trail.slice(0, 7).map((item) => item.id))
    const items = buildTimeline(trailInOrder(pages), true)
    expect(items.map((item) => item.key)).toEqual(['created', ...trail.slice(0, 7).map((item) => item.id)])
    expect(items[0]).toMatchObject({ kind: 'created', at: trail[0]?.created_at })
  })

  test('总数恰为整页时多翻一次得到空页才停; 没有 ID 的条目不当游标', () => {
    const trail = [1, 2, 3, 4, 5, 6].map(entry)
    expect(earlierCursor(newestPage(trail, 'TR-4'), SIZE)).toBe('TR-1')
    expect(earlierCursor(newestPage(trail, 'TR-1'), SIZE)).toBeUndefined()
    expect(earlierCursor([{ ...entry(1), id: null }, entry(2), entry(3)], SIZE)).toBeUndefined()
  })
})
