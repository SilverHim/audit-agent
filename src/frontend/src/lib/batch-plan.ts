import type { BatchIssue, BatchItem, DateFormat, FieldSource, TableKind, TablePlan, TableSchema } from '@/lib/data-api'
import type { Messages } from '@/locales/en'

// 智能导入确认单的纯函数 (不含界面): 问题的文案, 哪些项勾了也导不进去, 调整表格项时规整方案与列对应界面之间的换算

// 跳过规则与分组标题: 按一列的文本认出一类行
export type RowRule = NonNullable<TablePlan['skip']>[number]
export type GroupTitle = NonNullable<TablePlan['group_title']>
// 调整界面里的一条跳过规则: id 为打开调整时生成的本地标识 (列表的 key), 不进方案
export type SkipRule = { id: string; rule: RowRule }

let ruleCount = 0
function ruleId(): string {
  ruleCount += 1
  return `skip-${ruleCount}`
}

export type FormatDate = (value: string | null | undefined) => string

// 列对应: 字段 → 列名, 为空表示不导入
export type Mapping = Record<string, string | null>

// 列对应里 "沿用规整方案的取法" 的取值 (借贷两列, 分组标题, 拆正负, 取值映射等); 不会是列名
export const RULE = '@@plan-rule'

// 换数据类型时按列名重新对应: 先取后端建议, 再按字段名与列名 (忽略大小写与分隔符) 相同的配对
export function autoMap(schema: TableSchema | undefined, columns: string[], suggested: Mapping): Mapping {
  const plain = (value: string) => value.toLowerCase().replace(/[^a-z0-9]/g, '')
  const result: Mapping = {}
  for (const field of schema?.fields ?? []) {
    const hint = suggested[field.name]
    result[field.name] =
      hint && columns.includes(hint) ? hint : (columns.find((column) => plain(column) === plain(field.name)) ?? null)
  }
  return result
}

const CANNOT_IMPORT = new Set(['plan_invalid', 'parse_failed', 'already_registered'])

// 勾了也导不进去的项 (确认时后端拒绝或不处理): 不是账表, 方案套不上, 有解析失败, 文件没有类型, 之前已登记
export function blocked(item: BatchItem): boolean {
  if ((item.issues ?? []).some((issue) => CANNOT_IMPORT.has(issue.code))) return true
  return item.item_kind === 'table' ? !item.plan?.kind : !item.document_type
}

export function itemNumber(id: string): string {
  return id.slice(id.lastIndexOf('-') + 1)
}

export function periodText(start: unknown, end: unknown, t: Messages, formatDate: FormatDate): string {
  const from = typeof start === 'string' ? start : null
  const to = typeof end === 'string' ? end : null
  if (from && to) return `${formatDate(from)} – ${formatDate(to)}`
  if (to) return t.batch.asAt(formatDate(to))
  if (from) return `${formatDate(from)} –`
  return '—'
}

export function issueText(issue: BatchIssue, t: Messages, lang: 'zh' | 'en', formatDate: FormatDate): string {
  const p = issue.params ?? {}
  const text = t.batch.issues
  const table = (kind: unknown) => t.data.tables[String(kind)] ?? String(kind)
  switch (issue.code) {
    case 'plan_invalid':
      return text.plan_invalid(String(p.message ?? p.code ?? ''))
    case 'parse_failed':
      return text.parse_failed(Number(p.count ?? 0))
    case 'company_mismatch':
      return text.company_mismatch(String(p.company ?? ''), String(p.expected ?? ''))
    case 'period_mismatch':
      return text.period_mismatch(
        periodText(p.period_start, p.period_end, t, formatDate),
        periodText(p.expected_start, p.expected_end, t, formatDate),
      )
    case 'duplicate_table': {
      const others = (Array.isArray(p.items) ? p.items : []).map((id) => t.batch.itemNo(itemNumber(String(id))))
      return text.duplicate_table(table(p.kind), others.join(lang === 'zh' ? '、' : ', '))
    }
    case 'replaces_data':
      return text.replaces_data(table(p.kind), Number(p.existing ?? 0))
    case 'unclassified':
      return text.unclassified[String(p.step ?? 'classify')] ?? text.unclassified.classify
    default:
      return text[issue.code]
  }
}

export type AmountUnit = NonNullable<TablePlan['amount_unit']>

export type Draft = {
  kind: TableKind | ''
  headerRow: number
  dateFormat: DateFormat
  amountUnit: AmountUnit | ''
  mapping: Mapping
  skip: SkipRule[] // 跳过规则: 可删, 可把列改选为当前表头里的列
  groupTitle: GroupTitle | null // 分组标题: 同上; 删了之后取分组标题的字段一并不再沿用
}

export function plainColumn(source: FieldSource): boolean {
  return Boolean(source.column) && !source.part && !Object.keys(source.values ?? {}).length
}

export function describeSource(source: FieldSource, t: Messages): string {
  const text = t.batch.sources
  let base = source.column ?? ''
  if (source.debit && source.credit) base = text.debitCredit(source.debit, source.credit)
  else if (source.group) base = text.group(source.group)
  else if (source.column && source.part) base = text[source.part](source.column)
  const mapped = Object.keys(source.values ?? {}).length
  return mapped ? text.values(base, mapped) : base
}

