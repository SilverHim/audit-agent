import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { AlertTriangle, CircleCheck, Clock3, FolderOpen, Link2, RefreshCw, Table2, Upload } from 'lucide-react'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { evidenceListDocumentsOptions } from '@/client/@tanstack/react-query.gen'
import type { DocumentRef, DocumentType } from '@/client/types.gen'
import { Code, PageHeader, StatusPill } from '@/components/chrome'
import { ImportDialog } from '@/components/import-dialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { useActorName } from '@/lib/actor'
import {
  type DataCheck,
  type DataSummary,
  type TableKind,
  type TableSummary,
  useDataReady,
  useDataSummary,
  useSetDocumentType,
  useUploadDocuments,
  useXeroConnect,
  useXeroDisconnect,
  useXeroSync,
} from '@/lib/data-api'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'
import { NotAvailable } from '@/lib/new-api'
import { useEngagement } from '@/lib/use-engagement'
import type { Messages } from '@/locales/en'

export const Route = createFileRoute('/engagements/$eid/data/')({
  validateSearch: (search: Record<string, unknown>): { xero?: string } =>
    typeof search.xero === 'string' ? { xero: search.xero } : {},
  component: DataPage,
})

const DOC_ACCEPT = '.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg'
const STEP_DONE: Record<string, number> = { received: 1, classified: 2, extracted: 6 }
const FAILED_AT: Record<string, number> = { classify: 1, read: 2, extract: 3, link: 4 }
// 合同与回函分类后还要抽取, 其他类型分类后即处理完 (后端停在 classified)
const EXTRACTED_TYPES = new Set<DocumentType>(['contract', 'confirmation'])

function finished(doc: DocumentRef): boolean {
  return doc.status === 'extracted' || (doc.status === 'classified' && !EXTRACTED_TYPES.has(doc.type))
}

// Xero 接口 (R12) 还没合入时为 NotAvailable
function failMessage(error: unknown, t: Messages): string {
  return error instanceof NotAvailable ? t.importDialog.unavailable : errorText(error, t)
}

function DataPage() {
  const { eid } = Route.useParams()
  const { xero } = Route.useSearch()
  const navigate = useNavigate()
  const { t, formatDate } = useI18n()
  const overview = useEngagement(eid).data
  const summary = useDataSummary(eid).data ?? null
  // 数据管理智能体处理文件时跟着概览一起刷新处理进度
  const documents = useQuery({
    ...evidenceListDocumentsOptions({ path: { eid } }),
    refetchInterval: summary?.processing ? 3000 : false,
  })
  const upload = useUploadDocuments(eid)
  const ready = useDataReady(eid)
  const fileInput = useRef<HTMLInputElement>(null)
  const [importing, setImporting] = useState<{ kind?: TableKind } | null>(null)

  // Xero 授权回调带回 ?xero=connected 或 ?xero=error, 提示一次后清掉
  useEffect(() => {
    if (!xero) return
    if (xero === 'connected') toast.success(t.data.xero.status.connected)
    else toast.error(t.data.xero.failed)
    void navigate({ to: '/engagements/$eid/data', params: { eid }, search: {}, replace: true })
  }, [xero, eid, navigate, t])

  const send = (files: FileList | File[] | null) => {
    const list = Array.from(files ?? [])
    if (!list.length) return
    upload.mutate(
      { files: list },
      {
        onSuccess: (docs) => toast.success(t.data.uploaded(docs.length)),
        onError: (error) => toast.error(failMessage(error, t)),
      },
    )
    if (fileInput.current) fileInput.current.value = ''
  }

  const docs = documents.data ?? []
  const importedTables = summary?.tables.filter((table) => table.source === 'import').length ?? 0

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
            <Button type="button" variant="outline" size="lg" onClick={() => setImporting({})}>
              <Table2 className="size-4" aria-hidden="true" />
              {t.data.importLedger}
            </Button>
            <Button type="button" size="lg" disabled={upload.isPending} onClick={() => fileInput.current?.click()}>
              <Upload className="size-4" aria-hidden="true" />
              {t.data.uploadDocuments}
            </Button>
          </>
        }
      />
      <input
        ref={fileInput}
        type="file"
        multiple
        accept={DOC_ACCEPT}
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => send(event.target.files)}
      />

      <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-[22px] px-4 py-6 sm:px-7">
        {overview?.awaiting_data ? (
          <section className="flex flex-wrap items-center justify-between gap-3 rounded-[14px] border-[1.5px] border-primary bg-card px-5 py-4">
            <span className="font-semibold text-[15px]">{t.data.awaiting}</span>
            <Button
              type="button"
              size="lg"
              disabled={ready.isPending}
              onClick={() => ready.mutate(undefined, { onError: (error) => toast.error(failMessage(error, t)) })}
            >
              {t.data.ready}
            </Button>
          </section>
        ) : null}

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
                <Button type="button" variant="outline" onClick={() => setImporting({})}>
                  {t.data.importLedger}
                </Button>
                <Button type="button" variant="outline" onClick={() => fileInput.current?.click()}>
                  {t.data.uploadDocuments}
                </Button>
              </div>
            </SourceCard>
          </div>
        </section>

        {summary ? (
          <>
            <FinancialTables eid={eid} tables={summary.tables} onImport={(kind) => setImporting({ kind })} />
            <Checks eid={eid} checks={summary.checks} />
          </>
        ) : null}

        <Documents
          eid={eid}
          docs={docs}
          loading={documents.isPending}
          onFiles={send}
          onChoose={() => fileInput.current?.click()}
        />
      </div>

      <ImportDialog
        eid={eid}
        open={importing !== null}
        kind={importing?.kind}
        onOpenChange={(open) => {
          if (!open) setImporting(null)
        }}
      />
    </>
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

