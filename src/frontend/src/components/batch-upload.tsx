import { cn } from 'cn'
import { Check, FileSpreadsheet, FileText, LoaderCircle } from 'lucide-react'
import { type ReactNode, useRef, useState } from 'react'
import { toast } from 'sonner'
import type { DocumentType, TableKind } from '@/client/types.gen'
import { StatusPill } from '@/components/chrome'
import { FailureTable, MappingTable, SELECT } from '@/components/import-dialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import {
  type AmountUnit,
  autoMap,
  blocked,
  type Draft,
  draftOf,
  issueText,
  itemNumber,
  missingFieldColumns,
  missingRuleColumns,
  periodText,
  planOf,
  planRules,
  RULE,
  ruleColumns,
} from '@/lib/batch-plan'
import {
  type BatchItem,
  type DateFormat,
  type ImportBatch,
  TABLE_KINDS,
  type TablePlan,
  type TableSchema,
  UPLOAD_ACCEPT,
  useBatch,
  useConfirmBatch,
  useEditBatchItem,
  useOpenBatches,
  useRetryBatch,
  useStartBatch,
  useTableSchemas,
} from '@/lib/data-api'
import { errorText, type ParseFailure, parseFailures } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { formatAud } from '@/lib/money'

// 智能导入 (后端需求 R277): "上传资料" 入口. 一批文件 (表格与其他文件混着) 交给数据管理智能体识别, 审计师看
// 确认单, 单项可调整, 点一次 "确认导入" 写入. 数据管理页与补交卡片共用; 补交卡片上的上传带 request_id

// 上传入口: choose 打开文件选择, send 直接上传 (拖放), open 打开已有批次的确认单; element 放在页面里
// (隐藏的文件框与确认单)
export function useBatchUpload(eid: string) {
  const { t } = useI18n()
  const start = useStartBatch(eid)
  const input = useRef<HTMLInputElement>(null)
  const requestId = useRef<string | undefined>(undefined)
  const [batchId, setBatchId] = useState<string | null>(null)

  const send = (files: FileList | File[] | null, request?: string) => {
    const list = Array.from(files ?? [])
    if (!list.length) return
    start.mutate(
      { files: list, requestId: request },
      { onSuccess: (batch) => setBatchId(batch.id), onError: (error) => toast.error(errorText(error, t)) },
    )
  }

  const choose = (request?: string) => {
    requestId.current = request
    input.current?.click()
  }

  const element = (
    <>
      <input
        ref={input}
        type="file"
        multiple
        accept={UPLOAD_ACCEPT}
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => {
          send(event.target.files, requestId.current)
          event.target.value = ''
        }}
      />
      <BatchDialog eid={eid} batchId={batchId} onClose={() => setBatchId(null)} />
    </>
  )
  return { choose, send, open: setBatchId, pending: start.isPending, element }
}