// 方案里不是简单取一列的字段 (字段 → 说明): 借贷两列, 分组标题, 拆正负, 取值映射; 试算表按期分列时为期间与余额.
// groups 为假 (分组标题已删) 时取分组标题的字段不再列出
export function planRules(plan: TablePlan, t: Messages, groups = true): Record<string, string> {
  const rules: Record<string, string> = {}
  for (const [name, source] of Object.entries(plan.fields ?? {})) {
    if (!groups && source.group) continue
    if (!plainColumn(source)) rules[name] = describeSource(source, t)
  }
  if (plan.periods?.length) {
    const text = plan.periods
      .map((period) => `${t.batch.sources.period[period.period] ?? period.period} ${describeSource(period.balance, t)}`)
      .join('; ')
    rules.period = t.batch.sources.periods(text)
    rules.balance_cents = rules.period
  }
  return rules
}

// 沿用的规则用到的列 (不算未对应): 字段规则, 按期分列, 跳过规则与分组标题
export function ruleColumns(plan: TablePlan, draft: Draft): string[] {
  const sources = Object.entries(plan.fields ?? {})
    .filter(([name]) => draft.mapping[name] === RULE)
    .map(([, source]) => source)
  if (draft.mapping.period === RULE) sources.push(...(plan.periods ?? []).map((period) => period.balance))
  const columns = sources.flatMap((source) => [source.column, source.debit, source.credit])
  columns.push(...draft.skip.map((skip) => skip.rule.column), draft.groupTitle?.column)
  return columns.filter((column): column is string => Boolean(column))
}

// 指向当前表头里没有的列的跳过规则与分组标题 (方案套不上的原因之一), 界面标红
export function missingRuleColumns(draft: Draft, columns: string[]): Set<string> {
  const used = [...draft.skip.map((skip) => skip.rule.column), ...(draft.groupTitle ? [draft.groupTitle.column] : [])]
  return new Set(used.filter((column) => !columns.includes(column)))
}

// 对应的列不在当前表头里的字段 (方案套不上的原因之一), 界面标红: 取一列的字段看它的列; 沿用方案规则的字段看规则
// 用到的各列 (借贷两列, 拆正负与取值映射的列; 按期分列时期间与余额看各期余额的列)
export function missingFieldColumns(plan: TablePlan, draft: Draft, columns: string[]): Set<string> {
  const absent = (names: (string | null | undefined)[]) => names.some((name) => name && !columns.includes(name))
  const missing = new Set<string>()
  for (const [name, value] of Object.entries(draft.mapping)) {
    if (value === RULE) {
      const source = plan.fields?.[name]
      if (source && absent([source.column, source.debit, source.credit])) missing.add(name)
    } else if (value && !columns.includes(value)) missing.add(name)
  }
  const periods = (plan.periods ?? []).flatMap((period) => [
    period.balance.column,
    period.balance.debit,
    period.balance.credit,
  ])
  if (draft.mapping.period === RULE && absent(periods)) {
    missing.add('period')
    missing.add('balance_cents')
  }
  return missing
}

// 调整的初值: 预填方案. 取一列的字段照方案的列名, 列不在当前表头里也保留 (界面标红), 改表头行读列名再改回来,
// 对应全部恢复
export function draftOf(item: BatchItem, t: Messages): Draft {
  const plan: TablePlan = item.plan ?? { kind: null }
  const rules = planRules(plan, t)
  const mapping: Mapping = {}
  for (const [name, source] of Object.entries(plan.fields ?? {})) {
    mapping[name] = name in rules ? RULE : (source.column ?? null)
  }
  if (plan.periods?.length) {
    mapping.period = RULE
    mapping.balance_cents = RULE
  }
  return {
    kind: plan.kind ?? '',
    headerRow: plan.header_row ?? 1,
    dateFormat: plan.date_format ?? 'dmy',
    amountUnit: plan.amount_unit ?? '',
    mapping,
    skip: (plan.skip ?? []).map((rule) => ({ id: ruleId(), rule })),
    groupTitle: plan.group_title ?? null,
  }
}

// 按调整结果出新方案: 换了表种类时字段规则不沿用; 不是账表时不对应字段; 跳过规则与分组标题取调整后的 (删了的
// 分组标题, 取它的字段也不再沿用); 去重照留
export function planOf(base: TablePlan, draft: Draft): TablePlan {
  const common = {
    ...base,
    header_row: draft.headerRow,
    date_format: draft.dateFormat,
    amount_unit: draft.amountUnit || null,
    skip: draft.skip.map((skip) => skip.rule),
    group_title: draft.groupTitle,
  }
  if (!draft.kind) return { ...common, kind: null, fields: {}, periods: [] }
  const same = draft.kind === base.kind
  const fields: Record<string, FieldSource> = {}
  for (const [name, value] of Object.entries(draft.mapping)) {
    const kept = base.fields?.[name]
    if (value === RULE) {
      if (same && kept && (draft.groupTitle || !kept.group)) fields[name] = kept
    } else if (value) fields[name] = { column: value }
  }
  const periods =
    same && draft.mapping.period === RULE && draft.mapping.balance_cents === RULE ? (base.periods ?? []) : []
  return { ...common, kind: draft.kind, fields, periods }
}
