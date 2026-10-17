import { Link } from '@tanstack/react-router'

// 页内的二级切换: 证据 / 合同, 报告 / 错报与意见
type Tab = {
  to:
    | '/engagements/$eid/evidence'
    | '/engagements/$eid/contracts'
    | '/engagements/$eid/report'
    | '/engagements/$eid/misstatements'
  label: string
}

export function SubTabs({ eid, tabs }: { eid: string; tabs: Tab[] }) {
  return (
    <nav className="flex gap-1 border-b bg-card px-4 sm:px-7">
      {tabs.map((tab) => (
        <Link
          key={tab.to}
          to={tab.to}
          params={{ eid }}
          className="-mb-px border-transparent border-b-2 px-3 py-2.5 font-medium text-[13.5px] text-muted-foreground hover:text-foreground"
          activeProps={{ className: 'border-primary! font-semibold text-foreground!' }}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  )
}
