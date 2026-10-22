import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { Upload } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { evidenceListDocumentsOptions } from '@/client/@tanstack/react-query.gen'
import type { DocumentRef, EvidenceRequest, EvidenceRequestIn, EvidenceUnavailableIn } from '@/client/types.gen'
import { Code, StatusPill } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { Button, buttonVariants } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { UPLOAD_ACCEPT } from '@/lib/data-api'
import { decisionsDisabled } from '@/lib/engagement'
import { errorText } from '@/lib/errors'
import {
  useEditEvidenceRequest,
  useEvidenceRequests,
  useMarkEvidenceUnavailable,
  useUploadEvidenceDocuments,
} from '@/lib/evidence-api'
import {
  editRequestBody,
  REQUESTED_FROM,
  type RequestStatus,
  rejudgedRequest,
  requestDocuments,
  requestEditForm,
  requestFrom,
  requestOpen,
  requestRelated,
  requestStatus,
  unavailableBody,
} from '@/lib/evidence-requests'
import { useI18n } from '@/lib/i18n'
import { reasonText } from '@/lib/reason'
import { useEngagement } from '@/lib/use-engagement'

// 补充证据请求 (后端需求 R369, 页面 R370): 数据管理页的一块. 每条显示状态 (等待证据/已收到/无法取得), 要什么, 向谁要,
// 关联对象与收到后登记的文件; 还在等证据的可改, 可对着请求上传文件 (置已收到), 也可标无法取得 (另记范围受限). 原复核
// 任务随之回到待处理, 状态变化时由 lib/evidence-api 刷新本项目其他查询.

const TONE: Record<RequestStatus, 'warn' | 'ok' | 'neutral'> = {
  open: 'warn',
  received: 'ok',
  unavailable: 'neutral',
}

export function EvidenceRequests({ eid }: { eid: string }) {
  const { t } = useI18n()
  const requests = useEvidenceRequests(eid)
  const documents = useQuery(evidenceListDocumentsOptions({ path: { eid } }))
  const overview = useEngagement(eid).data
  const edit = useEditEvidenceRequest(eid)
  const unavailable = useMarkEvidenceUnavailable(eid)
  const upload = useUploadEvidenceDocuments(eid)
  const [editing, setEditing] = useState<EvidenceRequest | null>(null)
  const [marking, setMarking] = useState<EvidenceRequest | null>(null)
  const [uploading, setUploading] = useState<string | null>(null)
  const rows = requests.data ?? []
  if (!rows.length) return null
  const openCount = rows.filter(requestOpen).length
  const disabled = decisionsDisabled(overview)
  const send = (request: EvidenceRequest, files: File[]) => {
    setUploading(request.id)
    upload.mutate(
      { path: { eid }, body: { files, request_id: request.id } },
      {
        onSuccess: (saved) => toast.success(t.evidenceRequests.uploaded(saved.length)),
        onError: (error) => toast.error(errorText(error, t)),
        onSettled: () => setUploading(null),
      },
    )
  }
  return (
    <section
      aria-label={t.evidenceRequests.title}
      className="overflow-hidden rounded-[14px] border bg-card"
      data-testid="evidence-requests"
    >
      <div className="flex flex-col gap-0.5 border-line-soft border-b px-5 py-3.5">
        <h2 className="m-0 font-semibold text-base">{t.evidenceRequests.title}</h2>
        <span className="text-[13px] text-muted-foreground">
          {openCount ? t.evidenceRequests.intro(openCount) : t.evidenceRequests.closed}
        </span>
      </div>
      <QueryState isPending={requests.isPending} error={requests.error}>
        <ul className="m-0 list-none p-0">
          {rows.map((request) => (
            <EvidenceRequestRow
              key={request.id}
              eid={eid}
              request={request}
              documents={documents.data ?? []}
              disabled={disabled}
              uploading={uploading === request.id}
              onEdit={setEditing}
              onUnavailable={setMarking}
              onUpload={send}
            />
          ))}
        </ul>
      </QueryState>
      {editing ? (
        <EditRequestDialog
          request={editing}
          saving={edit.isPending}
          onSave={(body) =>
            edit.mutate(
              { path: { eid, request_id: editing.id }, body },
              {
                onSuccess: () => {
                  toast.success(t.evidenceRequests.saved)
                  setEditing(null)
                },
                onError: (error) => toast.error(errorText(error, t)),
              },
            )
          }
          onClose={() => setEditing(null)}
        />
      ) : null}
      {marking ? (
        <UnavailableDialog
          request={marking}
          saving={unavailable.isPending}
          onSave={(body) =>
            unavailable.mutate(
              { path: { eid, request_id: marking.id }, body },
              {
                onSuccess: () => {
                  toast.success(t.evidenceRequests.unavailableDone)
                  setMarking(null)
                },
                onError: (error) => toast.error(errorText(error, t)),
              },
            )
          }
          onClose={() => setMarking(null)}
        />
      ) : null}
    </section>
  )
}

