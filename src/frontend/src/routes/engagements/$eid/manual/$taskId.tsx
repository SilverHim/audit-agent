import { useMutation, useQueryClient } from '@tanstack/react-query'
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { Upload } from 'lucide-react'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { manualListManualTasksQueryKey, reviewsSubmitReviewRecordMutation } from '@/client/@tanstack/react-query.gen'
import type { DocumentRef } from '@/client/types.gen'
import { Code, PageHeader, SectionLabel, StatusPill } from '@/components/chrome'
import { EvidenceLink } from '@/components/evidence-link'
import { SELECT } from '@/components/import-dialog'
import { QueryState } from '@/components/query-state'
import { Button, buttonVariants } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { UPLOAD_ACCEPT } from '@/lib/data-api'
import { decisionsDisabled, invalidateEngagement } from '@/lib/engagement'
import { apiMessage, errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import {
  emptyManualDraft,
  type ManualDraft,
  type ManualTaskView,
  manualRecordBody,
  materialText,
  type ResultField,
  resultFields,
  useDocumentStatus,
  useManualTasks,
  useUploadDocuments,
} from '@/lib/manual-api'
import { procedureName } from '@/lib/review'
import { useEngagement } from '@/lib/use-engagement'

export const Route = createFileRoute('/engagements/$eid/manual/$taskId')({ component: ManualTaskPage })

// 一条人工程序 (后端需求 R94): 看说明与材料, 上传并选证据 (文件与页), 按结果模型填结构化结果, 写执行日期,
// 结论与理由后提交 manual_result. 校验以后端为准, 这里只拦明显缺的项
function ManualTaskPage() {
  const { eid, taskId } = Route.useParams()
  const { t } = useI18n()
  const tasks = useManualTasks(eid)
  const view = tasks.data?.find((item) => item.task.id === taskId)
  return (
    <>
      <PageHeader
        title={
          view ? (
            <>
              <Code>{view.task.procedure_id}</Code> · {procedureName(t, view.task.procedure_id, view.procedure_name)}
            </>
          ) : (
            taskId
          )
        }
        subtitle={view ? t.manualPage.owner(t.agents[view.owner] ?? view.owner) : undefined}
        actions={
          <Link
            to="/engagements/$eid/manual"
            params={{ eid }}
            className={buttonVariants({ variant: 'outline', size: 'lg' })}
          >
            {t.manualPage.back}
          </Link>
        }
      />
      <div className="mx-auto flex w-full max-w-[900px] flex-col gap-5 px-4 py-6 sm:px-7">
        <QueryState isPending={tasks.isPending} error={tasks.error}>
          {view ? (
            <ManualTask eid={eid} view={view} />
          ) : (
            <p className="text-muted-foreground text-sm">{t.manualPage.notFound}</p>
          )}
        </QueryState>
      </div>
    </>
  )
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-xl border bg-card p-5">
      <h2 className="m-0 font-semibold text-base">{title}</h2>
      {children}
    </section>
  )
}

