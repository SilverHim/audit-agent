import { useIsMutating, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { RefreshCw } from 'lucide-react'
import { type ComponentProps, useId } from 'react'
import { toast } from 'sonner'
import { reviewsRerunProceduresMutation } from '@/client/@tanstack/react-query.gen'
import type { EngagementOverview, ProcedureSummary } from '@/client/types.gen'
import { StatusPill } from '@/components/chrome'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button, buttonVariants } from '@/components/ui/button'
import { invalidateEngagement } from '@/lib/engagement'
import { apiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useOverview } from '@/lib/use-engagement'
import type { Messages } from '@/locales/en'

// 过期标记与重跑入口 (后端 R274): 过期与能否重跑取总览的 stale, rerunnable, can_rerun, 重跑调 POST /procedures/rerun

type Target = Pick<ProcedureSummary, 'id' | 'phase' | 'status'>

// short: 表格行内用短标记, 完整说明放 title; onSoft: 放在同为 warn 底色的提示块里时换白底加边框, 与块的底色区分
export function StaleBadge({ short = false, onSoft = false }: { short?: boolean; onSoft?: boolean }) {
  const { t } = useI18n()
  return (
    <StatusPill tone="warn" className={cn(onSoft && 'border border-warn-strong/40 bg-card')}>
      <span title={short ? t.rerun.stale : undefined}>{short ? t.rerun.staleShort : t.rerun.stale}</span>
    </StatusPill>
  )
}

// 现在不能重跑的原因, 能重跑为 null; procedure 为空时按 "全部重跑" 判断 (按钮只给过期的程序显示).
// 全部重跑在没有可重跑的程序时先说没有, 不说位置或出错 (到了别的位置也重跑不了)
export function rerunBlocked(overview: EngagementOverview | undefined, t: Messages, procedure?: Target): string | null {
  if (!overview) return t.app.loading
  if (overview.engagement.locked) return t.rerun.blocked.locked
  if (procedure && procedure.phase !== 'P2_Executing') return t.rerun.blocked.planning
  if (procedure && procedure.status === 'executing') return t.rerun.blocked.queued
  if (overview.running) return t.rerun.blocked.running
  if (!procedure && !overview.procedures.some((item) => item.rerunnable)) return t.rerun.blocked.none
  // 功能集与项目冻结的不同 (FEATURES_MISMATCH): 布局不给继续执行按钮, 不提示 "请先继续执行"
  if (overview.run_error?.startsWith('FEATURES_MISMATCH')) return t.rerun.blocked.featuresMismatch
  if (overview.run_error) return t.rerun.blocked.runError
  if (!overview.can_rerun) return t.rerun.blocked.position
  return null
}

// 过期程序分三组 (按 ids 的顺序): 可重跑的 (与 "全部重跑 (n)" 的 n 同源, 即总览 rerunnable), 已在等重跑的执行阶段
// 程序, 只作标记的计划阶段程序. 总览里查不到的归入可重跑一组
export function staleGroups(overview: EngagementOverview | undefined, ids: string[]) {
  const byId = new Map((overview?.procedures ?? []).map((item) => [item.id, item]))
  const group = (id: string) => {
    const item = byId.get(id)
    if (!item || item.rerunnable) return 'rerunnable'
    return item.phase === 'P2_Executing' ? 'queued' : 'marked'
  }
  return {
    rerunnable: ids.filter((id) => group(id) === 'rerunnable'),
    queued: ids.filter((id) => group(id) === 'queued'),
    marked: ids.filter((id) => group(id) === 'marked'),
  }
}

function rerunKey(eid: string) {
  return ['procedures-rerun', eid]
}

// 出错提示按错误码取本地文案, 不显示后端消息: INVALID_VALUE 为所选程序已不可重跑 (缓存过时), 其余取通用错误码文案
function rerunErrorText(error: unknown, t: Messages): string {
  const code = apiCode(error)
  if (code === 'INVALID_VALUE') return t.rerun.errors.outdated
  if (code === 'FEATURES_MISMATCH') return t.rerun.blocked.featuresMismatch
  return (code && t.apiErrors.codes[code]) || t.rerun.errors.failed(code)
}

