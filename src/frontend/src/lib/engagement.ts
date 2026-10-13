import type { QueryClient } from '@tanstack/react-query'
import type { EngagementOverview } from '@/client/types.gen'

export function decisionsDisabled(overview: EngagementOverview | undefined): boolean {
  if (!overview) return true
  return Boolean(overview.running || overview.run_error != null || overview.engagement.locked)
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
