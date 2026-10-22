import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { workpapersListWorkpapersOptions } from '@/client/@tanstack/react-query.gen'
import { useI18n } from '@/lib/i18n'
import { useOverview } from '@/lib/use-engagement'

export function WorkpaperLinks({ eid, procedureIds }: { eid: string; procedureIds: string[] }) {
  const { t } = useI18n()
  const overview = useOverview(eid)
  const papers = useQuery({
    ...workpapersListWorkpapersOptions({ path: { eid } }),
    staleTime: 2000,
    refetchInterval: overview?.running ? 2000 : false,
  })
  const linked = papers.data?.filter(
    (p) => p.procedure_id && procedureIds.includes(p.procedure_id) && p.availability === 'generated',
  )
  if (!linked?.length) return null
  return (
    <span className="ml-2 inline-flex flex-wrap gap-2">
      {linked.map((paper) => (
        <Link
          key={paper.ref}
          className="underline"
          to="/engagements/$eid/workpapers"
          params={{ eid }}
          search={{ ref: paper.ref }}
        >
          {t.workpapers.view} {paper.ref}
        </Link>
      ))}
    </span>
  )
}
