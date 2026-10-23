import { expect, test } from 'bun:test'
import type { AssistantPlan } from '../src/client/types.gen'
import { pendingPlans } from '../src/lib/assistant-plans'

const plan: AssistantPlan = {
  id: 'AP-1',
  action: 'confirm_reviews',
  proposed_for: 'U-1',
  created_at: '2026-10-24T00:00:00Z',
  instruction: 'confirm',
  reason: null,
  status: 'proposed',
  targets: [
    { kind: 'review_task', id: 'T-1', version: 1, fingerprint: 'original', label: 'Review 1' },
    { kind: 'review_task', id: 'T-2', version: 1, fingerprint: 'original-2', label: 'Review 2' },
  ],
}

test('重复待确认方案只显示一次, 与范围顺序无关', () => {
  expect(pendingPlans([plan, { ...plan, id: 'AP-2', targets: [...plan.targets].reverse() }])).toEqual([plan])
})

test('范围变化或其他授权人的方案保留, 已完成范围不再出现旧重复卡片', () => {
  const changed = {
    ...plan,
    id: 'AP-3',
    targets: plan.targets.slice(0, 1).map((target) => ({ ...target, fingerprint: 'changed' })),
  }
  const other = { ...plan, id: 'AP-4', proposed_for: 'U-2' }
  expect(
    pendingPlans([
      plan,
      changed,
      other,
      { ...plan, id: 'AP-2', status: 'completed', reason: 'Reason supplied when confirming' },
    ]),
  ).toEqual([changed, other])
  expect(
    pendingPlans([
      { ...plan, status: 'cancelled' },
      { ...plan, id: 'AP-new' },
    ]),
  ).toHaveLength(1)
})
