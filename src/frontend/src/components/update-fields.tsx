import type { MisstatementType, MisstatementUpdate } from '@/client/types.gen'
import { Code, SectionLabel } from '@/components/chrome'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/lib/i18n'
import { type UpdateDraft, VERIFIED_STATUSES } from '@/lib/misstatements'
import { formatAud } from '@/lib/money'

// 错报更新载荷 (FIN-03 分类, FIN-09 验证; 需求 R159, R163): 判断带的载荷的显示, 与复核表单里人给载荷的字段

const TYPES: MisstatementType[] = ['factual', 'judgmental', 'projected']
const SELECT = 'h-10 rounded-lg border border-input bg-card px-2.5 text-[14px]'

// 判断带的载荷 (AI 给的分类或验证结果), 复核页的判断卡片里显示; 没写的字段不列
export function UpdatePayload({ update }: { update: MisstatementUpdate }) {
  const { t } = useI18n()
  const u = t.updateForm
  const rows: [string, string][] = []
  if (update.type) rows.push([u.type, t.misstatementTypes[update.type]])
  if (update.fraud_indicative != null) rows.push([u.fraud, update.fraud_indicative ? '✓' : '—'])
  if (update.fraud_reason) rows.push([u.fraudReason, update.fraud_reason])
  if (update.status) rows.push([u.status, t.misstatementStatuses[update.status] ?? update.status])
  if (update.adjustment_entry_nos?.length) rows.push([u.entryNos, update.adjustment_entry_nos.join(', ')])
  if (update.corrected_cents != null) rows.push([u.corrected, formatAud(update.corrected_cents)])
  if (update.residual_cents != null) rows.push([u.residual, formatAud(update.residual_cents)])
  if (update.needs_recommunication) rows.push([u.recommunicate, '✓'])
  return (
    <div className="flex flex-col gap-1.5 border-line-soft border-b px-4 py-4" data-testid="update-payload">
      <SectionLabel>
        {u.carried} · {update.kind === 'classification' ? u.classification : u.verification} ·{' '}
        <Code>
          {update.misstatement_id} v{update.base_version}
        </Code>
      </SectionLabel>
      <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-[13.5px]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="m-0">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

// 复核表单里人给的载荷 (推翻, 对例外认定错报, 失败任务给手工结果时必填): 分类为类型与舞弊迹象 (有迹象时理由选填);
// 验证为结果, 凭证号, 已更正与剩余金额 (是否须重新沟通由后端按结果推出, 不在这里填)
export function UpdateFields({
  id,
  kind,
  value,
  onChange,
  disabled,
}: {
  id: string
  kind: MisstatementUpdate['kind']
  value: UpdateDraft
  onChange: (patch: Partial<UpdateDraft>) => void
  disabled: boolean
}) {
  const { t } = useI18n()
  const u = t.updateForm
  return (
    <div className="flex flex-col gap-2.5 rounded-[10px] border p-3.5" data-testid="update-fields">
      <span className="font-semibold text-[13px]">{kind === 'classification' ? u.classification : u.verification}</span>
      {kind === 'classification' ? (
        <>
          <label htmlFor={`${id}-type`} className="flex flex-col gap-1 text-[12.5px] text-[#4a5262]">
            {u.type}
            <select
              id={`${id}-type`}
              className={SELECT}
              value={value.type}
              disabled={disabled}
              onChange={(event) => onChange({ type: event.target.value as MisstatementType })}
            >
              {TYPES.map((item) => (
                <option key={item} value={item}>
                  {t.misstatementTypes[item]}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-center gap-2 text-[13.5px]">
            <input
              id={`${id}-fraud`}
              type="checkbox"
              className="size-4 accent-primary"
              checked={value.fraud}
              disabled={disabled}
              onChange={(event) => onChange({ fraud: event.target.checked })}
            />
            <label htmlFor={`${id}-fraud`}>{u.fraud}</label>
          </div>
          {value.fraud ? (
            <label htmlFor={`${id}-fraud-reason`} className="flex flex-col gap-1 text-[12.5px] text-[#4a5262]">
              {u.fraudReasonInput}
              <Textarea
                id={`${id}-fraud-reason`}
                rows={2}
                value={value.fraudReason}
                disabled={disabled}
                onChange={(event) => onChange({ fraudReason: event.target.value })}
              />
            </label>
          ) : null}
        </>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-2.5">
          <label htmlFor={`${id}-status`} className="flex flex-col gap-1 text-[12.5px] text-[#4a5262]">
            {u.status}
            <select
              id={`${id}-status`}
              className={SELECT}
              value={value.status}
              disabled={disabled}
              onChange={(event) => onChange({ status: event.target.value as UpdateDraft['status'] })}
            >
              {VERIFIED_STATUSES.map((item) => (
                <option key={item} value={item}>
                  {t.misstatementStatuses[item] ?? item}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor={`${id}-entries`} className="flex flex-col gap-1 text-[12.5px] text-[#4a5262]">
            {u.entryNos}
            <Input
              id={`${id}-entries`}
              className="h-10 font-mono"
              value={value.entryNos}
              disabled={disabled}
              onChange={(event) => onChange({ entryNos: event.target.value })}
            />
          </label>
          <label htmlFor={`${id}-corrected`} className="flex flex-col gap-1 text-[12.5px] text-[#4a5262]">
            {u.corrected}
            <Input
              id={`${id}-corrected`}
              className="h-10 font-mono"
              inputMode="decimal"
              value={value.corrected}
              disabled={disabled}
              onChange={(event) => onChange({ corrected: event.target.value })}
            />
          </label>
          <label htmlFor={`${id}-residual`} className="flex flex-col gap-1 text-[12.5px] text-[#4a5262]">
            {u.residual}
            <Input
              id={`${id}-residual`}
              className="h-10 font-mono"
              inputMode="decimal"
              value={value.residual}
              disabled={disabled}
              onChange={(event) => onChange({ residual: event.target.value })}
            />
          </label>
        </div>
      )}
    </div>
  )
}
