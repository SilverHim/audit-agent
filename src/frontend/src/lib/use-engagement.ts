import { useQuery } from '@tanstack/react-query'
import { engagementsGetEngagementOptions } from '@/client/@tanstack/react-query.gen'
import type { EngagementOverview } from '@/client/types.gen'
import { busy } from '@/lib/activity'

// 在执行或有当前活动 (需求 R362) 时每 2 秒重取, 状态条与按钮旁的说明随之更新
export function useEngagement(eid: string) {
  return useQuery({
    ...engagementsGetEngagementOptions({ path: { eid } }),
    refetchInterval: (query) => (busy(query.state.data) ? 2000 : false),
  })
}

export function useOverview(eid: string): EngagementOverview | undefined {
  return useEngagement(eid).data
}
