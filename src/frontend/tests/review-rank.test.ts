// 复核队列排序 (lib/review.rankTasks) 与人工任务分组 (splitManual) 的测试, bun test 运行 (不在 tsconfig 的 src 里, 不经 tsc).
// 数据是合成的: 判断只填排序用到的字段.
import { describe, expect, test } from 'bun:test'
import type { EngagementOverview, ReviewTaskView } from '../src/client/types.gen'
import { rankTasks, splitManual } from '../src/lib/review'

function view(
  id: string,
  kind: string,
  created: string,
  confidence: number,
  amount: number | null,
  status = 'pending',
) {
  const draft =
    amount == null
      ? null
      : { type: 'factual', account: 'Cash', assertion: 'Cutoff', amount_cents: amount, direction: 'overstated' }
  return {
    task: {
      id,
      version: 1,
      kind,
      procedure_id: 'CSH-02',
      target_type: 'Judgment',
      target_id: `J-${id}`,
      target_version: 1,
      created_at: created,
      status,
    },
    target: {
      id: `J-${id}`,
      subject_id: id,
      conclusion: 'synthetic',
      confidence,
      proposed_action: 'accept',
      misstatement_draft: draft,
      exception_ids: [],
      autonomy_level: 'L3',
    },
    allowed_decisions: [],
  } as unknown as ReviewTaskView
}

// 两条待决争议: 晚建的 D-late 金额很大 (档位最高) 且置信度 0.95, 早建的 D-early 金额很小; 一条已决定的争议, 两条普通
// 任务. 按档位或金额排时 D-late 在前, 按创建时间时 D-early 在前, 两者分得开 (第 1 轮审核 R151-01)
const VIEWS = [
  view('N-big', 'normal', '2026-01-01T00:00:01Z', 0.5, 9_000_000),
  view('D-late', 'dispute', '2026-01-01T00:00:05Z', 0.95, 99_999_999),
  view('N-small', 'normal', '2026-01-01T00:00:02Z', 0.5, 100),
  view('D-early', 'dispute', '2026-01-01T00:00:04Z', 0.5, 10),
  view('D-done', 'dispute', '2026-01-01T00:00:00Z', 0.5, 1, 'decided'),
]

function order(features: string[] | undefined): string[] {
  const overview = { features, performance_materiality_cents: 1000 } as unknown as EngagementOverview
  return rankTasks(VIEWS, new Map(), overview).map((ranked) => ranked.view.task.id)
}

describe('rankTasks', () => {
  test('没开 collab 时按档位, 金额, 创建时间 (同原来)', () => {
    expect(order(['critic'])).toEqual(['D-late', 'N-big', 'N-small', 'D-early', 'D-done'])
    expect(order(undefined)).toEqual(order(['critic']))
  })

  test('开了 collab 时待决争议按创建时间 (不按档位或金额) 排最前, 其余相对顺序不变', () => {
    const on = order(['collab', 'critic'])
    expect(on.slice(0, 2)).toEqual(['D-early', 'D-late'])
    const rest = order(['critic']).filter((id) => id !== 'D-early' && id !== 'D-late')
    expect(on.slice(2)).toEqual(rest)
  })
})

describe('splitManual', () => {
  test('人工程序的任务单列 (需求 R303), 两组各自保持原顺序, 不排序', () => {
    const views = [
      view('M-2', 'manual', '2026-01-01T00:00:03Z', 0.5, null),
      ...VIEWS.slice(0, 2),
      view('M-1', 'manual', '2026-01-01T00:00:01Z', 0.5, null),
      ...VIEWS.slice(2),
    ]
    const [reviews, manual] = splitManual(views)
    expect(reviews.map((item) => item.task.id)).toEqual(VIEWS.map((item) => item.task.id))
    expect(manual.map((item) => item.task.id)).toEqual(['M-2', 'M-1'])
    expect(splitManual([])).toEqual([[], []])
  })
})