// 一条请求: 状态, 要什么, 向谁要, 关联对象, 收到的文件或无法取得的理由; 等证据时给修改, 上传与标无法取得三个入口
export function EvidenceRequestRow({
  eid,
  request,
  documents,
  disabled,
  uploading = false,
  onEdit,
  onUnavailable,
  onUpload,
}: {
  eid: string
  request: EvidenceRequest
  documents: DocumentRef[]
  disabled: boolean
  uploading?: boolean
  onEdit: (request: EvidenceRequest) => void
  onUnavailable: (request: EvidenceRequest) => void
  onUpload: (request: EvidenceRequest, files: File[]) => void
}) {
  const { t, formatDateTime } = useI18n()
  const status = requestStatus(request)
  const related = requestRelated(request)
  const received = requestDocuments(request)
  const open = requestOpen(request)
  const files = received.map((id) => ({ id, document: documents.find((item) => item.id === id) }))
  return (
    <li
      className="flex flex-col gap-2 border-line-soft border-b px-5 py-3.5 last:border-b-0"
      data-testid={`evidence-request-${request.id}`}
      data-status={status}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Code className="font-semibold">{request.id}</Code>
        <StatusPill tone={TONE[status]}>{t.evidenceRequests.statuses[status]}</StatusPill>
        <span className="text-[13px] text-muted-foreground">{t.evidenceRequests.sources[requestFrom(request)]}</span>
        <Code className="text-[12px] text-muted-foreground">{request.procedure_id}</Code>
        <Code className="text-[12px] text-muted-foreground">{request.subject_id}</Code>
        <span className="flex-1" />
        <span className="text-[12px] text-muted-foreground">
          {t.evidenceRequests.requestedAt(formatDateTime(request.requested_at))}
        </span>
      </div>
      <p className="m-0 text-[14px]">{request.description}</p>
      <div className="flex flex-col gap-1 text-[13px]">
        {related.length ? (
          <span className="flex flex-wrap items-baseline gap-1.5">
            <span className="text-muted-foreground">{t.evidenceRequests.fields.relatedObjects}:</span>
            {related.map((id) => (
              <Code key={id}>{id}</Code>
            ))}
          </span>
        ) : null}
        {status === 'received' && files.length ? (
          <span className="flex flex-wrap items-baseline gap-1.5">
            <span className="text-muted-foreground">{t.evidenceRequests.fields.documents}:</span>
            {files.map(({ id, document }) =>
              document ? (
                <Link
                  key={id}
                  to="/engagements/$eid/data/documents/$docId"
                  params={{ eid, docId: id }}
                  className="text-accent-foreground underline underline-offset-2"
                >
                  {document.file_name}
                </Link>
              ) : (
                <Code key={id}>{id}</Code>
              ),
            )}
          </span>
        ) : null}
        {status === 'unavailable' ? (
          <span>
            <span className="text-muted-foreground">{t.evidenceRequests.fields.unavailableReason}: </span>
            {reasonText(request.unavailable_reason, t)}
          </span>
        ) : null}
      </div>
      {open ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" disabled={disabled} onClick={() => onEdit(request)}>
            {t.evidenceRequests.edit}
          </Button>
          <label
            className={cn(
              buttonVariants({ variant: 'outline' }),
              disabled || uploading ? 'pointer-events-none opacity-50' : 'cursor-pointer',
            )}
          >
            <Upload className="size-4" aria-hidden="true" />
            {uploading ? t.evidenceRequests.uploading : t.evidenceRequests.upload}
            <input
              type="file"
              multiple
              accept={UPLOAD_ACCEPT}
              className="sr-only"
              disabled={disabled || uploading}
              aria-label={t.evidenceRequests.upload}
              data-testid={`evidence-upload-${request.id}`}
              onChange={(event) => {
                const files = Array.from(event.target.files ?? [])
                if (files.length) onUpload(request, files)
                event.target.value = ''
              }}
            />
          </label>
          <Button type="button" variant="ghost" disabled={disabled} onClick={() => onUnavailable(request)}>
            {t.evidenceRequests.unavailable}
          </Button>
        </div>
      ) : null}
    </li>
  )
}

