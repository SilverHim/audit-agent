import type { AbnLookupView } from '@/client/types.gen'
import type { Messages } from '@/locales/en'

// ABN 在线核对结果的文字 (需求 R289): text 为登记信息或查不了的原因, warnings 为要警示的几点 (已注销, 查不到,
// 名称与填写的法定名称不一致); nameSettled 为假 (名称还在防抖, 或结果是上次名称的) 时先不提示名称不一致
export function lookupSummary(
  result: AbnLookupView,
  t: Messages,
  nameSettled = true,
): { text: string; warnings: string[] } {
  if (result.status === 'unavailable') {
    return { text: t.abnLookup.unavailable[result.reason ?? 'service_error'], warnings: [] }
  }
  const record = result.record
  if (result.status === 'not_found' || !record) return { text: '', warnings: [t.abnLookup.notFound] }
  const facts = [
    record.entity_name || t.abnLookup.nameNotPublic,
    t.abnLookup.status[record.status],
    record.entity_type,
    record.acn ? `ACN ${record.acn}` : '',
  ].filter(Boolean)
  const warnings = []
  if (record.status === 'Cancelled') warnings.push(t.abnLookup.cancelled)
  if (nameSettled && result.name_matches === false) warnings.push(t.abnLookup.nameMismatch)
  return { text: t.abnLookup.registered(facts.join(' · ')), warnings }
}
