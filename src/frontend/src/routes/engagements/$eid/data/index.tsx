import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { AlertTriangle, CircleCheck, Clock3, FlaskConical, FolderOpen, Link2, RefreshCw, Upload } from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { evidenceListDocumentsOptions } from '@/client/@tanstack/react-query.gen'
import type { DocumentRef, DocumentType, XeroSource } from '@/client/types.gen'
import { OpenBatches, useBatchUpload } from '@/components/batch-upload'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { DataReadyButton } from '@/components/data-ready'
import { MarkedRequests } from '@/components/data-requests'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { XeroProfileCard } from '@/components/xero-profile'
import { useActorName } from '@/lib/actor'
import {
  type DataCheck,
  type DataSummary,
  type TableSummary,
  useDataRequests,
  useDataSummary,
  useDemoImport,
  useSetDocumentType,
  useXeroChooseTenant,
  useXeroConnect,
  useXeroDisconnect,
  useXeroSync,
  useXeroTenants,
} from '@/lib/data-api'
import { checkDetail } from '@/lib/data-checks'
import { demoButton, demoResultText } from '@/lib/demo-import'
import { invalidateEngagement } from '@/lib/engagement'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useEngagement } from '@/lib/use-engagement'
import { skippedByReason, syncFailureText, useXeroCallbackNotice, type XeroNotice, xeroNotice } from '@/lib/xero'
import { checkStatus, pendingCheck } from '@/lib/xero-profile'

export const Route = createFileRoute('/engagements/$eid/data/')({
  validateSearch: (search: Record<string, unknown>): XeroNotice => xeroNotice(search),
  component: DataPage,
})

const STEP_DONE: Record<string, number> = { received: 1, classified: 2, extracted: 6 }
const FAILED_AT: Record<string, number> = { classify: 1, read: 2, extract: 3, link: 4 }
// 合同与回函分类后还要抽取, 其他类型分类后即处理完 (后端停在 classified)
const EXTRACTED_TYPES = new Set<DocumentType>(['contract', 'confirmation'])

function finished(doc: DocumentRef): boolean {
  return doc.status === 'extracted' || (doc.status === 'classified' && !EXTRACTED_TYPES.has(doc.type))
}

