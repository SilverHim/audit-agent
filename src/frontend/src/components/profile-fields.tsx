import { CircleCheck, CircleHelp, CircleX } from 'lucide-react'
import type { ReactNode } from 'react'
import type { ClientProfile } from '@/client/types.gen'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { normalizeAbn } from '@/lib/abn'
import { useI18n } from '@/lib/i18n'

export const SELECT = 'h-10 rounded-lg border border-input bg-card px-2.5 text-[14px]'

export function LabeledField({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[12.5px] text-[#4a5262]">
        {label}
      </label>
      {children}
    </div>
  )
}

// 画像的 6 项必填与备注, ACC-01 的编辑表单与新建客户表单共用
export type ProfileDraft = {
  legalName: string
  abn: string
  entityType: string
  industry: string
  listed: boolean
  directors: string // 每行一位
  notes: string
}

export function profileDraft(profile?: ClientProfile): ProfileDraft {
  return {
    legalName: profile?.legal_name ?? '',
    abn: profile?.abn ?? '',
    entityType: profile?.entity_type ?? 'small_proprietary',
    industry: profile?.industry_anzsic ?? '',
    listed: profile?.listed ?? false,
    directors: profile?.directors.join('\n') ?? '',
    notes: profile?.notes ?? '',
  }
}

// 请求体里的这几项: 名称与行业去首尾空白, ABN 去空白, 董事按行拆开并去掉空行
export function draftFields(draft: ProfileDraft) {
  return {
    legal_name: draft.legalName.trim(),
    abn: normalizeAbn(draft.abn),
    entity_type: draft.entityType,
    industry_anzsic: draft.industry.trim(),
    listed: draft.listed,
    directors: draft.directors
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
    notes: draft.notes,
  }
}

// 就地提示: errors 为某项的错误 (该项标红), abnHint 显示在 ABN 下 (新建客户表单的核对结果, R289 的在线核对也放这里)
export type ProfileErrors = Partial<Record<'legalName' | 'abn', string>>

function FieldNote({ id, error, children }: { id: string; error?: string; children?: ReactNode }) {
  if (error) {
    return (
      <span id={id} className="text-[12.5px] text-destructive">
        {error}
      </span>
    )
  }
  return children ? (
    <span id={id} className="text-[12.5px] text-muted-foreground">
      {children}
    </span>
  ) : null
}

export function ProfileFields({
  idPrefix,
  draft,
  onChange,
  errors = {},
  abnHint,
}: {
  idPrefix: string
  draft: ProfileDraft
  onChange: (draft: ProfileDraft) => void
  errors?: ProfileErrors
  abnHint?: ReactNode
}) {
  const { t } = useI18n()
  const field = 'h-10'
  const set = (changes: Partial<ProfileDraft>) => onChange({ ...draft, ...changes })
  const described = (key: keyof ProfileErrors, hint?: ReactNode) =>
    errors[key] || hint ? `${idPrefix}-${key}-note` : undefined
  return (
    <>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
        <LabeledField id={`${idPrefix}-name`} label={t.clientPage.legalName}>
          <Input
            id={`${idPrefix}-name`}
            className={field}
            value={draft.legalName}
            aria-invalid={errors.legalName ? true : undefined}
            aria-describedby={described('legalName')}
            onChange={(event) => set({ legalName: event.target.value })}
          />
          <FieldNote id={`${idPrefix}-legalName-note`} error={errors.legalName} />
        </LabeledField>
        <LabeledField id={`${idPrefix}-abn`} label={t.clientPage.abn}>
          <Input
            id={`${idPrefix}-abn`}
            className={`${field} font-mono`}
            value={draft.abn}
            aria-invalid={errors.abn ? true : undefined}
            aria-describedby={described('abn', abnHint)}
            onChange={(event) => set({ abn: event.target.value })}
          />
          <FieldNote id={`${idPrefix}-abn-note`} error={errors.abn}>
            {abnHint}
          </FieldNote>
        </LabeledField>
        <LabeledField id={`${idPrefix}-type`} label={t.clientPage.entityType}>
          <select
            id={`${idPrefix}-type`}
            className={SELECT}
            value={draft.entityType}
            onChange={(event) => set({ entityType: event.target.value })}
          >
            {Object.entries(t.clientPage.entityTypes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </LabeledField>
        <LabeledField id={`${idPrefix}-industry`} label={t.clientPage.industry}>
          <Input
            id={`${idPrefix}-industry`}
            className={`${field} font-mono`}
            value={draft.industry}
            onChange={(event) => set({ industry: event.target.value })}
          />
        </LabeledField>
      </div>
      <label className="flex items-center gap-2 text-[14px]">
        <input
          type="checkbox"
          className="size-4 accent-primary"
          checked={draft.listed}
          onChange={(event) => set({ listed: event.target.checked })}
        />
        {t.clientPage.listed}
      </label>
      <LabeledField id={`${idPrefix}-directors`} label={`${t.clientPage.directors} · ${t.clientPage.directorsHint}`}>
        <Textarea
          id={`${idPrefix}-directors`}
          value={draft.directors}
          onChange={(event) => set({ directors: event.target.value })}
        />
      </LabeledField>
      <LabeledField id={`${idPrefix}-notes`} label={t.clientPage.notes}>
        <Textarea
          id={`${idPrefix}-notes`}
          value={draft.notes}
          onChange={(event) => set({ notes: event.target.value })}
        />
      </LabeledField>
    </>
  )
}

// 登记核查一项的状态: 通过, 未通过, 或待人工确认 (passed 为 null: 新建的客户还没有可查的登记来源)
export type CheckTone = 'ok' | 'risk' | 'warn'

export function checkTone(passed: boolean | null | undefined): CheckTone {
  if (passed === null || passed === undefined) return 'warn'
  return passed ? 'ok' : 'risk'
}

export function CheckIcon({ passed, className }: { passed: boolean | null | undefined; className: string }) {
  const tone = checkTone(passed)
  if (tone === 'warn') return <CircleHelp className={`${className} shrink-0 text-warn`} aria-hidden="true" />
  if (tone === 'ok') return <CircleCheck className={`${className} shrink-0 text-ok`} aria-hidden="true" />
  return <CircleX className={`${className} shrink-0 text-risk`} aria-hidden="true" />
}
