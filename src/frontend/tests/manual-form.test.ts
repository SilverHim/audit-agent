// 人工程序详情页的表单换算 (lib/manual-api 的 resultFields, parseDetails, manualRecordBody) 的测试, bun test 运行
// (不在 tsconfig 的 src 里, 不经 tsc). fixtures/manual-tst-m01.json 是后端 GET /manual-tasks 对测试程序 TST-M01
// (取得签字的管理层声明书, 结果模型 TstLetter, 至少 2 条证据; 后端 tests/test_manual 的装配) 给的一项, 文件为
// Numbat 合成数据集的登记加审计师上传的声明书; created_at 取整到秒. 其余 schema 为手写的 Pydantic 写法.
import { describe, expect, test } from 'bun:test'
import type { ManualTaskView } from '../src/client/types.gen'
import { emptyManualDraft, manualRecordBody, parseDetails, resultFields } from '../src/lib/manual-api'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'
import fixture from './fixtures/manual-tst-m01.json'

const VIEW = fixture as ManualTaskView
const LETTER = 'DOC-0a1b2c3d4e5f' // 声明书, 2 页, internal_original, 作证据为 voucher
const CONTRACT = 'DOC-3e7fb18af92a' // 合同 CON-001.pdf, 3 页, external_copy, 作证据为 contract

// 按 Pydantic 的 model_json_schema 写法: 枚举在 $defs 里经 $ref 引用, Optional 为 anyOf 带 null; $ref 与
// Optional[$ref] 的字段 Pydantic 不生成 title
const MIXED = {
  $defs: { Basis: { enum: ['cash', 'accrual'], title: 'Basis', type: 'string' } },
  properties: {
    count: { title: 'Count', type: 'integer' },
    rate: { title: 'Rate', type: 'number' },
    agreed: { title: 'Agreed', type: 'boolean' },
    audit_basis: { $ref: '#/$defs/Basis' },
    prior_basis: { anyOf: [{ $ref: '#/$defs/Basis' }, { type: 'null' }], default: null },
    note: { anyOf: [{ type: 'string' }, { type: 'null' }], default: null, title: 'Note' },
    items: { items: { type: 'string' }, title: 'Items', type: 'array' },
  },
  required: ['count', 'rate', 'agreed', 'audit_basis', 'items'],
  title: 'Mixed',
  type: 'object',
}

describe('resultFields', () => {
  test('TstLetter 的两个字段: 日期与文字, 都必填', () => {
    expect(resultFields(VIEW.result_schema)).toEqual([
      { name: 'signed_on', title: 'Signed On', kind: 'date', required: true, options: [] },
      { name: 'signatory', title: 'Signatory', kind: 'string', required: true, options: [] },
    ])
  })

  test('整数, 小数, 勾选, 引用的枚举 (含可空的), 可空的文字各得对应的输入, 其余 (列表) 为 JSON', () => {
    expect(resultFields(MIXED).map((field) => [field.name, field.kind, field.required, field.options])).toEqual([
      ['count', 'integer', true, []],
      ['rate', 'number', true, []],
      ['agreed', 'boolean', true, []],
      ['audit_basis', 'enum', true, ['cash', 'accrual']],
      ['prior_basis', 'enum', false, ['cash', 'accrual']],
      ['note', 'string', false, []],
      ['items', 'json', true, []],
    ])
  })

  test('没有 title 的 $ref 与 Optional[$ref] 字段一样按字段名起标题, 不取枚举类名', () => {
    expect(resultFields(MIXED).map((field) => field.title)).toEqual([
      'Count',
      'Rate',
      'Agreed',
      'Audit Basis',
      'Prior Basis',
      'Note',
      'Items',
    ])
  })

  test('没有结果模型时没有字段', () => {
    expect(resultFields(null)).toEqual([])
  })
})

describe('parseDetails', () => {
  const fields = resultFields(MIXED)
  const values = { count: '3', rate: '0.5', agreed: false, audit_basis: 'cash', note: '  ', items: '["a", "b"]' }

  test('按类型换成 details: 数字转数, JSON 解析, 勾选总带, 没填的选填字段不带', () => {
    expect(parseDetails(fields, values, zh)).toEqual({
      count: 3,
      rate: 0.5,
      agreed: false,
      audit_basis: 'cash',
      items: ['a', 'b'],
    })
  })

  test('必填没填, 整数写成小数, JSON 写错各报对应的错误', () => {
    expect(parseDetails(fields, { ...values, audit_basis: '' }, zh)).toBe(zh.manualPage.errors.required('Audit Basis'))
    expect(parseDetails(fields, { ...values, count: '2.5' }, zh)).toBe(zh.manualPage.errors.number('Count'))
    expect(parseDetails(fields, { ...values, rate: 'abc' }, zh)).toBe(zh.manualPage.errors.number('Rate'))
    expect(parseDetails(fields, { ...values, items: '[a' }, zh)).toBe(zh.manualPage.errors.json('Items'))
  })
})