function DataPage() {
  const { eid } = Route.useParams()
  const { xero, reason } = Route.useSearch()
  const navigate = useNavigate()
  const { t, formatDate } = useI18n()
  const overview = useEngagement(eid).data
  const summary = useDataSummary(eid).data ?? null
  // 数据管理智能体处理文件时跟着概览一起刷新处理进度
  const documents = useQuery({
    ...evidenceListDocumentsOptions({ path: { eid } }),
    refetchInterval: summary?.processing ? 3000 : false,
  })
  // "上传资料": 账表与其他文件一起交给数据管理智能体识别, 看确认单后导入
  const uploader = useBatchUpload(eid)
  const requests = useDataRequests(eid)

  // Xero 授权回调带回 ?xero=connected 或 ?xero=error&reason=<原因代码>, 提示一次后清掉
  const clearNotice = useCallback(
    () => void navigate({ to: '/engagements/$eid/data', params: { eid }, search: {}, replace: true }),
    [eid, navigate],
  )
  useXeroCallbackNotice({ xero, reason }, t, clearNotice)

  const docs = documents.data ?? []
  const importedTables = summary?.tables.filter((table) => table.source === 'import').length ?? 0
  // Xero 组织信息与客户画像不一致且还没处理时, 在数据源下面列出核对卡 (锁定的项目不提示)
  const profileCheck = pendingCheck(summary?.sources.xero, Boolean(overview?.engagement.locked))

  return (
    <>
      <PageHeader
        title={t.data.title}
        subtitle={
          overview
            ? t.data.period(formatDate(overview.engagement.period_start), formatDate(overview.engagement.period_end))
            : undefined
        }
        actions={
          <>
            {summary?.processing ? (
              <StatusPill tone="accent">
                <span className="size-2 rounded-full bg-primary" />
                {t.data.agentBusy(summary.processing)}
              </StatusPill>
            ) : null}
            <Button type="button" size="lg" disabled={uploader.pending} onClick={() => uploader.choose()}>
              <Upload className="size-4" aria-hidden="true" />
              {t.data.upload}
            </Button>
          </>
        }
      />
      {uploader.element}

      <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-[22px] px-4 py-6 sm:px-7">
        {overview?.awaiting_data ? (
          <section className="flex flex-wrap items-center justify-between gap-3 rounded-[14px] border-[1.5px] border-primary bg-card px-5 py-4">
            <span className="font-semibold text-[15px]">{t.data.awaiting}</span>
            <DataReadyButton eid={eid} size="lg" />
          </section>
        ) : null}

        <DemoCard eid={eid} summary={summary} />

        <OpenBatches eid={eid} onOpen={uploader.open} />

        <section aria-label={t.data.sources} className="flex flex-col gap-3">
          <h2 className="m-0 font-semibold text-[13px] text-muted-foreground tracking-wide">{t.data.sources}</h2>
          <div className="flex flex-wrap items-stretch gap-4">
            <XeroCard eid={eid} summary={summary} />
            <SourceCard
              icon={<FolderOpen className="size-[18px]" aria-hidden="true" />}
              iconClass="bg-standard-soft text-standard"
              title={t.data.offline.title}
              kind={t.data.offline.kind}
            >
              <p className="m-0 text-[13px] text-[#2d3443]">{t.data.offline.accepts}</p>
              <span className="text-[13px] text-[#4a5262]">{t.data.offline.counts(importedTables, docs.length)}</span>
              <div className="mt-auto flex flex-wrap gap-2 pt-1">
                <Button type="button" variant="outline" disabled={uploader.pending} onClick={() => uploader.choose()}>
                  {t.data.upload}
                </Button>
              </div>
            </SourceCard>
          </div>
        </section>

        {profileCheck ? (
          <XeroProfileCard
            key={profileCheck.key}
            eid={eid}
            check={profileCheck}
            disabled={Boolean(overview?.running || summary?.sources.xero?.syncing)}
          />
        ) : null}

        {summary ? (
          <>
            <FinancialTables eid={eid} tables={summary.tables} onImport={() => uploader.choose()} />
            <Checks eid={eid} checks={summary.checks} />
          </>
        ) : null}

        <MarkedRequests requests={(requests.data ?? []).filter((item) => item.status === 'unavailable')} />

        <Documents
          eid={eid}
          docs={docs}
          loading={documents.isPending}
          onFiles={(files) => uploader.send(files)}
          onChoose={() => uploader.choose()}
        />
      </div>
    </>
  )
}

// 导入演示资料 (后端 R269, R270): 只对带补造资料的 Xero 演示数据集显示; 按钮的阶段取概览的 demo.suggested (后端按流程
// 停的位置给出), 不在要导入的位置时按钮在而不可点. 发起后在后台导入, 进度同其他作业看 processing; 下方一行是上次
// 导入的结果 (demo.last)
function DemoCard({ eid, summary }: { eid: string; summary: DataSummary | null }) {
  const { t } = useI18n()
  const run = useDemoImport(eid)
  const button = demoButton(summary?.demo, t)
  if (!button || !summary) return null
  const last = summary.demo.last ?? null
  const hintId = `demo-hint-${eid}`
  const start = (stage: string) =>
    run.mutate(stage, {
      onSuccess: (result) =>
        toast.success(t.data.demo.started(result.tables?.length ?? 0, result.uploaded?.length ?? 0)),
      onError: (error) => toast.error(errorText(error, t)),
    })
  const disabled = button.stage === null || run.isPending || summary.processing > 0
  return (
    <section
      aria-label={t.data.demo.title}
      className="flex flex-wrap items-center justify-between gap-3 rounded-[14px] border border-dashed bg-card px-5 py-4"
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent text-primary">
          <FlaskConical className="size-[18px]" aria-hidden="true" />
        </span>
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="font-semibold text-[15px]">{t.data.demo.title}</span>
          <span className="text-[13px] text-muted-foreground">{t.data.demo.intro}</span>
          {last ? (
            <span className="text-[13px] text-[#4a5262]">{t.data.demo.last(demoResultText(last, t))}</span>
          ) : null}
        </div>
      </div>
      <div className="flex flex-col items-end gap-1">
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          aria-describedby={button.hint ? hintId : undefined}
          onClick={() => {
            if (button.stage) start(button.stage)
          }}
        >
          {button.label}
        </Button>
        {button.hint ? (
          <span id={hintId} className="text-[12.5px] text-muted-foreground">
            {button.hint}
          </span>
        ) : null}
      </div>
    </section>
  )
}

