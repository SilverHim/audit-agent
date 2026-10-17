import { createFileRoute, Link } from '@tanstack/react-router'
import { MessageCircle } from 'lucide-react'
import { PageHeader } from '@/components/chrome'
import { GatePanel } from '@/components/gate-form'
import { buttonVariants } from '@/components/ui/button'
import { decisionsDisabled } from '@/lib/engagement'
import { useI18n } from '@/lib/i18n'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/gate')({ component: Gate })

// 表单视图: 当前阶段决定的材料与决定卡片, 不经对话
function Gate() {
  const { eid } = Route.useParams()
  const { t } = useI18n()
  const overview = useEngagement(eid).data
  return (
    <>
      <PageHeader
        title={
          overview?.current_gate ? `${overview.current_gate} · ${t.gates[overview.current_gate]}` : t.formView.gate
        }
        subtitle={t.workspace.formView}
        actions={
          <Link
            to="/engagements/$eid"
            params={{ eid }}
            search={{}}
            className={buttonVariants({ variant: 'outline', size: 'lg' })}
          >
            <MessageCircle className="size-4" aria-hidden="true" />
            {t.formView.backToConcierge}
          </Link>
        }
      />
      <div className="mx-auto flex w-full max-w-[900px] flex-col px-4 py-6 sm:px-7">
        <GatePanel eid={eid} disabled={decisionsDisabled(overview)} />
      </div>
    </>
  )
}
