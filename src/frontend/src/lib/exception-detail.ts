import { formatAud } from '@/lib/money'
import type { Messages } from '@/locales/en'

// 例外明细 (AuditException.detail, 即规则 SQL 的其余输出列) 按字段显示 (需求 R350): 以 _cents 结尾的按全站金额
// 格式, 以 _pct 结尾的按百分比 (一位小数), 其余按文本 (认得的取值换成文案). 数据库的 numeric 列序列化后可能是
// 字符串, 一并按数解析. 字段名取文案里的标签, 没有的退回字段名. 对象逐个字段展开; 数组的元素以逗号连接, 元素是
// 对象时各自展开

export type DetailRow =
  | { key: string; label: string; text: string } // 一个字段
  | { key: string; label: string; rows: DetailRow[] } // 对象: 下一层的字段

type Labels = Messages['exceptionDetail']

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asNumber(value: unknown): number | null {
  const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value
  return typeof number === 'number' && Number.isFinite(number) ? number : null
}

// 一个原子值的文本: 金额与比例按字段名后缀, 布尔取是否, 认得的字符串取值换成文案
function valueText(key: string, value: unknown, labels: Labels): string {
  if (value === null || value === undefined) return '—'
  const number = asNumber(value)
  if (key.endsWith('_cents') && number !== null) return formatAud(number)
  if (key.endsWith('_pct') && number !== null) return `${(number * 100).toFixed(1)}%`
  if (typeof value === 'boolean') return value ? labels.yes : labels.no
  if (typeof value === 'string') return labels.values[value] ?? value
  return String(value)
}

function row(key: string, value: unknown, labels: Labels): DetailRow {
  const label = labels.fields[key] ?? key
  if (isObject(value)) return { key, label, rows: detailRows(value, labels) }
  if (Array.isArray(value) && value.some(isObject)) {
    const items = value.map((item, n): DetailRow => {
      const at = { key: `${key}.${n + 1}`, label: `${label} ${n + 1}` }
      return isObject(item) ? { ...at, rows: detailRows(item, labels) } : { ...at, text: valueText(key, item, labels) }
    })
    return { key, label, rows: items }
  }
  const text = Array.isArray(value)
    ? value.map((item) => valueText(key, item, labels)).join(', ')
    : valueText(key, value, labels)
  return { key, label, text }
}

// 明细的各行, 顺序同 detail 的字段顺序; 空的明细为空列表
export function detailRows(detail: Record<string, unknown>, labels: Labels): DetailRow[] {
  return Object.entries(detail).map(([key, value]) => row(key, value, labels))
}
