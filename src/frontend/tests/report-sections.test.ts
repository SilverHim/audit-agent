// 审计报告各段的段名只显示一次 (lib/report-sections, 需求 R352), bun test 运行.
// 正文取 RPT-03 固定输出与报告模板的写法: 意见段等首行是段名, 签名块不带段名
import { describe, expect, test } from 'bun:test'
import { reportPart } from '../src/lib/report-sections'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const OPINION = 'Opinion\n\nWe have audited the financial report of the Company.\n\nIn our opinion, the report is fair.'
const SIGNATURE = 'Jordan Ellis\nTessellate Audit Partners'

describe('报告段名', () => {
  test.each([
    ['en', en.reportSections],
    ['zh', zh.reportSections],
  ])('%s: 自带段名的段取正文首行作段名, 正文不再重复它', (_, labels) => {
    expect(reportPart('opinion', OPINION, labels.opinion)).toEqual({
      heading: 'Opinion',
      body: 'We have audited the financial report of the Company.\n\nIn our opinion, the report is fair.',
    })
  })

  test('签名块不带段名: 用界面的段名, 正文原样', () => {
    expect(reportPart('signature', SIGNATURE, zh.reportSections.signature)).toEqual({
      heading: '签字',
      body: SIGNATURE,
    })
  })

  test('正文为空: 用界面的段名, 正文为 "—" (自带段名的段也一样)', () => {
    expect(reportPart('other_legal_and_regulatory', '', en.reportSections.other_legal_and_regulatory)).toEqual({
      heading: 'Other legal and regulatory requirements',
      body: '—',
    })
    expect(reportPart('basis_for_opinion', '', en.reportSections.basis_for_opinion)).toEqual({
      heading: 'Basis for opinion',
      body: '—',
    })
  })
})
