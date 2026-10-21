import { Link, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import type { UnrecognisedSuggestion } from '@/client/types.gen'
import { Code, StatusPill } from '@/components/chrome'
import { WithdrawDecision } from '@/components/withdraw-decision'
import { useActorName } from '@/lib/actor'
import { decisionsDisabled } from '@/lib/engagement'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import { procedureName } from '@/lib/review'
import { useOverview } from '@/lib/use-engagement'

// FIN-01 闸门材料的 "AI 建议为错报但未认定" 一节 (需求 R364, 后端 R363 的 schedule.unrecognised): 只提示, 不阻塞.
// 金额超过明显微小临界值的醒目, 超过实际执行的重要性或达到重要性水平的标红, 并写对候选意见可能的影响 (would_qualify).
// 要改判时撤回那条复核决定 (总览 can_withdraw 为真时), 再到该任务的表单视图重新选择
const LEVEL_TONES = {
  trivial: 'neutral',
  above_trivial: 'warn',
  above_performance: 'risk',
  material: 'risk',
} as const

export function UnrecognisedSuggestions({ eid, items }: { eid: string; items: UnrecognisedSuggestion[] }) {
  const { t, formatDateTime } = useI18n()
  const actorName = useActorName()
  const overview = useOverview(eid)
  const navigate = useNavigate()
  const labels = t.completion.unrecognised
  if (!items.length) return null
  return (
    <section className="flex flex-col gap-2" data-testid="unrecognised-suggestions">
      <h3 className="m-0 font-semibold text-[14px] text-risk">{labels.title(items.length)}</h3>
      <span className="text-[13px] text-muted-foreground">{labels.intro}</span>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
        {items.map((item) => {
          const red = LEVEL_TONES[item.level] === 'risk'
          const taskId = item.task_id
          const recordId = item.record_id
          return (
            <li
              key={item.judgment_id}
              className={cn(
                'flex flex-col gap-2 rounded-lg border px-3.5 py-2.5 text-[14px]',
                red && 'border-risk bg-risk-soft',
              )}
              data-testid={`unrecognised-${item.subject_id}`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <Code className="font-semibold">{item.procedure_id}</Code>
                <span>{procedureName(t, item.procedure_id)}</span>
                <Code>{item.subject_id}</Code>
                <StatusPill tone={LEVEL_TONES[item.level]}>{labels.levels[item.level]}</StatusPill>
                <span className="flex-1" />
                <Code className={cn('font-semibold', red && 'text-risk')}>{formatAud(item.amount_cents)}</Code>
              </div>
              <span>
                {item.account}
                {item.account_code ? <Code className="ml-1.5">{item.account_code}</Code> : null} ·{' '}
                {t.assertions[item.assertion]} · {t.directions[item.direction]}
              </span>
              <span className="text-[13px] text-muted-foreground">
                {labels.reviewed(
                  item.decision ? t.decisions[item.decision] : t.clientPage.notRecorded,
                  [actorName(item.reviewed_by), formatDateTime(item.decided_at)].filter(Boolean).join(' · '),
                )}
              </span>
              {red ? (
                <span className="font-semibold text-[13px] text-risk">
                  {item.would_qualify ? labels.wouldQualify : labels.wouldNotQualify}
                </span>
              ) : null}
              <div className="flex flex-wrap items-start gap-2">
                {taskId ? (
                  <Link
                    to="/engagements/$eid/reviews/$taskId"
                    params={{ eid, taskId }}
                    className="self-center text-[13px] text-primary hover:underline"
                  >
                    {labels.open}
                  </Link>
                ) : null}
                {overview?.can_withdraw && taskId && recordId ? (
                  <WithdrawDecision
                    eid={eid}
                    recordId={recordId}
                    label={labels.redecide}
                    disabled={decisionsDisabled(overview)}
                    onDone={() => void navigate({ to: '/engagements/$eid/reviews/$taskId', params: { eid, taskId } })}
                  />
                ) : null}
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