function ManualTask({ eid, view }: { eid: string; view: ManualTaskView }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const overview = useEngagement(eid).data
  const [draft, setDraft] = useState<ManualDraft>(emptyManualDraft)
  const set = (patch: Partial<ManualDraft>) => setDraft((current) => ({ ...current, ...patch }))
  const fields = resultFields(view.result_schema)
  const pending = view.task.status === 'pending'
  const material = Object.entries(view.request.material)

  const submit = useMutation({
    ...reviewsSubmitReviewRecordMutation(),
    onSuccess: async () => {
      toast.success(t.toasts.decisionRecorded)
      await invalidateEngagement(queryClient, eid)
      void navigate({ to: '/engagements/$eid/manual', params: { eid } })
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  const record = () => {
    const body = manualRecordBody(view, draft, t)
    if (typeof body === 'string') return toast.error(body)
    submit.mutate({ path: { eid }, body })
  }
  const busy = decisionsDisabled(overview) || submit.isPending

  return (
    <>
      <Card title={t.manualPage.instructions}>
        <p className="m-0 text-[14px] leading-[1.65]" lang="en">
          {view.request.instructions}
        </p>
        <div className="flex flex-col gap-1.5">
          <SectionLabel>{t.manualPage.material}</SectionLabel>
          {material.length ? (
            <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-[14px]">
              {material.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-mono text-muted-foreground">{key}</dt>
                  <dd className="m-0 break-all font-mono">{materialText(value)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <span className="text-[13px] text-muted-foreground">{t.manualPage.noMaterial}</span>
          )}
        </div>
      </Card>

      {pending ? (
        <>
          <EvidenceCard
            eid={eid}
            view={view}
            draft={draft}
            onChange={(evidence) => set({ evidence })}
            disabled={busy}
          />
          <Card title={t.manualPage.result}>
            {fields.map((field) => (
              <ResultInput
                key={field.name}
                id={`detail-${field.name}`}
                field={field}
                value={draft.values[field.name]}
                onChange={(value) => set({ values: { ...draft.values, [field.name]: value } })}
              />
            ))}
            <div className="flex flex-col gap-1.5">
              <label htmlFor="performed-on" className="font-semibold text-[13px]">
                {t.manualPage.performedOn}
              </label>
              <Input
                id="performed-on"
                type="date"
                className="h-10 max-w-[220px]"
                value={draft.performedOn}
                onChange={(event) => set({ performedOn: event.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="manual-conclusion" className="font-semibold text-[13px]">
                {t.manualPage.conclusion}
              </label>
              <Textarea
                id="manual-conclusion"
                rows={3}
                value={draft.conclusion}
                onChange={(event) => set({ conclusion: event.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="manual-reason" className="font-semibold text-[13px]">
                {t.card.reason}
              </label>
              <Textarea
                id="manual-reason"
                rows={3}
                value={draft.reason}
                onChange={(event) => set({ reason: event.target.value })}
              />
            </div>
            <div className="flex justify-end">
              <Button type="button" size="lg" disabled={busy} onClick={record}>
                {t.manualPage.submit}
              </Button>
            </div>
          </Card>
        </>
      ) : (
        <StatusPill tone="ok" className="self-start">
          {t.manualPage.alreadyDecided}
        </StatusPill>
      )}
    </>
  )
}

// 证据: 上传 (已有的 POST /documents), 再从本项目可作证据的已登记文件里选文件与页
function EvidenceCard({
  eid,
  view,
  draft,
  onChange,
  disabled,
}: {
  eid: string
  view: ManualTaskView
  draft: ManualDraft
  onChange: (evidence: ManualDraft['evidence']) => void
  disabled: boolean
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const upload = useUploadDocuments(eid)
  const [uploaded, setUploaded] = useState<DocumentRef[]>([])
  const usable = new Set(view.documents.map((item) => item.id))
  const waiting = uploaded.filter((item) => !usable.has(item.id))
  const status = useDocumentStatus(
    eid,
    waiting.map((item) => item.id),
  )
  // 文件登记每刷新一次 (还在分类时每 3 秒), 人工队列跟着重取, 分好类的文件出现在可选里
  useEffect(() => {
    if (status.dataUpdatedAt)
      void queryClient.invalidateQueries({ queryKey: manualListManualTasksQueryKey({ path: { eid } }) })
  }, [status.dataUpdatedAt, queryClient, eid])

  const send = (files: FileList | null) => {
    const list = Array.from(files ?? [])
    if (!list.length) return
    upload.mutate(list, {
      onSuccess: (documents) =>
        setUploaded((current) => [...current, ...documents.filter((item) => !current.some((c) => c.id === item.id))]),
      onError: (error) => toast.error(errorText(error, t)),
    })
  }
  const first = view.documents[0]
  const update = (index: number, patch: Partial<ManualDraft['evidence'][number]>) =>
    onChange(draft.evidence.map((item, i) => (i === index ? { ...item, ...patch } : item)))

  return (
    <Card title={t.manualPage.evidence}>
      <span className="text-[13px] text-muted-foreground">{t.manualPage.minEvidence(view.request.min_evidence)}</span>
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={input}
          type="file"
          multiple
          accept={UPLOAD_ACCEPT}
          className="sr-only"
          tabIndex={-1}
          aria-label={t.manualPage.upload}
          data-testid="manual-upload"
          onChange={(event) => {
            send(event.target.files)
            event.target.value = ''
          }}
        />
        <Button
          type="button"
          variant="outline"
          disabled={disabled || upload.isPending}
          onClick={() => input.current?.click()}
        >
          <Upload className="size-4" aria-hidden="true" />
          {upload.isPending ? t.manualPage.uploading : t.manualPage.upload}
        </Button>
      </div>
      {waiting.length ? (
        <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13px]">
          {waiting.map((item) => {
            const current = status.data?.find((doc) => doc.id === item.id) ?? item
            return (
              <li key={item.id} className="flex flex-wrap gap-2">
                <span className="font-medium">{item.file_name}</span>
                <span className="text-muted-foreground">
                  {current.status === 'received'
                    ? t.manualPage.classifying
                    : current.status === 'needs_manual'
                      ? t.manualPage.unclassified
                      : t.manualPage.notEvidence(t.evidencePage.docTypes[current.type] ?? current.type)}
                </span>
              </li>
            )
          })}
        </ul>
      ) : null}

      {view.documents.length ? (
        <>
          {draft.evidence.map((item, index) => {
            const document = view.documents.find((doc) => doc.id === item.documentId)
            return (
              <div
                // biome-ignore lint/suspicious/noArrayIndexKey: 同一文件可选多页, 行没有别的稳定键
                key={index}
                className="grid grid-cols-[minmax(0,1fr)_110px_auto] items-end gap-2.5"
              >
                <div className="flex flex-col gap-1">
                  <label htmlFor={`evidence-doc-${index}`} className="text-[12.5px] text-[#4a5262]">
                    {t.manualPage.document}
                  </label>
                  <select
                    id={`evidence-doc-${index}`}
                    className={SELECT}
                    value={item.documentId}
                    onChange={(event) => update(index, { documentId: event.target.value, page: 1 })}
                  >
                    {view.documents.map((doc) => (
                      <option key={doc.id} value={doc.id}>
                        {doc.file_name} · {t.evidencePage.docTypes[doc.type] ?? doc.type}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="flex flex-col gap-1">
                  <label htmlFor={`evidence-page-${index}`} className="text-[12.5px] text-[#4a5262]">
                    {t.manualPage.page}
                  </label>
                  <Input
                    id={`evidence-page-${index}`}
                    type="number"
                    min={1}
                    max={document?.page_count}
                    className="h-10 font-mono"
                    value={item.page}
                    onChange={(event) => update(index, { page: Number(event.target.value) })}
                  />
                </div>
                <div className="flex items-center gap-2 pb-1">
                  {document ? (
                    <EvidenceLink
                      eid={eid}
                      label={t.evidencePage.open}
                      pointer={{
                        type: view.evidence_types[document.id],
                        source_type: 'document',
                        source_id: document.id,
                        locator: document.file_name,
                        page: item.page,
                      }}
                    />
                  ) : null}
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => onChange(draft.evidence.filter((_, i) => i !== index))}
                  >
                    {t.manualPage.remove}
                  </Button>
                </div>
              </div>
            )
          })}
          <Button
            type="button"
            variant="outline"
            className="self-start"
            disabled={disabled}
            onClick={() => onChange([...draft.evidence, { documentId: first.id, page: 1 }])}
          >
            {t.manualPage.addEvidence}
          </Button>
        </>
      ) : (
        <span className="text-[13px] text-muted-foreground">{t.manualPage.noDocuments}</span>
      )}
    </Card>
  )
}

// 结构化结果的一个字段: 文字, 数字, 日期, 勾选, 枚举各用对应的输入框, 其余写 JSON
function ResultInput({
  id,
  field,
  value,
  onChange,
}: {
  id: string
  field: ResultField
  value: string | boolean | undefined
  onChange: (value: string | boolean) => void
}) {
  const { t } = useI18n()
  const text = typeof value === 'string' ? value : ''
  if (field.kind === 'boolean') {
    return (
      <div className="flex items-center gap-2 text-[13.5px]">
        <input
          id={id}
          type="checkbox"
          className="size-4 accent-primary"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
        <label htmlFor={id}>{field.title}</label>
      </div>
    )
  }
  const label = (
    <label htmlFor={id} className="font-semibold text-[13px]">
      {field.title}{' '}
      <span className="font-normal text-muted-foreground">
        {field.kind === 'json' ? t.manualPage.json : null}
        {field.required ? null : ` ${t.card.reasonOptional}`}
      </span>
    </label>
  )
  if (field.kind === 'enum') {
    return (
      <div className="flex flex-col gap-1.5">
        {label}
        <select id={id} className={SELECT} value={text} onChange={(event) => onChange(event.target.value)}>
          <option value="" />
          {field.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>
    )
  }
  if (field.kind === 'json') {
    return (
      <div className="flex flex-col gap-1.5">
        {label}
        <Textarea
          id={id}
          rows={3}
          className="font-mono"
          value={text}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-1.5">
      {label}
      <Input
        id={id}
        type={field.kind === 'date' ? 'date' : 'text'}
        inputMode={field.kind === 'number' || field.kind === 'integer' ? 'decimal' : undefined}
        className="h-10 max-w-[420px]"
        value={text}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  )
}
