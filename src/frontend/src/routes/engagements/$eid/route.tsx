import { createFileRoute, Link, Outlet } from '@tanstack/react-router'

export const Route = createFileRoute('/engagements/$eid')({ component: EngagementLayout })

const NAV = [
  { to: '/engagements/$eid', label: 'Overview', exact: true },
  { to: '/engagements/$eid/gate', label: 'Gate' },
  { to: '/engagements/$eid/reviews', label: 'Review queue' },
  { to: '/engagements/$eid/evidence', label: 'Evidence' },
  { to: '/engagements/$eid/contracts', label: 'Contracts' },
  { to: '/engagements/$eid/misstatements', label: 'Misstatements' },
  { to: '/engagements/$eid/report', label: 'Report' },
  { to: '/engagements/$eid/trail', label: 'Audit trail' },
] as const

function EngagementLayout() {
  const { eid } = Route.useParams()
  return (
    <div className="space-y-6">
      <div>
        <p className="text-muted-foreground text-sm">Engagement</p>
        <h1 className="font-heading font-semibold text-xl">{eid}</h1>
      </div>
      <nav className="flex flex-wrap gap-4 border-b pb-2 text-sm">
        {NAV.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            params={{ eid }}
            activeOptions={{ exact: 'exact' in item }}
            className="text-muted-foreground hover:text-foreground"
            activeProps={{ className: 'text-foreground font-medium' }}
          >
            {item.label}
          </Link>
        ))}
      </nav>
      <Outlet />
    </div>
  )
}
