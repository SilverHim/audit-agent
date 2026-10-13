import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { reviewsListReviewTasksOptions } from '@/client/@tanstack/react-query.gen'
import { QueryState } from '@/components/query-state'
import { ReviewDetail } from '@/components/review-detail'
import { decisionsDisabled } from '@/lib/engagement'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/reviews/$taskId')({ component: ReviewTaskPage })

function ReviewTaskPage() {
  const { eid, taskId } = Route.useParams()
  const overview = useEngagement(eid)
  const tasks = useQuery(reviewsListReviewTasksOptions({ path: { eid } }))
  const item = tasks.data?.find((task) => task.task.id === taskId)
  return (
    <QueryState isPending={tasks.isPending} error={tasks.error}>
      {item ? (
        <ReviewDetail eid={eid} item={item} disabled={decisionsDisabled(overview.data)} />
      ) : (
        <p className="text-muted-foreground text-sm">Review task {taskId} was not found.</p>
      )}
    </QueryState>
  )
}