// 修改请求: 要什么, 向谁要, 关联对象; 只提交改了的项 (lib/evidence-requests.editRequestBody)
function EditRequestDialog({
  request,
  saving,
  onSave,
  onClose,
}: {
  request: EvidenceRequest
  saving: boolean
  onSave: (body: EvidenceRequestIn) => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const [form, setForm] = useState(() => requestEditForm(request))
  const set = (patch: Partial<typeof form>) => setForm((current) => ({ ...current, ...patch }))
  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="text-base">{t.evidenceRequests.editTitle(request.id)}</DialogTitle>
          <DialogDescription>{t.evidenceRequests.editIntro}</DialogDescription>
        </DialogHeader>
        <label htmlFor="evidence-request-description" className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {t.evidenceRequests.fields.description}
          <Textarea
            id="evidence-request-description"
            rows={3}
            className="font-normal"
            value={form.description}
            onChange={(event) => set({ description: event.target.value })}
          />
        </label>
        <label htmlFor="evidence-request-from" className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {t.evidenceRequests.fields.requestedFrom}
          <select
            id="evidence-request-from"
            className="h-10 rounded-[9px] border border-input bg-card px-2.5 font-normal text-[14px]"
            value={form.requestedFrom}
            onChange={(event) => set({ requestedFrom: event.target.value as typeof form.requestedFrom })}
          >
            {REQUESTED_FROM.map((value) => (
              <option key={value} value={value}>
                {t.evidenceRequests.sources[value]}
              </option>
            ))}
          </select>
        </label>
        <label htmlFor="evidence-request-related" className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {t.evidenceRequests.fields.relatedObjects}
          <Input
            id="evidence-request-related"
            className="font-normal"
            value={form.relatedIds}
            onChange={(event) => set({ relatedIds: event.target.value })}
          />
          <span className="font-normal text-[12.5px] text-muted-foreground">{t.evidenceRequests.relatedHint}</span>
        </label>
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.evidenceRequests.cancel}
          </Button>
          <Button
            type="button"
            disabled={saving}
            onClick={() => {
              const body = editRequestBody(request, form, t)
              if (typeof body === 'string') return toast.error(body)
              onSave(body)
            }}
          >
            {t.evidenceRequests.save}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// 标无法取得: 理由选填; 原复核任务回待处理, 另记范围受限 (后端)
function UnavailableDialog({
  request,
  saving,
  onSave,
  onClose,
}: {
  request: EvidenceRequest
  saving: boolean
  onSave: (body: EvidenceUnavailableIn) => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const [reason, setReason] = useState('')
  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle className="text-base">{t.evidenceRequests.unavailableTitle(request.id)}</DialogTitle>
          <DialogDescription>{t.evidenceRequests.unavailableIntro}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="evidence-unavailable-reason">{t.reason.label}</label>
          <Textarea
            id="evidence-unavailable-reason"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.evidenceRequests.cancel}
          </Button>
          <Button type="button" disabled={saving} onClick={() => onSave(unavailableBody(reason))}>
            {t.evidenceRequests.unavailable}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// 判断详情里"因补充证据重判"的来龙去脉 (需求 R370 第 3 条): 请求的 rejudged_judgment_id 等于该判断时给一条指向补充
// 证据请求区块的链接; 后端字段为后续预留, 现在恒为空, 空时不显示
export function RejudgeNotice({ eid, judgmentId }: { eid: string; judgmentId: string }) {
  const { t } = useI18n()
  const requests = useEvidenceRequests(eid, { poll: false })
  const request = rejudgedRequest(requests.data ?? [], judgmentId)
  if (!request) return null
  return (
    <p className="m-0 text-[13px]" data-testid="rejudge-notice">
      <Link
        to="/engagements/$eid/data"
        params={{ eid }}
        search={{}}
        className="text-accent-foreground underline underline-offset-2"
      >
        {t.evidenceRequests.rejudged(request.id)}
      </Link>
    </p>
  )
}
