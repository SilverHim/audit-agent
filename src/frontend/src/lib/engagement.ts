import type { QueryClient } from '@tanstack/react-query'
import type { EngagementOverview, GateView } from '@/client/types.gen'

export function decisionsDisabled(overview: EngagementOverview | undefined): boolean {
  if (!overview) return true
  return Boolean(overview.running || overview.run_error != null || overview.engagement.locked)
}

// 闸门决定卡片的 key: 闸门, 呈报对象与版本. 呈报对象换了 (如 PLN-15 退回后 PLAN-1 v1 换成 PLAN-2 v1) 卡片就重新挂载,
// 表单的初值 (如计划的三项确认) 按新的材料取
export function decisionCardKey(view: Pick<GateView, 'gate' | 'target_id' | 'target_version'>): string {
  return `${view.gate}:${view.target_id}:${view.target_version}`
}

export function invalidateEngagement(queryClient: QueryClient, eid: string) {
  return queryClient.invalidateQueries({
    predicate: (query) => {
      const head = query.queryKey[0]
      if (!head || typeof head !== 'object') return false
      const path = (head as { path?: { eid?: string } }).path
      return path?.eid === eid
    },
  })
}

export function invalidateEngagementList(queryClient: QueryClient) {
  return queryClient.invalidateQueries({
    predicate: (query) => {
      const head = query.queryKey[0]
      return Boolean(head && typeof head === 'object' && '_id' in head && head._id === 'engagementsListEngagements')
    },
  })
}
