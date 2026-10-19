// 文件补登记表单 (lib/document-details.ts) 的测试, bun test 运行: 请求体只带改了的项, 被拒的错误码文案, 历史的说明.
import { describe, expect, test } from 'bun:test'
import type { DocumentRef, ExtractionRecord } from '../src/client/types.gen'
import { detailsBody, detailsErrorText, detailsForm, historyFields, parseRelatedIds } from '../src/lib/document-details'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const DOC: DocumentRef = {
  id: 'DOC-0db690a7c05e',
  type: 'invoice',
  file_name: 'smart_agency_invoice_SM0195-70135.pdf',
  sha256: '0db690a7c05e',
  page_count: 1,
  reliability: 'internal_copy',
  status: 'classified',
  uploaded_at: '2026-10-17T00:00:00Z',
  document_date: null,
  related_ids: [],
}

const rejected = (code: string, message?: string) => ({ detail: { code, message } })

describe('detailsBody', () => {
  test('只带改了的项, 理由去空白; 关联对象按逗号, 中文逗号或换行分隔', () => {
    const form = {
      ...detailsForm(DOC),
      reliability: 'external_copy' as const,
      documentDate: '2026-09-05',
      relatedIds: 'XJ-548，BT-090-0097\n',
      reason: ' 核对发票原件 ',
    }
    expect(detailsBody(DOC, form)).toEqual({
      reason: '核对发票原件',
      reliability: 'external_copy',
      document_date: '2026-09-05',
      related_ids: ['XJ-548', 'BT-090-0097'],
    })
  })

  test('都没改为 null (前端提示至少改一项, 不发请求); 清空日期不算改', () => {
    const dated = { ...DOC, document_date: '2026-09-05', related_ids: ['XJ-548'] }
    expect(detailsBody(dated, { ...detailsForm(dated), reason: '没改' })).toBeNull()
    expect(detailsBody(dated, { ...detailsForm(dated), documentDate: '', reason: '清空' })).toBeNull()
    expect(detailsBody(dated, { ...detailsForm(dated), relatedIds: '', reason: '去掉关联' })).toEqual({
      reason: '去掉关联',
      related_ids: [],
    })
  })

  test('parseRelatedIds 去空项', () => {
    expect(parseRelatedIds(' , XJ-1 ,,\nXJ-2 ')).toEqual(['XJ-1', 'XJ-2'])
  })
})

describe('detailsErrorText', () => {
  test.each([
    ['MISSING_FIELD', undefined, '请填写补登记的理由。'],
    ['INVALID_VALUE', '文件 x.pdf 的关联对象重复: XJ-1', '取值不合格，未保存（文件 x.pdf 的关联对象重复: XJ-1）'],
    ['ENGAGEMENT_LOCKED', 'ENG-1', '项目已签字，只能查看。'],
    ['ENGAGEMENT_BUSY', 'ENG-1 正在执行', '项目正在处理中，请稍后再试。'],
  ])('%s', (code, message, text) => {
    expect(detailsErrorText(rejected(code, message), zh)).toBe(text)
  })

  test('附后端说明的格式随界面语言 (中文全角括号, 英文半角)', () => {
    expect(detailsErrorText(rejected('INVALID_VALUE', 'XJ-1'), en)).toBe('The values could not be saved (XJ-1)')
  })
})

describe('historyFields', () => {
  const record = (field: string | null): ExtractionRecord => ({
    target_type: 'document',
    target_id: DOC.id,
    version: 2,
    data: {},
    field,
    changed_by: 'U-1',
    changed_at: '2026-10-19T00:00:00Z',
  })

  test('改了的项按后端的顺序; 第一版为登记时', () => {
    expect(historyFields(record('reliability,document_date,related_ids'), zh)).toBe('可靠性、文件日期、关联对象')
    expect(historyFields(record('related_ids'), en)).toBe('related records')
    expect(historyFields(record(null), en)).toBe('As registered')
  })
})
