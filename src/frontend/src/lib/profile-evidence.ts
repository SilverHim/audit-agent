// 客户画像作证据 (client_profile 指针, 需求 R128) 的显示: 取本项目采用的那一版画像, 把指针所引字段整理成行.
// 内容来自 GET /engagements/{eid}/client-profile 的 history 与 engagement_profile_version (采用版本), 不取最新版:
// 画像改出新版本后 (如同客户的其他项目更新), 本项目采用的旧版本照旧显示. 纯函数, bun test 覆盖.
import type { ClientProfileView, EvidencePointer } from '@/client/types.gen'
import { formatAud, formatPct } from '@/lib/money'

// 键值对的一项: 字段名 (humanize 后) 与显示文本
export type ProfilePair = { key: string; value: string }

// 列表字段的一行: 标量为文本, 对象按键值
export type ProfileRow = { text: string } | { pairs: ProfilePair[] }

// 所引字段的内容: 未申报 (空), 标量, 列表 (按行), 对象 (按键值)
export type ProfileField =
  | { kind: 'undeclared' }
  | { kind: 'text'; text: string }
  | { kind: 'rows'; rows: ProfileRow[] }
  | { kind: 'pairs'; pairs: ProfilePair[] }

// 弹窗的结果: 显示 (采用版本与字段), 或说明为何显示不了
export type ProfileEvidence =
  | { kind: 'shown'; version: number; field: ProfileField }
  | { kind: 'not_adopted' } // history 中没有采用的那一版
  | { kind: 'other_profile' } // 指针的 source_id 不是本项目客户的画像
  | { kind: 'unknown_field' } // locator 不是画像的字段

// 布尔值的文案 (中英由调用方给)
export type BooleanWords = { yes: string; no: string }

// 字段名的显示: 去掉 _cents 后缀, 下划线换空格 (同证据弹窗的数据行)
export function humanize(key: string): string {
  return key.replace(/_cents$/, '').replaceAll('_', ' ')
}

// 本项目采用的那一版: history 中版本等于 engagement_profile_version 的 (旧版本缺省版本号按 1)
export function adoptedProfile(view: Pick<ClientProfileView, 'history' | 'engagement_profile_version'>) {
  return view.history.find((profile) => (profile.version ?? 1) === view.engagement_profile_version)
}

export function profileEvidence(
  view: Pick<ClientProfileView, 'history' | 'engagement_profile_version'>,
  pointer: Pick<EvidencePointer, 'source_id' | 'locator'>,
  words: BooleanWords,
): ProfileEvidence {
  const profile = adoptedProfile(view)
  if (!profile) return { kind: 'not_adopted' }
  if (profile.id !== pointer.source_id) return { kind: 'other_profile' }
  if (!Object.hasOwn(profile, pointer.locator)) return { kind: 'unknown_field' }
  const value = (profile as Record<string, unknown>)[pointer.locator]
  return { kind: 'shown', version: profile.version ?? 1, field: profileField(pointer.locator, value, words) }
}

// 空值 (null, 空串, 空列表, 空对象) 即没有申报
function isEmpty(value: unknown): boolean {
  if (value == null) return true
  if (typeof value === 'string') return value.trim() === ''
  if (typeof value === 'object') return Object.keys(value).length === 0 // 空列表与空对象
  return false
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function profileField(key: string, value: unknown, words: BooleanWords): ProfileField {
  if (isEmpty(value)) return { kind: 'undeclared' }
  if (Array.isArray(value)) {
    return {
      kind: 'rows',
      rows: value.map((item) => (isRecord(item) ? { pairs: pairs(item, words) } : { text: inline(key, item, words) })),
    }
  }
  if (isRecord(value)) return { kind: 'pairs', pairs: pairs(value, words) }
  return { kind: 'text', text: inline(key, value, words) }
}

function pairs(record: Record<string, unknown>, words: BooleanWords): ProfilePair[] {
  return Object.entries(record).map(([key, value]) => ({ key: humanize(key), value: inline(key, value, words) }))
}

// 按字段名定单位: 金额 (_cents 结尾) 与比例 (名中有 pct 一节, 0 至 1)
function isCents(key: string): boolean {
  return key.endsWith('_cents')
}

function isPercent(key: string): boolean {
  return /(^|_)pct(_|$)/.test(key)
}

// 一个值的单行文本: 金额为 AUD, 比例为百分比, 布尔为是否, 空为 "—"; 嵌套的列表用分号连接, 嵌套的对象写成
// "键: 值" (键为数据的字典, 如各年的收费占比 dependency_pct_by_year, 其值沿用字段名的单位)
function inline(key: string, value: unknown, words: BooleanWords): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? words.yes : words.no
  if (typeof value === 'number') {
    if (isCents(key)) return formatAud(value)
    if (isPercent(key)) return formatPct(value)
    return String(value)
  }
  if (Array.isArray(value)) return value.length ? value.map((item) => inline(key, item, words)).join('; ') : '—'
  if (isRecord(value)) {
    const unit = isCents(key) || isPercent(key)
    const parts = Object.entries(value).map(([k, v]) => `${humanize(k)}: ${inline(unit ? key : k, v, words)}`)
    return parts.length ? parts.join(', ') : '—'
  }
  return String(value)
}
