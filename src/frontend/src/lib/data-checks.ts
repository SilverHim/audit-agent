import type { DataCheck } from '@/client/types.gen'
import { formatAud } from '@/lib/money'
import type { Messages } from '@/locales/en'

// 一项数据体检结果的说明文字 (按 params), 数据管理页的体检卡与确认数据已齐时的未通过项共用
export function checkDetail(check: DataCheck, t: Messages, lang: 'zh' | 'en'): string {
  const p = check.params ?? {}
  const num = (value: unknown) => Number(value ?? 0)
  switch (check.id) {
    case 'debits_equal_credits':
      return t.data.checkDetail.debits(formatAud(num(p.debit_cents)), formatAud(num(p.credit_cents)))
    case 'journals_agree_tb':
      return t.data.checkDetail.journals(num(p.accounts), Array.isArray(p.differences) ? p.differences.length : 0)
    case 'count_agrees_source':
      return t.data.checkDetail.count(
        String(p.source ?? ''),
        num(p.expected).toLocaleString('en-AU'),
        num(p.received).toLocaleString('en-AU'),
      )
    case 'mapping_complete':
      return t.data.checkDetail.mapping(num(p.mapped), num(p.total))
    case 'preparer_approver_available': {
      if (check.status === 'passed') return ''
      const fields = (Array.isArray(p.missing_fields) ? p.missing_fields : []).map(
        (field) => t.data.fieldNames[String(field)] ?? String(field),
      )
      return t.data.checkDetail.fields(fields.join(lang === 'zh' ? '与' : ' and '), String(p.source ?? 'Xero'))
    }
    case 'tb_agrees_statements':
      if (check.status === 'missing_data')
        return num(p.statements) ? t.data.checkDetail.statementsNoTb : t.data.checkDetail.statementsMissing
      // 报表数字的勾稽归 FIN-12, 还没执行时为 pending
      if (check.status === 'pending') return t.data.checkDetail.statementsPending(num(p.statements))
      return p.difference_cents ? t.data.checkDetail.statementsDiff(formatAud(num(p.difference_cents))) : ''
    default:
      return ''
  }
}
