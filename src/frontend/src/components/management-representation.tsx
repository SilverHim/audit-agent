import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { evidenceListDocumentsOptions } from '@/client/@tanstack/react-query.gen'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { errorText } from '@/lib/errors'
import { useRegisterManagementRepresentation } from '@/lib/evidence-api'
import { emptyRepresentation, representationBody } from '@/lib/evidence-requests'
import { useI18n } from '@/lib/i18n'

// 登记管理层说明为证据 (后端需求 R369, 页面 R370): 说明人, 时间与内容; 后端存成证据文件, 可靠性为管理层说明 (低于
// 外部证据), 判断可以引用它. 成功后刷新文件列表; 项目已签字只读时不给提交.
export function ManagementRepresentationForm({ eid, disabled }: { eid: string; disabled: boolean }) {
  const { t } = useI18n()
  const register = useRegisterManagementRepresentation(eid)
  const documents = useQuery(evidenceListDocumentsOptions({ path: { eid } }))
  const [attachments, setAttachments] = useState<string[]>([])
  const [related, setRelated] = useState('')
  const [draft, setDraft] = useState(emptyRepresentation)
  const set = (patch: Partial<typeof draft>) => setDraft((current) => ({ ...current, ...patch }))
  const labels = t.managementRepresentation
  const submit = () => {
    const body = representationBody(draft, t)
    if (typeof body === 'string') return toast.error(body)
    register.mutate(
      {
        path: { eid },
        body: {
          ...body,
          attachment_ids: attachments,
          related_object_ids: related
            .split(/[,，]/)
            .map((s) => s.trim())
            .filter(Boolean),
        },
      },
      {
        onSuccess: () => {
          toast.success(labels.registered)
          setDraft(emptyRepresentation())
          setAttachments([])
          setRelated('')
        },
        onError: (error) => toast.error(errorText(error, t)),
      },
    )
  }
  return (
    <section
      aria-label={labels.title}
      className="flex flex-col gap-3 rounded-xl border bg-card p-5"
      data-testid="management-representation"
    >
      <div className="flex flex-col gap-0.5">
        <h2 className="m-0 font-semibold text-base">{labels.title}</h2>
        <span className="text-[13px] text-muted-foreground">{labels.intro}</span>
      </div>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
        <label htmlFor="mr-person" className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {labels.person}
          <Input
            id="mr-person"
            className="font-normal"
            value={draft.person}
            disabled={disabled}
            onChange={(event) => set({ person: event.target.value })}
          />
        </label>
        <label htmlFor="mr-stated-on" className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {labels.statedOn}
          <Input
            id="mr-stated-on"
            className="font-normal"
            placeholder={labels.statedOnHint}
            value={draft.statedOn}
            disabled={disabled}
            onChange={(event) => set({ statedOn: event.target.value })}
          />
        </label>
      </div>
      <label htmlFor="mr-content" className="flex flex-col gap-1.5 font-semibold text-[13px]">
        {labels.content}
        <Textarea
          id="mr-content"
          rows={3}
          className="font-normal"
          value={draft.content}
          disabled={disabled}
          onChange={(event) => set({ content: event.target.value })}
        />
      </label>
      <label htmlFor="mr-related" className="text-sm">
        {labels.related}
        <Input
          id="mr-related"
          value={related}
          disabled={disabled}
          onChange={(e) => setRelated(e.target.value)}
          aria-label={labels.related}
        />
      </label>
      <fieldset disabled={disabled} className="flex flex-col gap-2">
        <legend>{labels.attachments}</legend>
        {(documents.data ?? []).map((d) => (
          <label key={d.id} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={attachments.includes(d.id)}
              onChange={(e) =>
                setAttachments((ids) => (e.target.checked ? [...ids, d.id] : ids.filter((i) => i !== d.id)))
              }
            />
            {d.file_name}
          </label>
        ))}
      </fieldset>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" disabled={disabled || register.isPending} onClick={submit}>
          {register.isPending ? labels.registering : labels.register}
        </Button>
        {disabled ? <span className="text-[12.5px] text-muted-foreground">{labels.locked}</span> : null}
      </div>
    </section>
  )
}