describe('manualRecordBody', () => {
  const draft = {
    ...emptyManualDraft(),
    conclusion: 'Signed letter obtained, dated 23 October 2026',
    reason: 'Letter received from the CFO',
    performedOn: '2026-10-23',
    evidence: [
      { documentId: LETTER, page: 2 },
      { documentId: CONTRACT, page: 3 },
    ],
    values: { signed_on: '2026-10-23', signatory: 'Alex Morgan' },
  }

  test('TST-M01: manual_result 带执行日期, 两条证据 (种类按文件取 evidence_types, 可靠性取文件登记) 与 details', () => {
    const letter = {
      type: 'voucher',
      source_type: 'document',
      source_id: LETTER,
      locator: 'representation_letter.pdf',
      page: 2,
      reliability: 'internal_original',
    }
    const contract = {
      type: 'contract',
      source_type: 'document',
      source_id: CONTRACT,
      locator: 'CON-001.pdf',
      page: 3,
      reliability: 'external_copy',
    }
    expect(manualRecordBody(VIEW, draft, zh)).toEqual({
      task_id: 'T-RUN-TST-M01-1-v1',
      target_version: 1,
      decision: 'manual_result',
      reason: 'Letter received from the CFO',
      manual_result: {
        conclusion: 'Signed letter obtained, dated 23 October 2026',
        performed_on: '2026-10-23',
        evidence: [letter, contract],
        details: { signed_on: '2026-10-23', signatory: 'Alex Morgan' },
      },
    })
  })

  test('理由选填: 空时照发空串 (需求 R360)', () => {
    expect(manualRecordBody(VIEW, { ...draft, reason: ' ' }, zh)).toMatchObject({
      decision: 'manual_result',
      reason: '',
    })
  })

  test('缺结论, 执行日期, 证据少于 min_evidence (不在可选文件里的不算) 或结构化结果缺字段时报错', () => {
    expect(manualRecordBody(VIEW, { ...draft, conclusion: ' ' }, zh)).toBe(zh.errors.conclusionRequired)
    expect(manualRecordBody(VIEW, { ...draft, performedOn: '' }, zh)).toBe(zh.manualPage.errors.performedOn)
    const one = [draft.evidence[0], { documentId: 'DOC-ffffffffffff', page: 1 }]
    expect(manualRecordBody(VIEW, { ...draft, evidence: one }, zh)).toBe(zh.manualPage.errors.evidence(2))
    const partial = { signed_on: '2026-10-23', signatory: '' }
    expect(manualRecordBody(VIEW, { ...draft, values: partial }, zh)).toBe(zh.manualPage.errors.required('Signatory'))
  })

  test('列出的文件没有证据种类时报错, 不兜底成别的种类', () => {
    const { [CONTRACT]: _, ...types } = VIEW.evidence_types
    expect(() => manualRecordBody({ ...VIEW, evidence_types: types }, draft, zh)).toThrow(`${CONTRACT} 没有证据种类`)
  })
})

describe('证据要求的提示', () => {
  test('至少要几页时写出页数; 不要求证据 (min_evidence 为 0, 如 FIN-08) 时写证据可不附, 不写 "至少 0 页"', () => {
    for (const t of [en, zh]) {
      expect(t.manualPage.minEvidence(2)).toContain('2')
      expect(t.manualPage.minEvidence(0)).not.toContain('0')
    }
    expect(en.manualPage.minEvidence(1)).toBe('Choose at least 1 page from registered documents.')
  })
})

test('更正意向登记自动记录执行日期与说明, 不要求另填通用人工表单', () => {
  const view = { ...fixture, request: { ...fixture.request, result_model: 'ManagementResponses', min_evidence: 0 } }
  const result = manualRecordBody(view, emptyManualDraft(), zh, {
    responses: [{ misstatement_id: 'M-1', base_version: 1, agrees_to_correct: true }],
  })
  expect(typeof result).not.toBe('string')
  if (typeof result === 'string') throw new Error(result)
  expect(result.manual_result?.conclusion).toBe(zh.fin08.recorded)
  expect(result.manual_result?.performed_on).toMatch(/^\d{4}-\d{2}-\d{2}$/)
})
