import type { AssistantPlan } from '@/client/types.gen'

// 已保存的重复方案只显示一份; 完成及取消记录仍由服务端保留.
export function pendingPlans(plans: AssistantPlan[]): AssistantPlan[] {
  const scope = (plan: AssistantPlan) =>
    JSON.stringify([
      plan.proposed_for,
      plan.action,
      plan.targets.map((target) => [target.kind, target.id, target.version, target.fingerprint]).sort(),
    ])
  const completed = new Set(plans.filter((plan) => plan.status === 'completed').map(scope))
  const seen = new Set<string>()
  return plans.filter((plan) => {
    if (plan.status !== 'proposed') return false
    const targetScope = scope(plan)
    const key = JSON.stringify([targetScope, plan.reason])
    if (completed.has(targetScope) || seen.has(key)) return false
    seen.add(key)
    return true
  })
}