function SourceCard({
  icon,
  iconClass,
  title,
  kind,
  badge,
  children,
}: {
  icon: ReactNode
  iconClass: string
  title: string
  kind: string
  badge?: ReactNode
  children: ReactNode
}) {
  return (
    <article className="flex min-w-0 flex-[1_1_320px] flex-col gap-3 rounded-[14px] border bg-card px-5 py-[18px]">
      <div className="flex items-center gap-2.5">
        <span className={cn('flex size-[34px] items-center justify-center rounded-[9px]', iconClass)}>{icon}</span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="font-semibold text-[15px]">{title}</span>
          <span className="text-[12.5px] text-muted-foreground">{kind}</span>
        </span>
        {badge}
      </div>
      {children}
    </article>
  )
}

const SELECT =
  'h-9 min-w-0 flex-1 rounded-lg border border-input bg-card px-2.5 text-[13px] disabled:cursor-not-allowed disabled:opacity-50'

// Xero 卡片 (后端 R279, R282): 状态一律取概览的 sources.xero, 不从表的来源与 converting 推断. 未连接时连接;
// 已连接时同步与断开, 授权给到多个组织时先选组织 (按 tenant_id 匹配, 选定前同步置灰并说明); 授权失效 (expired)
// 时说明原因并给重新连接, 也可以断开. 同步中 (syncing) 时同步, 组织下拉与断开都置灰, 卡片转圈. 上次同步的结果
// (last_sync): 失败写原因, 有没写的表时逐项写原因.
// 连接, 同步, 选组织, 断开共用一个提交中标记 (同步的 ref, 连点的第二下在重渲染之前就到, 不能只靠 isPending);
// 断开在确认对话框里再点一次, 确认按钮不在原按钮的位置
function XeroCard({ eid, summary }: { eid: string; summary: DataSummary | null }) {
  const { t, lang, formatDateTime } = useI18n()
  const connect = useXeroConnect(eid)
  const sync = useXeroSync(eid)
  const disconnect = useXeroDisconnect(eid)
  const choose = useXeroChooseTenant(eid)
  const [confirming, setConfirming] = useState(false)
  const busy = useRef(false)
  const [submitting, setSubmitting] = useState(false)
  const queryClient = useQueryClient()
  // 连接成功后保持提交中 (整页跳到 Xero). 审计师在 Xero 页按后退, 浏览器从往返缓存恢复本页时 JS 状态原样保留:
  // 复位提交中标记, 并重取本项目数据
  useEffect(() => {
    const restored = (event: PageTransitionEvent) => {
      if (!event.persisted) return
      busy.current = false
      setSubmitting(false)
      void invalidateEngagement(queryClient, eid)
    }
    window.addEventListener('pageshow', restored)
    return () => window.removeEventListener('pageshow', restored)
  }, [queryClient, eid])
  const xero: XeroSource = summary?.sources.xero ?? { status: 'not_connected' }
  const connected = xero.status === 'connected'
  const expired = xero.status === 'expired'
  const linked = connected || expired // 有连接记录
  const syncing = Boolean(xero.syncing)
  const tenants = useXeroTenants(eid, connected).data ?? []
  const chosen = tenants.find((tenant) => tenant.id === xero.tenant_id)
  const unchosen = connected && !xero.tenant_id
  const locked = syncing || submitting
  const hintId = `xero-hint-${eid}`
  const joiner = lang === 'zh' ? '、' : ', '
  const last = xero.last_sync ?? null
  const skipped = last?.ok ? skippedByReason(last.skipped, t, lang) : []
  // 锁定的项目不列核对卡 (状态行不写 "见下方")
  const checked = checkStatus(xero.profile_check, t, Boolean(useEngagement(eid).data?.engagement.locked))
  const differs = Boolean(xero.profile_check?.differences?.length && !xero.profile_check.resolution)

  // 一次只提交一个; action 返回 true 时保持提交中 (连接成功后整页跳到 Xero)
  const submit = async (action: () => Promise<boolean | undefined>) => {
    if (busy.current) return
    busy.current = true
    setSubmitting(true)
    let keep = false
    try {
      keep = (await action()) === true
    } catch (error) {
      toast.error(errorText(error, t))
    } finally {
      if (!keep) {
        busy.current = false
        setSubmitting(false)
      }
    }
  }
  const startConnect = () =>
    submit(async () => {
      await connect.mutateAsync()
      return true
    })
  const startSync = () =>
    submit(async () => {
      await sync.mutateAsync()
      toast.success(t.data.xero.syncStarted)
      return undefined
    })
  const pick = (tenantId: string) =>
    submit(async () => {
      const source = await choose.mutateAsync(tenantId)
      toast.success(t.data.xero.chosen(source.organisation ?? ''))
      return undefined
    })
  const endLink = () =>
    submit(async () => {
      try {
        const { revoked } = await disconnect.mutateAsync()
        if (revoked) toast.success(t.data.xero.disconnected)
        else toast.warning(t.data.xero.notRevoked, { duration: 12000 })
      } finally {
        setConfirming(false)
      }
      return undefined
    })
  return (
    <SourceCard
      icon={<Link2 className="size-[18px]" aria-hidden="true" />}
      iconClass="bg-accent text-accent-foreground"
      title={t.data.xero.name}
      kind={t.data.xero.kind}
      badge={
        <StatusPill tone={connected ? 'ok' : expired ? 'warn' : 'neutral'}>
          {t.data.xero.status[xero.status]}
        </StatusPill>
      }
    >
      {linked ? (
        <dl className="m-0 grid grid-cols-[72px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
          <dt className="text-muted-foreground">{t.data.xero.organisation}</dt>
          <dd className="m-0">{xero.organisation ?? t.data.xero.notChosen}</dd>
          <dt className="text-muted-foreground">{t.data.xero.lastSynced}</dt>
          <dd className="m-0">{xero.last_synced_at ? formatDateTime(xero.last_synced_at) : '—'}</dd>
          <dt className="text-muted-foreground">{t.data.xero.scope}</dt>
          <dd className="m-0">{(xero.scope ?? []).map((kind) => t.data.tables[kind]).join(joiner) || '—'}</dd>
          {last ? (
            <>
              <dt className="text-muted-foreground">{t.data.xero.lastResult}</dt>
              <dd className={cn('m-0', !last.ok && 'text-warn-strong')} data-testid="xero-last-sync">
                {!last.ok ? (
                  t.data.xero.lastFailed(syncFailureText(last.reason, t), formatDateTime(last.at))
                ) : skipped.length ? (
                  <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
                    {skipped.map((item) => (
                      <li key={item.reason}>{t.data.xero.skippedLine(item.tables, item.reason)}</li>
                    ))}
                  </ul>
                ) : (
                  t.data.xero.lastOk
                )}
              </dd>
            </>
          ) : null}
          {checked ? (
            <>
              <dt className="text-muted-foreground">{t.data.xero.profileCheck}</dt>
              <dd className={cn('m-0', differs && 'text-warn-strong')} data-testid="xero-profile-check">
                {checked}
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}
      {connected && tenants.length > 1 ? (
        <div className="flex items-center gap-2 text-[13px]">
          <label htmlFor={`xero-tenant-${eid}`} className="shrink-0 text-muted-foreground">
            {t.data.xero.chooseLabel}
          </label>
          <select
            id={`xero-tenant-${eid}`}
            className={SELECT}
            value={chosen?.id ?? ''}
            disabled={locked}
            onChange={(event) => void pick(event.target.value)}
          >
            {chosen ? null : (
              <option value="" disabled>
                {t.data.xero.choosePlaceholder}
              </option>
            )}
            {tenants.map((tenant) => (
              <option key={tenant.id} value={tenant.id}>
                {tenant.name}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {unchosen ? (
        <p id={hintId} className="m-0 text-[13px] text-warn-strong">
          {t.data.xero.chooseFirst}
        </p>
      ) : null}
      {expired ? (
        <p id={hintId} className="m-0 text-[13px] text-warn-strong">
          {t.data.xero.expiredHint}
        </p>
      ) : null}
      <div className="mt-auto flex flex-wrap gap-2 pt-1">
        {connected ? (
          <Button
            type="button"
            variant="outline"
            disabled={locked || unchosen}
            aria-describedby={unchosen ? hintId : undefined}
            onClick={() => void startSync()}
          >
            <RefreshCw className={cn('size-3.5', syncing && 'animate-spin')} aria-hidden="true" />
            {syncing ? t.data.xero.syncing : t.data.xero.sync}
          </Button>
        ) : (
          <Button
            type="button"
            disabled={locked}
            aria-describedby={expired ? hintId : undefined}
            onClick={() => void startConnect()}
          >
            {expired ? t.data.xero.reconnect : t.data.xero.connect}
          </Button>
        )}
        {linked ? (
          <Button type="button" variant="ghost" disabled={locked} onClick={() => setConfirming(true)}>
            {t.data.xero.disconnect}
          </Button>
        ) : null}
      </div>
      {/* 点对话框外面不关闭: 双击 "断开连接" 的第二下落在遮罩上, 对话框照样留着 */}
      <Dialog open={confirming} disablePointerDismissal onOpenChange={(open) => !busy.current && setConfirming(open)}>
        <DialogContent className="sm:max-w-[460px]">
          <DialogHeader>
            <DialogTitle className="font-semibold text-base">{t.data.xero.disconnectTitle}</DialogTitle>
            <DialogDescription>{t.data.xero.disconnectIntro}</DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2.5">
            <Button type="button" variant="outline" disabled={submitting} onClick={() => setConfirming(false)}>
              {t.dataTable.cancel}
            </Button>
            <Button
              type="button"
              variant="outline"
              className="border-risk text-risk"
              disabled={locked}
              onClick={() => void endLink()}
            >
              {t.data.xero.confirmDisconnect}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </SourceCard>
  )
}

function FinancialTables({ eid, tables, onImport }: { eid: string; tables: TableSummary[]; onImport: () => void }) {
  const { t, formatDate } = useI18n()
  const received = tables.filter((table) => table.status !== 'not_received').length
  const cell = 'border-line-soft border-b px-3 py-[11px]'
  return (
    <section aria-label={t.data.financialData} className="overflow-hidden rounded-[14px] border bg-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-5 pt-4 pb-3">
        <h2 className="m-0 font-semibold text-base">{t.data.financialData}</h2>
        <span className="text-[12.5px] text-muted-foreground">
          {t.data.tableCounts(received, tables.length - received)}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] border-collapse text-[13.5px]">
          <thead>
            <tr className="bg-[#fafbfc] text-left text-[12px] text-muted-foreground">
              <th scope="col" className="border-y px-5 py-2.5 font-semibold">
                {t.data.cols.data}
              </th>
              <th scope="col" className="border-y px-3 py-2.5 font-semibold">
                {t.data.cols.source}
              </th>
              <th scope="col" className="border-y px-3 py-2.5 text-right font-semibold">
                {t.data.cols.records}
              </th>
              <th scope="col" className="border-y px-3 py-2.5 font-semibold">
                {t.data.cols.period}
              </th>
              <th scope="col" className="border-y px-3 py-2.5 font-semibold">
                {t.data.cols.status}
              </th>
              <th scope="col" className="border-y px-5 py-2.5">
                <span className="sr-only">{t.data.cols.actions}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {tables.map((table) => {
              const missing = table.status === 'not_received'
              return (
                <tr key={table.kind}>
                  <td className={cn(cell, 'px-5 font-medium')}>{t.data.tables[table.kind] ?? table.kind}</td>
                  <td className={cn(cell, missing && 'text-muted-foreground')}>
                    {table.source === 'xero' ? (
                      t.data.xero.name
                    ) : table.source === 'import' ? (
                      <>
                        {t.data.imported('')}
                        <Code className="text-[12.5px]">{table.file_name}</Code>
                      </>
                    ) : (
                      t.data.notProvided
                    )}
                  </td>
                  <td className={cn(cell, 'text-right font-mono')}>
                    {missing
                      ? '—'
                      : table.records_detail
                        ? t.data.currentPrior(table.records_detail.current, table.records_detail.prior)
                        : table.records.toLocaleString('en-AU')}
                  </td>
                  <td className={cn(cell, 'text-[13px]', !table.period_from && 'text-muted-foreground')}>
                    {table.period_from && table.period_to
                      ? `${formatDate(table.period_from)} – ${formatDate(table.period_to)}`
                      : '—'}
                  </td>
                  <td className={cell}>
                    <StatusPill tone={missing ? 'warn' : table.status === 'converting' ? 'accent' : 'ok'}>
                      {t.data.tableStatus[table.status]}
                    </StatusPill>
                  </td>
                  <td className={cn(cell, 'px-5 text-right')}>
                    {missing ? (
                      <button type="button" className="text-[13px] text-accent-foreground" onClick={onImport}>
                        {t.data.import}
                      </button>
                    ) : (
                      <Link
                        to="/engagements/$eid/data/$kind"
                        params={{ eid, kind: table.kind }}
                        search={{}}
                        className="text-[13px]"
                      >
                        {table.kind === 'accounts' ? t.data.viewEdit : t.data.view}
                      </Link>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function CheckIcon({ status, label }: { status: string; label: string }) {
  const className = 'mt-0.5 size-[18px] shrink-0'
  if (status === 'passed') return <CircleCheck className={cn(className, 'text-ok')} aria-label={label} role="img" />
  if (status === 'pending')
    return <Clock3 className={cn(className, 'text-muted-foreground')} aria-label={label} role="img" />
  return <AlertTriangle className={cn(className, 'text-warn-strong')} aria-label={label} role="img" />
}

function Checks({ eid, checks }: { eid: string; checks: DataCheck[] }) {
  const { t, lang } = useI18n()
  if (!checks.length) return null
  const passed = checks.filter((check) => check.status === 'passed').length
  return (
    <section aria-label={t.data.checks} className="flex flex-col gap-1 rounded-[14px] border bg-card px-5 py-[18px]">
      <div className="flex items-baseline justify-between gap-2 pb-2">
        <h2 className="m-0 font-semibold text-base">{t.data.checks}</h2>
        <span className="text-[12.5px] text-muted-foreground">
          {t.data.checkCounts(passed, checks.length - passed)}
        </span>
      </div>
      {checks.map((check) => {
        const detail = checkDetail(check, t, lang)
        return (
          <div key={check.id} className="flex gap-3 border-line-soft border-t py-2.5">
            <CheckIcon status={check.status} label={t.data.checkStatus[check.status]} />
            <div className="flex min-w-0 flex-1 flex-wrap justify-between gap-2">
              <span className="flex min-w-0 flex-[1_1_280px] flex-col">
                <span className="font-medium">{t.data.checkTitles[check.id] ?? check.id}</span>
                {detail ? <span className="text-[#4a5262] text-[13px]">{detail}</span> : null}
              </span>
              {check.id === 'mapping_complete' ? (
                <Link
                  to="/engagements/$eid/data/$kind"
                  params={{ eid, kind: 'accounts' }}
                  search={{}}
                  className="self-center text-[13px]"
                >
                  {t.data.reviewMapping}
                </Link>
              ) : check.request_id && check.status !== 'passed' ? (
                <Link
                  to="/engagements/$eid"
                  params={{ eid }}
                  search={{ focus: `request:${check.request_id}` }}
                  className="self-center text-[13px]"
                >
                  {t.data.handleInConcierge}
                </Link>
              ) : null}
            </div>
          </div>
        )
      })}
    </section>
  )
}

// 智能导入里暂存 (还没确认) 或确认时没勾选而未导入的文件不算开始处理, 进度全灰
const HELD = new Set(['staged', 'not_imported'])

function Progress({ doc }: { doc: DocumentRef }) {
  const { t } = useI18n()
  const manual = doc.status === 'needs_manual'
  const staged = HELD.has(doc.status)
  const complete = finished(doc)
  const done = staged
    ? 0
    : manual
      ? (FAILED_AT[doc.failed_step ?? 'classify'] ?? 1)
      : complete
        ? t.data.steps.length
        : (STEP_DONE[doc.status] ?? 1)
  const label = staged
    ? t.data.docStatus[doc.status]
    : manual
      ? t.data.docStatus.needs_manual
      : complete
        ? t.data.docStatus.extracted
        : t.data.steps[Math.min(done, 5)]
  return (
    <span className="flex flex-col gap-[5px]">
      <span className="flex gap-[3px]" aria-hidden="true">
        {t.data.steps.map((step, index) => (
          <span
            key={step}
            title={step}
            className={cn(
              'h-[5px] w-[22px] rounded-[3px]',
              index < done
                ? 'bg-ok'
                : index === done && manual
                  ? 'bg-warn-strong'
                  : index === done && !staged
                    ? 'bg-primary'
                    : 'bg-border',
            )}
          />
        ))}
      </span>
      <span
        className={cn(
          'font-medium text-[12.5px]',
          manual ? 'text-warn' : complete ? 'text-ok' : staged ? 'text-muted-foreground' : 'text-accent-foreground',
        )}
      >
        {label}
      </span>
    </span>
  )
}

function Documents({
  eid,
  docs,
  loading,
  onFiles,
  onChoose,
}: {
  eid: string
  docs: DocumentRef[]
  loading: boolean
  onFiles: (files: FileList) => void
  onChoose: () => void
}) {
  const { t, formatDate } = useI18n()
  const actorName = useActorName()
  const [typing, setTyping] = useState<DocumentRef | null>(null)
  const done = docs.filter(finished).length
  const manual = docs.filter((doc) => doc.status === 'needs_manual').length
  const staged = docs.filter((doc) => doc.status === 'staged').length
  const dropped = docs.filter((doc) => doc.status === 'not_imported').length
  const cell = 'border-line-soft border-b px-3 py-[11px]'
  return (
    <section aria-label={t.data.documents} className="overflow-hidden rounded-[14px] border bg-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-5 pt-4 pb-3">
        <h2 className="m-0 font-semibold text-base">{t.data.documents}</h2>
        {docs.length ? (
          <span className="text-[12.5px] text-muted-foreground">
            {t.data.docCounts(
              docs.length,
              done,
              docs.length - done - manual - staged - dropped,
              manual,
              staged,
              dropped,
            )}
          </span>
        ) : null}
      </div>
      {/* biome-ignore lint/a11y/noStaticElementInteractions: 拖放区只接收拖入的文件, 键盘用户用其中的选择文件按钮 */}
      <div
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault()
          onFiles(event.dataTransfer.files)
        }}
        className="mx-5 mb-3.5 flex flex-wrap items-center justify-center gap-2.5 rounded-xl border-[1.5px] border-input border-dashed bg-[#fafbfc] p-[18px] text-[#4a5262] text-[13.5px]"
      >
        <Upload className="size-5" aria-hidden="true" />
        <span>{t.data.dropHere}</span>
        <Button type="button" variant="outline" onClick={onChoose}>
          {t.data.chooseFiles}
        </Button>
        <span className="text-[12.5px] text-muted-foreground">{t.data.fileLimits}</span>
      </div>
      {docs.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[820px] border-collapse text-[13.5px]">
            <thead>
              <tr className="bg-[#fafbfc] text-left text-[12px] text-muted-foreground">
                <th scope="col" className="border-y px-5 py-2.5 font-semibold">
                  {t.data.docCols.file}
                </th>
                <th scope="col" className="border-y px-3 py-2.5 font-semibold">
                  {t.data.docCols.type}
                </th>
                <th scope="col" className="border-y px-3 py-2.5 font-semibold">
                  {t.data.docCols.progress}
                </th>
                <th scope="col" className="border-y px-3 py-2.5 font-semibold">
                  {t.data.docCols.reliability}
                </th>
                <th scope="col" className="border-y px-5 py-2.5 font-semibold">
                  {t.data.docCols.received}
                </th>
              </tr>
            </thead>
            <tbody>
              {docs.map((doc) => {
                const untyped = doc.status === 'needs_manual' && (doc.failed_step ?? 'classify') === 'classify'
                // 暂存与未导入的文件登记为 other, 类型在确认单上定
                const unsettled = untyped || HELD.has(doc.status)
                return (
                  <tr key={doc.id}>
                    <td className={cn(cell, 'px-5')}>
                      <span className="flex flex-col">
                        <Link
                          to="/engagements/$eid/data/documents/$docId"
                          params={{ eid, docId: doc.id }}
                          className="font-medium font-mono text-[13px]"
                        >
                          {doc.file_name}
                        </Link>
                        <span className="text-[12.5px] text-muted-foreground">
                          {t.evidencePage.pages(doc.page_count)}
                        </span>
                      </span>
                    </td>
                    <td className={cn(cell, unsettled && 'text-muted-foreground')}>
                      {unsettled ? t.data.unknownType : (t.data.docTypes[doc.type] ?? doc.type)}
                    </td>
                    <td className={cell}>
                      <span className="flex flex-wrap items-end gap-2">
                        <Progress doc={doc} />
                        {untyped ? (
                          <button
                            type="button"
                            className="text-[12.5px] text-accent-foreground"
                            onClick={() => setTyping(doc)}
                          >
                            {t.data.setType}
                          </button>
                        ) : null}
                      </span>
                    </td>
                    <td className={cell}>{t.data.reliabilities[doc.reliability] ?? doc.reliability}</td>
                    <td className={cn(cell, 'px-5 text-[#4a5262] text-[13px]')}>
                      {[
                        doc.uploaded_by
                          ? actorName(doc.uploaded_by)
                          : t.data.channels[
                              !doc.channel || doc.channel === 'auditor_upload' ? 'systemArchive' : doc.channel
                            ],
                        formatDate(doc.uploaded_at),
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="m-0 border-line-soft border-t px-5 py-4 text-[13px] text-muted-foreground">
          {loading ? t.app.loading : t.data.noDocuments}
        </p>
      )}
      <SetTypeDialog eid={eid} doc={typing} onClose={() => setTyping(null)} />
    </section>
  )
}

function SetTypeDialog({ eid, doc, onClose }: { eid: string; doc: DocumentRef | null; onClose: () => void }) {
  const { t } = useI18n()
  const setType = useSetDocumentType(eid)
  const [type, setTypeValue] = useState<DocumentType>('contract')
  const [reason, setReason] = useState('')
  return (
    <Dialog
      open={doc !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle className="font-mono text-base">{doc?.file_name}</DialogTitle>
        </DialogHeader>
        <label className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {t.data.docCols.type}
          <select
            className="h-10 rounded-[9px] border border-input bg-card px-2.5 font-normal text-[14px]"
            value={type}
            onChange={(event) => setTypeValue(event.target.value as DocumentType)}
          >
            {Object.entries(t.data.docTypes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="doc-type-reason">{t.data.typeReason}</label>
          <Textarea id="doc-type-reason" rows={2} value={reason} onChange={(event) => setReason(event.target.value)} />
        </div>
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.dataTable.cancel}
          </Button>
          <Button
            type="button"
            disabled={!doc || !reason.trim() || setType.isPending}
            onClick={() =>
              doc &&
              setType.mutate(
                { documentId: doc.id, type, reason: reason.trim() },
                {
                  onSuccess: () => {
                    setReason('')
                    onClose()
                  },
                  onError: (error) => toast.error(errorText(error, t)),
                },
              )
            }
          >
            {t.dataTable.save}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
