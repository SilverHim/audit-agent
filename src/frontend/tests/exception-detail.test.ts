// 例外明细按字段显示 (lib/exception-detail, 需求 R350), bun test 运行.
// PLN07 取 Xero 演示 PLN-07 的 090 一条 (X-PLN-07-1-A01-1, 按后端 test_xero_programs 的流程取出; 数据库的 numeric 列
// 序列化为字符串); REV08 取复核页无归因任务的失败原因 (后端 test_presentation 的 REV-08 例外明细)
import { describe, expect, test } from 'bun:test'
import { detailRows } from '../src/lib/exception-detail'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const PLN07 = {
  account_name: 'Business Bank Account',
  basis: 'balance_sheet',
  current_cents: '-1090044',
  prior_cents: '-870320',
  opening_cents: '-870320',
  change_pct: '-0.25246346171523117934',
}
const REV08 = { posting_date: '2026-06-28', signed_date: '2026-07-03' }

describe('例外明细', () => {
  test('_cents 按金额 (数字串也按数), _pct 按百分比一位小数, basis 的取值换成文案', () => {
    expect(detailRows(PLN07, en.exceptionDetail)).toEqual([
      { key: 'account_name', label: 'Account', text: 'Business Bank Account' },
      { key: 'basis', label: 'Compared with', text: 'Balance sheet, opening' },
      { key: 'current_cents', label: 'Current', text: 'AUD -10,900.44' },
      { key: 'prior_cents', label: 'Prior period', text: 'AUD -8,703.20' },
      { key: 'opening_cents', label: 'Comparison base', text: 'AUD -8,703.20' },
      { key: 'change_pct', label: 'Change', text: '-25.2%' },
    ])
  })

  test('中文界面取中文标签与取值文案', () => {
    expect(detailRows(PLN07, zh.exceptionDetail)).toEqual([
      { key: 'account_name', label: '科目', text: 'Business Bank Account' },
      { key: 'basis', label: '比较口径', text: '资产负债类，期初' },
      { key: 'current_cents', label: '本期', text: 'AUD -10,900.44' },
      { key: 'prior_cents', label: '上期', text: 'AUD -8,703.20' },
      { key: 'opening_cents', label: '比较基数', text: 'AUD -8,703.20' },
      { key: 'change_pct', label: '变动', text: '-25.2%' },
    ])
  })

  test('复核页无归因任务的失败原因 (REV-08 的明细): 日期按文本, 中英界面取各自的标签', () => {
    expect(detailRows(REV08, en.exceptionDetail)).toEqual([
      { key: 'posting_date', label: 'Posting date', text: '2026-06-28' },
      { key: 'signed_date', label: 'Signed date', text: '2026-07-03' },
    ])
    expect(detailRows(REV08, zh.exceptionDetail).map((row) => row.label)).toEqual(['记账日期', '签收日期'])
  })

  test('对象逐个字段展开, 元素是对象的数组各自展开, 其余数组以逗号连接, 空数组为 "—"', () => {
    const detail = {
      matched: { invoice_amount_cents: 715000, received_cents: '385000' },
      paired_invoices: [{ invoice_id: 'SI-ORC1003' }, { invoice_id: 'SI-ORC1008' }],
      bank_transaction_ids: ['BT-090-0124', 'BT-090-0085'],
      line_set: [],
    }
    expect(detailRows(detail, en.exceptionDetail)).toEqual([
      {
        key: 'matched',
        label: 'matched',
        rows: [
          { key: 'invoice_amount_cents', label: 'Invoice amount', text: 'AUD 7,150.00' },
          { key: 'received_cents', label: 'Received', text: 'AUD 3,850.00' },
        ],
      },
      {
        key: 'paired_invoices',
        label: 'paired_invoices',
        rows: [
          {
            key: 'paired_invoices.1',
            label: 'paired_invoices 1',
            rows: [{ key: 'invoice_id', label: 'invoice_id', text: 'SI-ORC1003' }],
          },
          {
            key: 'paired_invoices.2',
            label: 'paired_invoices 2',
            rows: [{ key: 'invoice_id', label: 'invoice_id', text: 'SI-ORC1008' }],
          },
        ],
      },
      { key: 'bank_transaction_ids', label: 'Bank transactions', text: 'BT-090-0124, BT-090-0085' },
      { key: 'line_set', label: 'line_set', text: '—' },
    ])
  })

  test('未知字段退回字段名按文本; 取值文案只换所属字段的; 空值为 "—"; 不是数的 _cents 照原文; 空的明细没有行', () => {
    const detail = {
      rule_note: 'over threshold',
      statement: 'balance_sheet',
      flagged: true,
      net_cents: null,
      odd_cents: 'n/a',
    }
    expect(detailRows(detail, en.exceptionDetail)).toEqual([
      { key: 'rule_note', label: 'rule_note', text: 'over threshold' },
      { key: 'statement', label: 'statement', text: 'balance_sheet' },
      { key: 'flagged', label: 'flagged', text: 'yes' },
      { key: 'net_cents', label: 'net_cents', text: '—' },
      { key: 'odd_cents', label: 'odd_cents', text: 'n/a' },
    ])
    expect(detailRows({}, en.exceptionDetail)).toEqual([])
  })
})
