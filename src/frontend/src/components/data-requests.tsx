import { Link } from '@tanstack/react-router'
import { Database, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { StatusPill } from '@/components/chrome'
import { DataReadyButton } from '@/components/data-ready'
import { ImportDialog } from '@/components/import-dialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import { useActorName } from '@/lib/actor'
import { type DataRequest, requestTitle, useMarkUnavailable, useUploadDocuments } from '@/lib/data-api'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { procedureName } from '@/lib/review'
import type { Messages } from '@/locales/en'

const DOC_ACCEPT = '.pdf,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg'
// 就绪必需的表由 P1 等数据保证, 不能标客户无此类资料 (后端同样拒绝)
const REQUIRED = new Set(['accounts', 'trial_balance', 'journal_entries'])

function requestReason(request: DataRequest, t: Messages, lang: 'zh' | 'en'): string {
  const params = request.reason_params ?? {}
  if (request.reason_code === 'field_unavailable') {
    const fields = (Array.isArray(params.fields) ? params.fields : []).map(
      (field) => t.data.fieldNames[String(field)] ?? String(field),
    )
    return t.requests.reasons.field_unavailable(
      fields.join(lang === 'zh' ? '与' : ' and '),
      String(params.source ?? 'Xero'),
    )
  }
  if (request.reason_code === 'missing_table') return t.requests.reasons.missing_table
  if (request.reason_code === 'not_provided') return t.requests.reasons.not_provided
  return request.reason_code
}

// 一项请求的用途, 原因与格式; 标了客户无此类资料的另列理由与标记人 (只读)
function RequestDetails({ request }: { request: DataRequest }) {
  const { t, lang, formatDateTime } = useI18n()
  const actorName = useActorName()
  return (
    <div className="flex min-w-0 flex-[1_1_360px] flex-col gap-1.5">
      <span className="font-semibold text-[15px]">{requestTitle(request, t)}</span>
      <dl className="m-0 grid grid-cols-[72px_minmax(0,1fr)] gap-x-2.5 gap-y-1 text-[13px]">
        {request.used_for?.length ? (
          <>
            <dt className="text-muted-foreground">{t.requests.usedFor}</dt>
            <dd className="m-0">
              {request.used_for.map((id) => procedureName(t, id, id)).join(lang === 'zh' ? '、' : ', ')}
            </dd>
          </>
        ) : null}
        {request.status === 'unavailable' ? (
          <>
            <dt className="text-muted-foreground">{t.requests.unavailable.reason}</dt>
            <dd className="m-0 text-[#2d3443]">{request.unavailable_reason}</dd>
            <dt className="text-muted-foreground">{t.requests.unavailable.markedBy}</dt>
            <dd className="m-0 text-[#2d3443]">
              {[actorName(request.resolved_by), formatDateTime(request.resolved_at)].filter(Boolean).join(' · ')}
            </dd>
          </>
        ) : (
          <>
            <dt className="text-muted-foreground">{t.requests.why}</dt>
            <dd className="m-0 text-[#2d3443]">{requestReason(request, t, lang)}</dd>
            <dt className="text-muted-foreground">{t.requests.format}</dt>
            <dd className="m-0 text-[#2d3443]">{t.requests.formats[request.kind]}</dd>
          </>
        )}
      </dl>
    </div>
  )
}

function RequestStatus({ request }: { request: DataRequest }) {
  const { t } = useI18n()
  return (
    <StatusPill tone={request.status === 'received' ? 'ok' : 'neutral'}>
      {t.requests.statuses[request.status ?? 'open'] ?? request.status}
    </StatusPill>
  )
}

// 标了客户无此类资料的请求 (需求 R280): 只读, 列出理由与标记人, 不再有入口
export function MarkedRequests({ requests }: { requests: DataRequest[] }) {
  const { t } = useI18n()
  if (!requests.length) return null
  return (
    <section aria-label={t.requests.marked.title} className="overflow-hidden rounded-[14px] border bg-card">
      <div className="flex flex-col gap-0.5 border-line-soft border-b px-5 py-3.5">
        <h2 className="m-0 font-semibold text-base">{t.requests.marked.title}</h2>
        <span className="text-[13px] text-muted-foreground">{t.requests.marked.intro}</span>
      </div>
      <ul className="m-0 list-none p-0">
        {requests.map((request) => (
          <li key={request.id} className="flex flex-wrap items-start gap-3.5 border-line-soft border-b px-5 py-3.5">
            <RequestDetails request={request} />
            <RequestStatus request={request} />
          </li>
        ))}
      </ul>
    </section>
  )
}

// 数据管理智能体提出, 审计助理转达的补交资料清单; 每项可直接上传, 账表走导入, 其他走文件上传.
// marked 为标了客户无此类资料的, 列在后面, 只读
export function DataRequestCard({
  eid,
  requests,
  marked = [],
  awaiting,
  disabled,
  deferred,
  onDefer,
  deferring,
}: {
  eid: string
  requests: DataRequest[]
  marked?: DataRequest[]
  awaiting: boolean
  disabled: boolean
  deferred: boolean
  onDefer: () => void
  deferring: boolean
}) {
  const { t } = useI18n()
  const upload = useUploadDocuments(eid)
  const fileInput = useRef<HTMLInputElement>(null)
  const [target, setTarget] = useState<DataRequest | null>(null)
  const [importing, setImporting] = useState<DataRequest | null>(null)
  const [marking, setMarking] = useState<DataRequest | null>(null)

  const start = (request: DataRequest) => {
    if (request.kind === 'table') {
      setImporting(request)
      return
    }
    setTarget(request)
    fileInput.current?.click()
  }

  const onError = (error: unknown) => toast.error(errorText(error, t))

  return (
    <section
      aria-label={t.requests.title}
      className="ml-[42px] overflow-hidden rounded-[14px] border-[1.5px] border-primary bg-card shadow-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-line-soft border-b px-[18px] py-3.5">
        <div className="flex flex-col">
          <span className="font-semibold text-[12px] text-accent-foreground tracking-wide">{t.requests.eyebrow}</span>
          <h2 className="m-0 font-semibold text-[17px]">{t.requests.title}</h2>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-0.5 text-[12px] text-secondary-foreground">
          <Database className="size-3.5" aria-hidden="true" />
          {t.requests.raisedBy}
        </span>
      </div>
      <input
        ref={fileInput}
        type="file"
        multiple
        accept={DOC_ACCEPT}
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          if (files.length && target) {
            upload.mutate(
              { files, requestId: target.id },
              { onSuccess: (docs) => toast.success(t.data.uploaded(docs.length)), onError },
            )
          }
          event.target.value = ''
        }}
      />
      <ul className="m-0 list-none p-0">
        {[...requests, ...marked].map((request) => {
          const open = request.status === 'open'
          return (
            <li
              key={request.id}
              className="flex flex-wrap items-start gap-3.5 border-line-soft border-b px-[18px] py-3.5"
            >
              <RequestDetails request={request} />
              {open ? (
                <div className="flex flex-wrap gap-2">
                  {request.table_kind && REQUIRED.has(request.table_kind) ? null : (
                    <Button type="button" variant="ghost" disabled={disabled} onClick={() => setMarking(request)}>
                      {t.requests.unavailable.action}
                    </Button>
                  )}
                  <Button
                    type="button"
                    disabled={disabled || (upload.isPending && target?.id === request.id)}
                    onClick={() => start(request)}
                  >
                    <Upload className="size-4" aria-hidden="true" />
                    {upload.isPending && target?.id === request.id ? t.requests.uploading : t.requests.upload}
                  </Button>
                </div>
              ) : (
                <RequestStatus request={request} />
              )}
            </li>
          )
        })}
      </ul>
      <div className="flex flex-wrap items-center justify-between gap-2.5 bg-[#fafbfc] px-[18px] py-3">
        <div className="flex flex-wrap gap-2">
          {!deferred && requests.some((request) => request.status === 'open') ? (
            <Button type="button" variant="outline" disabled={deferring} onClick={onDefer}>
              {t.card.decideLater}
            </Button>
          ) : null}
          {awaiting ? <DataReadyButton eid={eid} disabled={disabled} /> : null}
        </div>
        <Link to="/engagements/$eid/data" params={{ eid }} search={{}} className="text-[13.5px]">
          {t.requests.open}
        </Link>
      </div>
      <ImportDialog
        eid={eid}
        open={importing !== null}
        kind={importing?.table_kind ?? undefined}
        requestId={importing?.id}
        onOpenChange={(next) => {
          if (!next) setImporting(null)
        }}
      />
      <UnavailableDialog eid={eid} request={marking} onClose={() => setMarking(null)} />
    </section>
  )
}

