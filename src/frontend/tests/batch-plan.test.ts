// 确认单调整界面的换算 (lib/batch-plan.ts) 的测试, bun test 运行 (不在 tsconfig 的 src 里, 不经 tsc).
// 数据是合成的: 列名照 Xero 网页导出的写法, 不含取值.
import { describe, expect, test } from 'bun:test'
import type { BatchItem, TablePlan } from '../src/client/types.gen'
import { draftOf, missingFieldColumns, missingRuleColumns, planOf } from '../src/lib/batch-plan'
import { zh } from '../src/locales/zh'

const TB_COLUMNS = ['Account', 'Account Code', 'Debit - Year to date', 'Credit - Year to date', '30 June 2025']
const TB_PLAN: TablePlan = {
  kind: 'trial_balance',
  header_row: 5,
  skip: [{ column: 'Account', pattern: '^Total' }],
  fields: { account_code: { column: 'Account Code' } },
  periods: [
    { period: 'current', balance: { debit: 'Debit - Year to date', credit: 'Credit - Year to date' } },
    { period: 'prior', balance: { column: '30 June 2025' } },
  ],
  date_format: 'dmy',
}
const INVOICE_COLUMNS = ['Invoice Number', 'Contact', 'Invoice Date', 'Total']
const INVOICE_PLAN: TablePlan = {
  kind: 'sales_invoices',
  header_row: 5,
  group_title: { column: 'Invoice Number', pattern: '^(?P<number>INV-\\d+)$' },
  fields: {
    invoice_no: { group: 'number' },
    customer: { column: 'Contact' },
    invoice_date: { column: 'Invoice Date' },
    posting_date: { column: 'Invoice Date' },
    amount_cents: { column: 'Total' },
  },
  date_format: 'dmy',
}

function item(plan: TablePlan, columns: string[]): BatchItem {
  return {
    id: 'BAT-test-1',
    file_name: 'export.xlsx',
    document_id: 'DOC-1',
    item_kind: 'table',
    sheet: 'S',
    plan,
    columns,
  }
}

// 界面上的 "按第 n 行读列名": 用当前草稿出方案存下, 后端按新表头重算列 (这里直接给新列), 再从新项重建草稿
function reread(current: BatchItem, headerRow: number, columns: string[]): BatchItem {
  const plan = planOf(current.plan as TablePlan, { ...draftOf(current, zh), headerRow })
  return item(plan, columns)
}

describe('改表头行读列名的往返', () => {
  test('试算表: 改到第 6 行 (列都不在) 再改回第 5 行, 字段, 分期与跳过规则都恢复', () => {
    const start = item(TB_PLAN, TB_COLUMNS)
    const moved = reread(start, 6, ['Demo Company (AU)'])
    const draft = draftOf(moved, zh)
    expect(draft.mapping.account_code).toBe('Account Code') // 列不在当前表头里也保留
    expect([...missingFieldColumns(moved.plan as TablePlan, draft, moved.columns ?? [])].sort()).toEqual([
      'account_code',
      'balance_cents',
      'period',
    ])
    expect([...missingRuleColumns(draft, moved.columns ?? [])]).toEqual(['Account'])
    const back = reread(moved, 5, TB_COLUMNS)
    expect(back.plan?.fields).toEqual(TB_PLAN.fields)
    expect(back.plan?.periods).toEqual(TB_PLAN.periods)
    expect(back.plan?.skip).toEqual(TB_PLAN.skip)
    expect(missingFieldColumns(back.plan as TablePlan, draftOf(back, zh), TB_COLUMNS).size).toBe(0)
  })

  test('销售发票: 往返后取一列的 4 个字段与取分组标题的发票号都还在', () => {
    const start = item(INVOICE_PLAN, INVOICE_COLUMNS)
    const moved = reread(start, 8, ['Contact', 'Something else'])
    const draft = draftOf(moved, zh)
    expect([...missingFieldColumns(moved.plan as TablePlan, draft, moved.columns ?? [])].sort()).toEqual([
      'amount_cents',
      'invoice_date',
      'posting_date',
    ]) // 还在新表头里的 Contact 照常对上
    const back = reread(moved, 5, INVOICE_COLUMNS)
    expect(back.plan?.fields).toEqual(INVOICE_PLAN.fields)
    expect(back.plan?.group_title).toEqual(INVOICE_PLAN.group_title)
  })
})

test('跳过规则带打开调整时生成的本地 id, 方案里不带', () => {
  const plan = { ...TB_PLAN, skip: [...(TB_PLAN.skip ?? []), { column: 'Account Code', pattern: '^Total' }] }
  const draft = draftOf(item(plan, TB_COLUMNS), zh)
  expect(new Set(draft.skip.map((skip) => skip.id)).size).toBe(2)
  expect(planOf(plan, draft).skip).toEqual(plan.skip)
})