// 还没写入的批次 (关掉确认单后从这里回去)
export function OpenBatches({ eid, onOpen }: { eid: string; onOpen: (batchId: string) => void }) {
  const { t, formatDateTime } = useI18n()
  const batches = useOpenBatches(eid).data ?? []
  if (!batches.length) return null
  return (
    <section aria-label={t.batch.open.title} className="overflow-hidden rounded-[14px] border bg-card">
      <h2 className="m-0 border-line-soft border-b px-5 py-3.5 font-semibold text-base">{t.batch.open.title}</h2>
      <ul className="m-0 list-none p-0">
        {batches.map((batch) => (
          <li
            key={batch.id}
            className="flex flex-wrap items-center gap-3 border-line-soft border-b px-5 py-3 last:border-b-0"
          >
            <span className="min-w-0 flex-[1_1_260px] text-[13.5px]">
              {t.batch.open.files(batch.files.length, formatDateTime(batch.created_at))}
            </span>
            <StatusPill tone={batch.status === 'processing' ? 'accent' : batch.status === 'failed' ? 'risk' : 'warn'}>
              {t.batch.open.status[batch.status] ?? batch.status}
            </StatusPill>
            <Button type="button" variant="outline" onClick={() => onOpen(batch.id)}>
              {t.batch.open.review}
            </Button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function BatchDialog({ eid, batchId, onClose }: { eid: string; batchId: string | null; onClose: () => void }) {
  const { t } = useI18n()
  const batch = useBatch(eid, batchId).data
  const edit = useEditBatchItem(eid, batchId ?? '')
  const confirm = useConfirmBatch(eid, batchId ?? '')
  const retry = useRetryBatch(eid, batchId ?? '')
  const [adjusting, setAdjusting] = useState<string | null>(null)
  const [failures, setFailures] = useState<ParseFailure[] | null>(null)
  const [discarding, setDiscarding] = useState(false) // 一项都没勾时点了 "都不导入", 等再确认一次
  const items = batch?.items ?? []
  const selected = items.filter((item) => item.selected)
  const ready = batch?.status === 'ready'
  const done = batch?.files.filter((file) => file.done).length ?? 0
  // 还没识别完的文件: 上传后识别中, 或改了类型后重新识别 (这时确认单上已有它的项, 标 "正在重新识别")
  const pending = new Set(batch?.files.filter((file) => !file.done).map((file) => file.document_id))
  const current = items.find((item) => item.id === adjusting) ?? null

  const close = () => {
    setFailures(null)
    setAdjusting(null)
    setDiscarding(false)
    onClose()
  }

  const toggle = (item: BatchItem, next: boolean) =>
    edit.mutate({ itemId: item.id, body: { selected: next } }, { onError: (error) => toast.error(errorText(error, t)) })

  const submit = () => {
    setFailures(null)
    confirm.mutate(undefined, {
      onSuccess: (result) => {
        const tables = selected.filter((item) => item.item_kind === 'table').length
        const dropped = result.files.filter((file) => file.not_imported).length
        const documents = selected.length - tables
        toast.success(tables || documents ? t.batch.done(tables, documents, dropped) : t.batch.doneNothing(dropped))
        close()
      },
      // 解析失败在确认单里逐处列出 (多半是确认前方案或文件有变), 其余提示后留在确认单
      onError: (error) => {
        const found = parseFailures(error)
        if (found) setFailures(found)
        toast.error(errorText(error, t))
      },
    })
  }

  return (
    <Dialog
      open={batchId !== null}
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent className="max-h-[92vh] gap-0 overflow-y-auto p-0 sm:max-w-[1200px]">
        <DialogHeader className="gap-1 border-line-soft border-b px-6 pt-5 pb-4">
          <DialogTitle className="font-semibold text-lg">{t.batch.title}</DialogTitle>
          <DialogDescription>
            {!batch
              ? t.app.loading
              : batch.status === 'processing'
                ? t.batch.processing(done, batch.files.length)
                : batch.status === 'failed'
                  ? t.batch.stopped(done, batch.files.length)
                  : t.batch.intro(items.length, selected.length)}
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4 px-6 py-4">
          {batch?.status === 'failed' ? (
            <div
              role="alert"
              className="flex flex-wrap items-center justify-between gap-3 rounded-[10px] bg-risk-soft px-3.5 py-3"
            >
              <span className="min-w-0 flex-[1_1_320px] text-[13px] text-risk">
                {t.batch.failed(batch.error ?? '')}
              </span>
              <Button
                type="button"
                variant="outline"
                disabled={retry.isPending}
                onClick={() => retry.mutate(undefined, { onError: (error) => toast.error(errorText(error, t)) })}
              >
                {retry.isPending ? t.batch.retrying : t.batch.retry}
              </Button>
            </div>
          ) : null}
          {batch?.status === 'processing' && !items.length ? (
            <ProcessingFiles batch={batch} />
          ) : items.length ? (
            <BatchTable
              items={items}
              pending={batch?.status === 'processing' ? pending : new Set()}
              disabled={!ready || edit.isPending || confirm.isPending}
              onToggle={toggle}
              onAdjust={(item) => setAdjusting(item.id)}
            />
          ) : null}
          {failures?.length ? (
            <div role="alert" className="flex flex-col gap-2 rounded-[10px] bg-risk-soft px-3.5 py-3">
              <span className="font-semibold text-[13px] text-risk">{t.batch.confirmFailures}</span>
              <FailureTable failures={failures} />
            </div>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2.5 border-line-soft border-t bg-[#fafbfc] px-6 py-3.5">
          {discarding && !selected.length ? (
            <>
              <span role="alert" className="min-w-0 flex-[1_1_320px] text-[13px] text-risk">
                {t.batch.discardWarning}
              </span>
              <Button type="button" variant="outline" size="lg" onClick={() => setDiscarding(false)}>
                {t.dataTable.cancel}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="border-risk text-risk"
                disabled={!ready || confirm.isPending || edit.isPending}
                onClick={submit}
              >
                {confirm.isPending ? t.batch.confirming : t.batch.discardConfirm}
              </Button>
            </>
          ) : (
            <>
              <Button type="button" variant="outline" size="lg" onClick={close}>
                {t.batch.later}
              </Button>
              {selected.length ? (
                <Button
                  type="button"
                  size="lg"
                  disabled={!ready || confirm.isPending || edit.isPending}
                  onClick={submit}
                >
                  {confirm.isPending ? t.batch.confirming : t.batch.confirm(selected.length)}
                </Button>
              ) : (
                // 一项都没勾: 次要样式, 点了先确认再关 (文件都记为未导入, 不能撤回)
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  disabled={!ready || confirm.isPending || edit.isPending}
                  onClick={() => setDiscarding(true)}
                >
                  {t.batch.closeWithout}
                </Button>
              )}
            </>
          )}
        </div>
        {batchId && current?.item_kind === 'table' ? (
          <AdjustTableDialog eid={eid} batchId={batchId} item={current} onClose={() => setAdjusting(null)} />
        ) : null}
        {batchId && current?.item_kind === 'document' ? (
          <AdjustDocumentDialog eid={eid} batchId={batchId} item={current} onClose={() => setAdjusting(null)} />
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function ProcessingFiles({ batch }: { batch: ImportBatch }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[13.5px]">
      {batch.files.map((file) => (
        <li key={file.document_id} className="flex items-center gap-2">
          {file.done ? (
            <Check className="size-4 text-ok" aria-hidden="true" />
          ) : (
            <LoaderCircle className="size-4 animate-spin text-accent-foreground" aria-hidden="true" />
          )}
          <span className="font-mono text-[13px]">{file.name}</span>
        </li>
      ))}
    </ul>
  )
}

function BatchTable({
  items,
  pending,
  disabled,
  onToggle,
  onAdjust,
}: {
  items: BatchItem[]
  pending: Set<string>
  disabled: boolean
  onToggle: (item: BatchItem, next: boolean) => void
  onAdjust: (item: BatchItem) => void
}) {
  const { t, lang, formatDate } = useI18n()
  const head = 'border-y px-3 py-2.5 font-semibold'
  const cell = 'border-line-soft border-b px-3 py-2.5 align-top'
  return (
    <div className="overflow-x-auto rounded-[10px] border">
      <table className="w-full min-w-[1040px] border-collapse text-[13.5px]">
        <thead>
          <tr className="bg-[#fafbfc] text-left text-[12px] text-muted-foreground">
            <th scope="col" className={cn(head, 'w-12')}>
              {t.batch.cols.select}
            </th>
            <th scope="col" className={head}>
              {t.batch.cols.file}
            </th>
            <th scope="col" className={head}>
              {t.batch.cols.recognised}
            </th>
            <th scope="col" className={cn(head, 'text-right')}>
              {t.batch.cols.records}
            </th>
            <th scope="col" className={head}>
              {t.batch.cols.companyPeriod}
            </th>
            <th scope="col" className={head}>
              {t.batch.cols.confidence}
            </th>
            <th scope="col" className={cn(head, 'w-[26%]')}>
              {t.batch.cols.issues}
            </th>
            <th scope="col" className={head}>
              <span className="sr-only">{t.batch.cols.actions}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const issues = item.issues ?? []
            const flagged = issues.length > 0
            const name = item.item_kind === 'table' && item.sheet ? `${item.file_name} · ${item.sheet}` : item.file_name
            const cannot = blocked(item)
            const registered = issues.some((issue) => issue.code === 'already_registered')
            return (
              <tr key={item.id} className={cn(flagged && 'bg-risk-soft/60')}>
                <td className={cell}>
                  <input
                    type="checkbox"
                    className="mt-0.5 size-4 accent-primary"
                    aria-label={t.batch.selectItem(name)}
                    title={cannot && !item.selected ? t.batch.cannotSelect : undefined}
                    checked={item.selected ?? false}
                    disabled={disabled || (cannot && !item.selected)}
                    onChange={(event) => onToggle(item, event.target.checked)}
                  />
                </td>
                <td className={cell}>
                  <span className="flex items-start gap-2">
                    {/* 项号: 问题 "同一批里同一张表出现两次" 按它指向其他项 */}
                    <span className="w-5 shrink-0 text-right font-mono text-[12.5px] text-muted-foreground">
                      {itemNumber(item.id)}
                    </span>
                    {item.item_kind === 'table' ? (
                      <FileSpreadsheet className="mt-0.5 size-4 shrink-0 text-ok" aria-hidden="true" />
                    ) : (
                      <FileText className="mt-0.5 size-4 shrink-0 text-standard" aria-hidden="true" />
                    )}
                    <span className="flex min-w-0 flex-col">
                      <span className="break-all font-medium font-mono text-[13px]">{item.file_name}</span>
                      {item.item_kind === 'table' && item.sheet ? (
                        <span className="text-[12.5px] text-muted-foreground">{item.sheet}</span>
                      ) : null}
                    </span>
                  </span>
                </td>
                <td className={cell}>
                  {pending.has(item.document_id) ? (
                    <span className="flex flex-col gap-1">
                      <Recognised item={item} />
                      <span className="flex items-center gap-1.5 text-[12.5px] text-accent-foreground">
                        <LoaderCircle className="size-3.5 animate-spin" aria-hidden="true" />
                        {t.batch.itemProcessing}
                      </span>
                    </span>
                  ) : (
                    <Recognised item={item} />
                  )}
                </td>
                <td className={cn(cell, 'text-right font-mono')}>
                  {item.item_kind === 'table' && item.records != null ? t.batch.rows(item.records) : '—'}
                </td>
                <td className={cn(cell, 'text-[13px]')}>
                  {item.reading ? (
                    <span className="flex flex-col">
                      <span>{item.reading.company ?? '—'}</span>
                      <span className="text-muted-foreground">
                        {periodText(item.reading.period_start, item.reading.period_end, t, formatDate)}
                      </span>
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
                <td className={cell}>
                  {item.reading ? (
                    <span className="flex flex-col items-start gap-1" title={item.reading.reason}>
                      <StatusPill tone={item.reading.confidence === 'high' ? 'ok' : 'warn'}>
                        {t.batch.confidence[item.reading.confidence]}
                      </StatusPill>
                      {item.reading.method === 'column_names' ? (
                        <span className="text-[12px] text-muted-foreground">{t.batch.columnNames}</span>
                      ) : null}
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
                <td className={cell}>
                  {flagged ? (
                    <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13px] text-risk">
                      {issues.map((issue) => (
                        <li key={issue.code}>{issueText(issue, t, lang, formatDate)}</li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                  {item.failures?.length ? (
                    <details className="mt-1.5 text-[13px]">
                      <summary className="cursor-pointer text-accent-foreground">{t.batch.showFailures}</summary>
                      <FailureTable failures={item.failures} />
                    </details>
                  ) : null}
                </td>
                <td className={cn(cell, 'text-right')}>
                  {registered ? null : (
                    <Button type="button" variant="ghost" disabled={disabled} onClick={() => onAdjust(item)}>
                      {t.batch.adjust}
                    </Button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// 识别为: 表格为哪张表 (或不是账表); 文件为类型, 合同带对方与条款数, 回函带银行与余额, 另列分类理由
function Recognised({ item }: { item: BatchItem }) {
  const { t } = useI18n()
  if (item.item_kind === 'table') {
    const kind = item.plan?.kind
    return kind ? (
      <span className="font-medium">{t.data.tables[kind] ?? kind}</span>
    ) : (
      <span className="text-muted-foreground">{t.batch.notATable}</span>
    )
  }
  let detail: ReactNode = null
  if (item.contract) {
    detail = [
      t.batch.contract(item.contract.counterparty, item.contract.terms.length),
      item.contract.amount_cents != null ? formatAud(item.contract.amount_cents) : null,
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (item.reply) {
    detail = t.batch.reply(
      String(item.reply.confirming_party ?? ''),
      formatAud(Number(item.reply.confirmed_balance_cents)),
    )
  }
  return (
    <span className="flex flex-col gap-0.5">
      <span className={cn('font-medium', !item.document_type && 'text-muted-foreground')}>
        {item.document_type ? (t.data.docTypes[item.document_type] ?? item.document_type) : t.batch.unknownType}
      </span>
      {detail ? <span className="text-[12.5px] text-[#4a5262]">{detail}</span> : null}
      {item.type_reason ? (
        <span className="line-clamp-2 text-[12px] text-muted-foreground" title={item.type_reason}>
          {item.type_reason}
        </span>
      ) : null}
    </span>
  )
}

// ── 调整表格项: 沿用账表导入的列对应界面, 预填规整方案 ──

function AdjustTableDialog({
  eid,
  batchId,
  item,
  onClose,
}: {
  eid: string
  batchId: string
  item: BatchItem
  onClose: () => void
}) {
  const { t } = useI18n()
  const schemas = useTableSchemas(true).data ?? []
  const edit = useEditBatchItem(eid, batchId)
  const [draft, setDraft] = useState<Draft>(() => draftOf(item, t))
  const plan: TablePlan = item.plan ?? { kind: null }
  const schema: TableSchema | undefined = schemas.find((entry) => entry.kind === draft.kind)
  const columns = item.columns ?? []
  const same = draft.kind === plan.kind
  const rules = same ? planRules(plan, t, Boolean(draft.groupTitle)) : {}
  const reread = draft.headerRow !== (plan.header_row ?? 1)
  const onError = (error: unknown) => toast.error(errorText(error, t))

  const changeKind = (next: TableKind | '') =>
    setDraft((current) => ({
      ...current,
      kind: next,
      mapping:
        next === plan.kind
          ? draftOf(item, t).mapping
          : next
            ? autoMap(
                schemas.find((entry) => entry.kind === next),
                columns,
                {},
              )
            : {},
    }))

  // 跳过规则与分组标题: 删除或改选列; 删了分组标题时取它的字段不再沿用
  const changeRules = (next: Partial<Pick<Draft, 'skip' | 'groupTitle'>>) =>
    setDraft((current) => {
      const mapping = { ...current.mapping }
      if ('groupTitle' in next && !next.groupTitle) {
        for (const [name, source] of Object.entries(plan.fields ?? {})) {
          if (source.group && mapping[name] === RULE) mapping[name] = null
        }
      }
      return { ...current, ...next, mapping }
    })

  // 试算表按期分列时期间与余额一起沿用或一起放弃
  const changeField = (field: string, value: string | null) =>
    setDraft((current) => {
      const mapping = { ...current.mapping, [field]: value }
      if ((field === 'period' || field === 'balance_cents') && same && plan.periods?.length) {
        const other = field === 'period' ? 'balance_cents' : 'period'
        if (value === RULE) mapping[other] = RULE
        else if (current.mapping[other] === RULE) mapping[other] = null
      }
      return { ...current, mapping }
    })

  const save = (keepOpen: boolean) =>
    edit.mutate(
      { itemId: item.id, body: { plan: planOf(plan, draft) } },
      {
        onSuccess: (batch) => {
          const next = batch.items?.find((entry) => entry.id === item.id)
          if (keepOpen && next) setDraft(draftOf(next, t))
          else onClose()
        },
        onError,
      },
    )

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="max-h-[90vh] gap-0 overflow-y-auto p-0 sm:max-w-[820px]">
        <DialogHeader className="gap-1 border-line-soft border-b px-6 pt-5 pb-4">
          <DialogTitle className="font-semibold text-base">
            {t.batch.table.title(item.sheet ? `${item.file_name} · ${item.sheet}` : item.file_name)}
          </DialogTitle>
          {item.reading ? (
            <DialogDescription>
              {t.batch.table.reason}: {item.reading.reason}
            </DialogDescription>
          ) : null}
        </DialogHeader>
        <div className="flex flex-col gap-[18px] px-6 py-[18px]">
          <div className="flex flex-wrap gap-4">
            <label className="flex flex-[1_1_180px] flex-col gap-1.5 font-semibold text-[13px]">
              {t.batch.table.kind}
              <select
                className={SELECT}
                value={draft.kind}
                onChange={(event) => changeKind(event.target.value as TableKind | '')}
              >
                <option value="">{t.batch.table.notATable}</option>
                {TABLE_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {t.data.tables[kind]}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-[0_1_120px] flex-col gap-1.5 font-semibold text-[13px]">
              {t.batch.table.headerRow}
              <input
                type="number"
                min={1}
                className={SELECT}
                value={draft.headerRow || ''}
                onChange={(event) =>
                  // 清空或写了非正整数时记 0, 读列名与保存都不可点, 直到写好
                  setDraft((current) => ({
                    ...current,
                    headerRow: Math.max(0, Math.floor(Number(event.target.value)) || 0),
                  }))
                }
              />
            </label>
            <label className="flex flex-[1_1_180px] flex-col gap-1.5 font-semibold text-[13px]">
              {t.importDialog.dateFormat}
              <select
                className={SELECT}
                value={draft.dateFormat}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, dateFormat: event.target.value as DateFormat }))
                }
              >
                {(['dmy', 'ymd', 'mdy'] as const).map((format) => (
                  <option key={format} value={format}>
                    {t.importDialog.dateFormats[format]}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-[1_1_160px] flex-col gap-1.5 font-semibold text-[13px]">
              {t.batch.table.amountUnit}
              <select
                className={SELECT}
                value={draft.amountUnit}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, amountUnit: event.target.value as AmountUnit | '' }))
                }
              >
                <option value="">{t.batch.table.amountUnits.auto}</option>
                <option value="dollars">{t.batch.table.amountUnits.dollars}</option>
                <option value="cents">{t.batch.table.amountUnits.cents}</option>
              </select>
            </label>
          </div>

          {reread ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-[10px] bg-warn-soft px-3.5 py-3 text-[13px] text-warn">
              <span>{t.batch.table.rereadHint}</span>
              <Button
                type="button"
                variant="outline"
                disabled={edit.isPending || draft.headerRow < 1}
                onClick={() => save(true)}
              >
                {t.batch.table.reread(draft.headerRow)}
              </Button>
            </div>
          ) : draft.kind ? (
            <MappingTable
              fields={schema?.fields ?? []}
              columns={columns}
              sample={item.sample ?? {}}
              mapping={draft.mapping}
              onChange={changeField}
              rules={rules}
              ruleColumns={ruleColumns(plan, draft)}
              sampleLabel={t.batch.table.sample}
              missing={missingFieldColumns(plan, draft, columns)}
              missingText={t.batch.table.missingField}
            />
          ) : null}

          <RowRules draft={draft} columns={columns} dedupe={Boolean(plan.dedupe)} onChange={changeRules} />
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2.5 border-line-soft border-t bg-[#fafbfc] px-6 py-3.5">
          <Button type="button" variant="outline" size="lg" onClick={onClose}>
            {t.dataTable.cancel}
          </Button>
          <Button type="button" size="lg" disabled={reread || edit.isPending} onClick={() => save(false)}>
            {t.batch.table.save}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// 方案里按行的规则: 跳过的行与分组标题可删, 也可把列改选为当前表头里的列 (不是完整的规则编辑器, 正则照留);
// 列不在当前表头里的标红 (方案套不上的原因之一). 去重只读
function RowRules({
  draft,
  columns,
  dedupe,
  onChange,
}: {
  draft: Draft
  columns: string[]
  dedupe: boolean
  onChange: (next: Partial<Pick<Draft, 'skip' | 'groupTitle'>>) => void
}) {
  const { t } = useI18n()
  const missing = missingRuleColumns(draft, columns)
  if (!draft.skip.length && !draft.groupTitle && !dedupe) return null
  const columnSelect = (value: string, onPick: (column: string) => void) => (
    <select
      aria-label={t.batch.table.ruleColumn}
      className={cn(SELECT, 'h-9 w-auto min-w-[160px]', missing.has(value) && 'border-risk text-risk')}
      value={value}
      onChange={(event) => onPick(event.target.value)}
    >
      {columns.includes(value) ? null : <option value={value}>{value}</option>}
      {columns.map((column) => (
        <option key={column} value={column}>
          {column}
        </option>
      ))}
    </select>
  )
  const problem = (column: string) =>
    missing.has(column) ? <span className="w-full text-[12.5px] text-risk">{t.batch.table.missingColumn}</span> : null
  return (
    <div className="flex flex-col gap-1.5">
      <span className="font-semibold text-[13px]">{t.batch.table.rules}</span>
      <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[13px]">
        {draft.skip.map(({ id, rule }) => (
          <li key={id} className="flex flex-wrap items-center gap-2">
            {columnSelect(rule.column, (column) =>
              onChange({
                skip: draft.skip.map((other) => (other.id === id ? { id, rule: { ...rule, column } } : other)),
              }),
            )}
            <span className="break-all text-[#4a5262]">{t.batch.table.skipMatches(rule.pattern)}</span>
            <Button
              type="button"
              variant="ghost"
              onClick={() => onChange({ skip: draft.skip.filter((other) => other.id !== id) })}
            >
              {t.batch.table.removeRule}
            </Button>
            {problem(rule.column)}
          </li>
        ))}
        {draft.groupTitle ? (
          <li className="flex flex-wrap items-center gap-2">
            {columnSelect(draft.groupTitle.column, (column) =>
              onChange({ groupTitle: draft.groupTitle ? { ...draft.groupTitle, column } : null }),
            )}
            <span className="break-all text-[#4a5262]">{t.batch.table.groupMatches(draft.groupTitle.pattern)}</span>
            <Button type="button" variant="ghost" onClick={() => onChange({ groupTitle: null })}>
              {t.batch.table.removeRule}
            </Button>
            {problem(draft.groupTitle.column)}
          </li>
        ) : null}
        {dedupe ? <li className="text-[#4a5262]">{t.batch.table.dedupe}</li> : null}
      </ul>
    </div>
  )
}

// ── 调整文件项: 改类型, 须写原因 ──

function AdjustDocumentDialog({
  eid,
  batchId,
  item,
  onClose,
}: {
  eid: string
  batchId: string
  item: BatchItem
  onClose: () => void
}) {
  const { t } = useI18n()
  const edit = useEditBatchItem(eid, batchId)
  const [type, setType] = useState<DocumentType>(item.document_type ?? 'contract')
  const [reason, setReason] = useState('')
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle className="break-all font-semibold text-base">
            {t.batch.document.title(item.file_name)}
          </DialogTitle>
        </DialogHeader>
        <label className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {t.batch.document.type}
          <select className={SELECT} value={type} onChange={(event) => setType(event.target.value as DocumentType)}>
            {Object.entries(t.data.docTypes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="batch-type-reason">{t.batch.document.reason}</label>
          <Textarea
            id="batch-type-reason"
            rows={2}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.dataTable.cancel}
          </Button>
          <Button
            type="button"
            disabled={!reason.trim() || edit.isPending}
            onClick={() =>
              edit.mutate(
                { itemId: item.id, body: { document_type: type, reason: reason.trim() } },
                { onSuccess: onClose, onError: (error) => toast.error(errorText(error, t)) },
              )
            }
          >
            {t.batch.document.save}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
