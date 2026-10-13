import { useQuery } from '@tanstack/react-query'
import { engagementsGetEngagementOptions } from '@/client/@tanstack/react-query.gen'
import type { EngagementOverview } from '@/client/types.gen'

export function useEngagement(eid: string) {
  return useQuery({
    ...engagementsGetEngagementOptions({ path: { eid } }),
    refetchInterval: (query) => (query.state.data?.running ? 2000 : false),
  })
}

export function useOverview(eid: string): EngagementOverview | undefined {
  return useEngagement(eid).data
}
