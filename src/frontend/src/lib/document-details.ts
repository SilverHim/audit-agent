import type { DocumentDetailsIn, DocumentRef, ExtractionRecord, Reliability } from '@/client/types.gen'
import { apiCode, errorText } from '@/lib/errors'
import type { Messages } from '@/locales/en'

// 文件的补登记 (后端需求 R90, 页面 R270): 可靠性, 文件日期与关联对象, 只提交改了的项; 历史取后端的文件登记各版

export type DetailsForm = {
  reliability: Reliability
  documentDate: string // YYYY-MM-DD, 空为没有
  relatedIds: string // 逗号或换行分隔
  reason: string
}

export function detailsForm(doc: DocumentRef): DetailsForm {
  return {
    reliability: doc.reliability,
    documentDate: doc.document_date ?? '',
    relatedIds: (doc.related_ids ?? []).join(', '),
    reason: '',
  }
}

// 关联对象: 逗号或换行分隔, 去首尾空白, 去掉空项 (重复的交给后端报 INVALID_VALUE)
export function parseRelatedIds(text: string): string[] {
  return text
    .split(/[,\n，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index])
}

// 请求体: 只带改了的项 (后端 None 为不改; 本版不支持把文件日期清空, 清空日期视为没改); 都没改为 null
export function detailsBody(doc: DocumentRef, form: DetailsForm): DocumentDetailsIn | null {
  const body: DocumentDetailsIn = { reason: form.reason.trim() }
  if (form.reliability !== doc.reliability) body.reliability = form.reliability
  if (form.documentDate && form.documentDate !== (doc.document_date ?? '')) body.document_date = form.documentDate
  const related = parseRelatedIds(form.relatedIds)
  if (!sameList(related, doc.related_ids ?? [])) body.related_ids = related
  const changed = body.reliability !== undefined || body.document_date !== undefined || body.related_ids !== undefined
  return changed ? body : null
}

// 补登记被拒的文字: 理由为空, 取值不合格 (附后端的说明, 如关联对象重复), 锁定, 忙各有文案; 其余同 errorText
export function detailsErrorText(error: unknown, t: Messages): string {
  const code = apiCode(error)
  if (code === 'MISSING_FIELD') return t.docView.details.errors.missing
  if (code === 'INVALID_VALUE') {
    const message = (error as { detail?: { message?: string } }).detail?.message
    return message ? t.docView.details.errors.invalidWith(message) : t.docView.details.errors.invalid
  }
  return errorText(error, t)
}

// 历史一版的说明: 改了的项 (field 为逗号分隔的 reliability, document_date, related_ids; 第一版或没补登记过为空)
export function historyFields(record: ExtractionRecord, t: Messages): string {
  if (!record.field) return t.docView.details.original
  return record.field
    .split(',')
    .map((name) => t.docView.details.fields[name as keyof Messages['docView']['details']['fields']] ?? name)
    .join(t.docView.details.listSeparator)
}