function XeroCard({ eid, summary }: { eid: string; summary: DataSummary | null }) {
  const { t, lang, formatDateTime } = useI18n()
  const connect = useXeroConnect(eid)
  const sync = useXeroSync(eid)
  const disconnect = useXeroDisconnect(eid)
  const [confirming, setConfirming] = useState(false)
  const xero = summary?.sources.xero ?? { status: 'not_connected', organisation: null, last_synced_at: null, scope: [] }
  const connected = xero.status === 'connected'
  const syncing = summary?.tables.some((table) => table.source === 'xero' && table.status === 'converting') ?? false
  const onError = (error: unknown) => toast.error(failMessage(error, t))
  return (
    <SourceCard
      icon={<Link2 className="size-[18px]" aria-hidden="true" />}
      iconClass="bg-accent text-accent-foreground"
      title={t.data.xero.name}
      kind={t.data.xero.kind}
      badge={
        <StatusPill tone={connected ? 'ok' : xero.status === 'expired' ? 'warn' : 'neutral'}>
          {t.data.xero.status[xero.status]}
        </StatusPill>
      }
    >
      {connected ? (
        <dl className="m-0 grid grid-cols-[72px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px]">
          <dt className="text-muted-foreground">{t.data.xero.organisation}</dt>
          <dd className="m-0">{xero.organisation}</dd>
          <dt className="text-muted-foreground">{t.data.xero.lastSynced}</dt>
          <dd className="m-0">{xero.last_synced_at ? formatDateTime(xero.last_synced_at) : '—'}</dd>
          <dt className="text-muted-foreground">{t.data.xero.scope}</dt>
          <dd className="m-0">
            {(xero.scope ?? []).map((kind) => t.data.tables[kind]).join(lang === 'zh' ? '、' : ', ')}
          </dd>
        </dl>
      ) : null}
      <div className="mt-auto flex flex-wrap gap-2 pt-1">
        {connected ? (
          <>
            <Button
              type="button"
              variant="outline"
              disabled={syncing || sync.isPending}
              onClick={() => sync.mutate(undefined, { onError })}
            >
              <RefreshCw className={cn('size-3.5', syncing && 'animate-spin')} aria-hidden="true" />
              {syncing ? t.data.xero.syncing : t.data.xero.sync}
            </Button>
            {confirming ? (
              <Button
                type="button"
                variant="outline"
                className="border-risk text-risk"
                disabled={disconnect.isPending}
                onClick={() => disconnect.mutate(undefined, { onError, onSettled: () => setConfirming(false) })}
              >
                {t.data.xero.confirmDisconnect}
              </Button>
            ) : (
              <Button type="button" variant="ghost" onClick={() => setConfirming(true)}>
                {t.data.xero.disconnect}
              </Button>
            )}
          </>
        ) : (
          <Button type="button" disabled={connect.isPending} onClick={() => connect.mutate(undefined, { onError })}>
            {xero.status === 'expired' ? t.data.xero.reconnect : t.data.xero.connect}
          </Button>
        )}
      </div>
    </SourceCard>
  )
}