// 标 "客户无此类资料": 理由必填, 等这项资料的程序记为未执行 (需求 R280)
function UnavailableDialog({
  eid,
  request,
  onClose,
}: {
  eid: string
  request: DataRequest | null
  onClose: () => void
}) {
  const { t } = useI18n()
  const mark = useMarkUnavailable(eid)
  const [reason, setReason] = useState('')
  // 取消, 关掉或标好后都清空, 下次对别的请求打开时不带上写到一半的理由
  const close = () => {
    setReason('')
    onClose()
  }
  return (
    <Dialog
      open={request !== null}
      onOpenChange={(open) => {
        if (!open) close()
      }}
    >
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle className="font-semibold text-base">
            {request ? t.requests.unavailable.title(requestTitle(request, t)) : null}
          </DialogTitle>
          <DialogDescription>{t.requests.unavailable.intro}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="request-unavailable-reason">{t.requests.unavailable.reason}</label>
          <Textarea
            id="request-unavailable-reason"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={close}>
            {t.dataTable.cancel}
          </Button>
          <Button
            type="button"
            disabled={!request || !reason.trim() || mark.isPending}
            onClick={() =>
              request &&
              mark.mutate(
                { requestId: request.id, reason: reason.trim() },
                {
                  onSuccess: () => {
                    toast.success(t.requests.unavailable.done)
                    close()
                  },
                  onError: (error) => toast.error(errorText(error, t)),
                },
              )
            }
          >
            {t.requests.unavailable.confirm}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