// 本项目的重跑请求: 各重跑按钮共用 mutationKey, 有一个在提交就都置灰, 提交中不再发第二次 (连点只发一次);
// 成功失败都刷新总览与详情, 按钮与原因跟上后端现状
export function useRerun(eid: string) {
  const queryClient = useQueryClient()
  const { t } = useI18n()
  const mutationKey = rerunKey(eid)
  const mutation = useMutation({
    ...reviewsRerunProceduresMutation(),
    mutationKey,
    onSuccess: () => {
      toast.success(t.rerun.started)
    },
    onError: (error) => {
      toast.error(rerunErrorText(error, t))
    },
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
  const pending = useIsMutating({ mutationKey }) > 0
  const submit = (procedureIds: string[] | null) => {
    if (queryClient.isMutating({ mutationKey }) > 0) return
    mutation.mutate({ path: { eid }, body: { procedure_ids: procedureIds } })
  }
  return { submit, pending }
}

// 重跑一条 (procedure) 或全部可重跑的过期程序; 不能重跑时置灰并写明原因: 默认写在按钮下, reasonAs title 时
// (表头) 放进按钮的 title, 另以视觉隐藏的文字经 aria-describedby 关联, 不撑高表头
export function RerunButton({
  eid,
  overview,
  procedure,
  size = 'lg',
  reasonAs = 'text',
}: {
  eid: string
  overview: EngagementOverview | undefined
  procedure?: Target
  size?: ComponentProps<typeof Button>['size']
  reasonAs?: 'text' | 'title'
}) {
  const { t } = useI18n()
  const rerun = useRerun(eid)
  const reasonId = useId()
  const blocked = rerunBlocked(overview, t, procedure)
  const count = overview?.procedures.filter((item) => item.rerunnable).length ?? 0
  const inTitle = reasonAs === 'title'
  return (
    <span
      className={cn('inline-flex max-w-[320px] flex-col items-start gap-1', inTitle && 'max-w-none')}
      title={inTitle ? (blocked ?? undefined) : undefined}
    >
      <Button
        type="button"
        variant="outline"
        size={size}
        disabled={blocked != null || rerun.pending}
        aria-describedby={blocked ? reasonId : undefined}
        title={inTitle ? (blocked ?? undefined) : undefined}
        onClick={() => rerun.submit(procedure ? [procedure.id] : null)}
      >
        <RefreshCw className="size-4" aria-hidden="true" />
        {procedure ? t.rerun.one : t.rerun.all(count)}
      </Button>
      {blocked ? (
        <span id={reasonId} className={inTitle ? 'sr-only' : 'text-[12px] text-muted-foreground'}>
          {blocked}
        </span>
      ) : null}
    </span>
  )
}

// 工作区: 有过期程序时提示, 分组写明哪些能重跑 (给 "全部重跑" 与程序页入口), 哪些已在等重跑, 哪些只作标记
export function StaleNotice({ eid, overview }: { eid: string; overview: EngagementOverview | undefined }) {
  const { t, lang } = useI18n()
  const ids = overview?.stale_procedure_ids ?? []
  if (!ids.length) return null
  const groups = staleGroups(overview, ids)
  const join = (items: string[]) => items.join(lang === 'zh' ? '、' : ', ')
  return (
    <section className="flex flex-col gap-2.5 rounded-xl bg-warn-soft p-4">
      <p className="m-0 font-semibold text-[14px]">{t.rerun.notice(ids.length)}</p>
      <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-[14px]">
        {groups.rerunnable.length ? (
          <li>{t.rerun.groups.rerunnable(groups.rerunnable.length, join(groups.rerunnable))}</li>
        ) : null}
        {groups.queued.length ? <li>{t.rerun.groups.queued(join(groups.queued))}</li> : null}
        {groups.marked.length ? <li>{t.rerun.groups.marked(join(groups.marked))}</li> : null}
      </ul>
      <div className="flex flex-wrap items-start gap-2.5">
        {groups.rerunnable.length ? <RerunButton eid={eid} overview={overview} /> : null}
        <Link
          to="/engagements/$eid/procedures"
          params={{ eid }}
          search={{ selected: groups.rerunnable[0] ?? ids[0] }}
          className={buttonVariants({ variant: 'ghost', size: 'lg' })}
        >
          {t.rerun.openProcedures}
        </Link>
      </div>
    </section>
  )
}

// FIN-01 材料: 尚未重跑的过期程序分两行, 可重跑的 (数量与程序页入口) 与只作标记的计划阶段程序 (说明为何不能重跑)
export function FinStaleAlert({ eid, ids }: { eid: string; ids: string[] }) {
  const { t, lang } = useI18n()
  const overview = useOverview(eid)
  if (!ids.length) return null
  const groups = staleGroups(overview, ids)
  const join = (items: string[]) => items.join(lang === 'zh' ? '、' : ', ')
  const pending = [...groups.rerunnable, ...groups.queued]
  return (
    <Alert>
      <AlertDescription className="flex flex-col items-start gap-1.5">
        {pending.length ? (
          <>
            <span>{t.rerun.finStale(pending.length, join(pending))}</span>
            <Link
              to="/engagements/$eid/procedures"
              params={{ eid }}
              search={{ selected: pending[0] }}
              className="text-accent-foreground underline underline-offset-2"
            >
              {t.rerun.openProcedures}
            </Link>
          </>
        ) : null}
        {groups.marked.length ? <span>{t.rerun.finMarked(join(groups.marked))}</span> : null}
      </AlertDescription>
    </Alert>
  )
}
