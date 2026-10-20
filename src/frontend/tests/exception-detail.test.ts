// 例外明细按字段显示 (lib/exception-detail, 需求 R350), bun test 运行.
// 明细取 Xero 演示 PLN-07 的一条波动例外 (数据库的 numeric 列序列化为字符串), 另加嵌套与未知字段
import { describe, expect, test } from 'bun:test'
import { detailRows } from '../src/lib/exception-detail'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const PLN07 = {
  account_name: 'Business Bank Account',
  basis: 'balance_sheet',
  opening_cents: -457222,
  current_cents: '-1090044',
  change_pct: '1.3840979218235426',
}

describe('例外明细', () => {
  test('_cents 按金额 (数或数字串), _pct 按百分比一位小数, 认得的取值换成文案', () => {
    expect(detailRows(PLN07, en.exceptionDetail)).toEqual([
      { key: 'account_name', label: 'Account', text: 'Business Bank Account' },
      { key: 'basis', label: 'Compared with', text: 'Balance sheet, opening' },
      { key: 'opening_cents', label: 'Comparison base', text: 'AUD -4,572.22' },
      { key: 'current_cents', label: 'Current', text: 'AUD -10,900.44' },
      { key: 'change_pct', label: 'Change', text: '138.4%' },
    ])
  })

  test('中文界面取中文标签与取值文案', () => {
    const [account, basis] = detailRows(PLN07, zh.exceptionDetail)
    expect([account?.label, basis?.label, basis && 'text' in basis ? basis.text : null]).toEqual([
      '科目',
      '比较口径',
      '资产负债类，期初',
    ])
  })

  test('对象逐个字段展开, 元素是对象的数组各自展开, 其余数组以逗号连接', () => {
    const detail = {
      matched: { invoice_amount_cents: 715000, received_cents: '385000' },
      paired_invoices: [{ invoice_id: 'SI-ORC1003' }, { invoice_id: 'SI-ORC1008' }],
      bank_transaction_ids: ['BT-090-0124', 'BT-090-0085'],
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
    ])
  })

  test('未知字段退回字段名按文本显示; 空值显示 "—"; 不是数的 _cents 照原文; 空的明细没有行', () => {
    const detail = { rule_note: 'over threshold', flagged: true, net_cents: null, odd_cents: 'n/a' }
    expect(detailRows(detail, en.exceptionDetail)).toEqual([
      { key: 'rule_note', label: 'rule_note', text: 'over threshold' },
      { key: 'flagged', label: 'flagged', text: 'yes' },
      { key: 'net_cents', label: 'net_cents', text: '—' },
      { key: 'odd_cents', label: 'odd_cents', text: 'n/a' },
    ])
    expect(detailRows({}, en.exceptionDetail)).toEqual([])
  })
})