function FinancialTables({
  eid,
  tables,
  onImport,
}: {
  eid: string
  tables: TableSummary[]
  onImport: (kind: TableKind) => void
}) {
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
                      <button
                        type="button"
                        className="text-[13px] text-accent-foreground"
                        onClick={() => onImport(table.kind)}
                      >
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

function checkDetail(check: DataCheck, t: Messages, lang: 'zh' | 'en'): string {
  const p = check.params ?? {}
  const num = (value: unknown) => Number(value ?? 0)
  switch (check.id) {
    case 'debits_equal_credits':
      return t.data.checkDetail.debits(formatAud(num(p.debit_cents)), formatAud(num(p.credit_cents)))
    case 'journals_agree_tb':
      return t.data.checkDetail.journals(num(p.accounts), Array.isArray(p.differences) ? p.differences.length : 0)
    case 'count_agrees_source':
      return t.data.checkDetail.count(
        String(p.source ?? ''),
        num(p.expected).toLocaleString('en-AU'),
        num(p.received).toLocaleString('en-AU'),
      )
    case 'mapping_complete':
      return t.data.checkDetail.mapping(num(p.mapped), num(p.total))
    case 'preparer_approver_available': {
      if (check.status === 'passed') return ''
      const fields = (Array.isArray(p.missing_fields) ? p.missing_fields : []).map(
        (field) => t.data.fieldNames[String(field)] ?? String(field),
      )
      return t.data.checkDetail.fields(fields.join(lang === 'zh' ? '与' : ' and '), String(p.source ?? 'Xero'))
    }
    case 'tb_agrees_statements':
      if (check.status === 'missing_data')
        return num(p.statements) ? t.data.checkDetail.statementsNoTb : t.data.checkDetail.statementsMissing
      // 报表数字的勾稽归 FIN-12, 还没执行时为 pending
      if (check.status === 'pending') return t.data.checkDetail.statementsPending(num(p.statements))
      return p.difference_cents ? t.data.checkDetail.statementsDiff(formatAud(num(p.difference_cents))) : ''
    default:
      return ''
  }
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

function Progress({ doc }: { doc: DocumentRef }) {
  const { t } = useI18n()
  const manual = doc.status === 'needs_manual'
  const complete = finished(doc)
  const done = manual
    ? (FAILED_AT[doc.failed_step ?? 'classify'] ?? 1)
    : complete
      ? t.data.steps.length
      : (STEP_DONE[doc.status] ?? 1)
  const label = manual
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
                  : index === done
                    ? 'bg-primary'
                    : 'bg-border',
            )}
          />
        ))}
      </span>
      <span
        className={cn(
          'font-medium text-[12.5px]',
          manual ? 'text-warn' : complete ? 'text-ok' : 'text-accent-foreground',
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
  const cell = 'border-line-soft border-b px-3 py-[11px]'
  return (
    <section aria-label={t.data.documents} className="overflow-hidden rounded-[14px] border bg-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-5 pt-4 pb-3">
        <h2 className="m-0 font-semibold text-base">{t.data.documents}</h2>
        {docs.length ? (
          <span className="text-[12.5px] text-muted-foreground">
            {t.data.docCounts(docs.length, done, docs.length - done - manual, manual)}
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
                    <td className={cn(cell, untyped && 'text-muted-foreground')}>
                      {untyped ? t.data.unknownType : (t.data.docTypes[doc.type] ?? doc.type)}
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
                      {[actorName(doc.uploaded_by), formatDate(doc.uploaded_at)].filter(Boolean).join(' · ')}
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
                  onError: (error) => toast.error(failMessage(error, t)),
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
