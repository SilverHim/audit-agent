import { expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Workpaper } from '../src/client/types.gen'
import { paperText, WorkpaperPreview } from '../src/components/workpaper-preview'
import { I18nProvider } from '../src/lib/i18n'
import { downloadName } from '../src/lib/workpapers'

const paper: Workpaper = {
  id: 'WP-D-3',
  ref: 'D-3',
  area: 'D',
  procedure_id: 'REV-08',
  basis: [],
  version: 1,
  content: {
    header: {
      title: { zh: '三单匹配', en: 'Three-way match' },
      client_name: 'Synthetic client',
      engagement_id: 'ENG-TEST',
      currency: 'NZD',
      preparer: { zh: '智能体', en: 'Agent' },
      procedure_id: 'REV-08',
    },
    sections: [
      {
        id: 'details',
        title: { zh: '测试明细', en: 'Test details' },
        tables: [
          {
            id: 'invoices',
            columns: [{ key: 'amount', label: { zh: '金额', en: 'Amount' }, format: 'money' }],
            rows: [{ amount: 990000 }],
          },
        ],
        children: [
          {
            id: 'subject',
            title: { zh: '合同', en: 'Contract' },
            generated_language: 'en',
            paragraphs: ['Original contract text.'],
          },
        ],
      },
    ],
  },
}

test('预览按快照币种格式化, 保留子节原文并支持两种固定文字', () => {
  const previous = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: 'en-AU', configurable: true })
  try {
    for (const language of ['zh', 'en'] as const) {
      const html = renderToStaticMarkup(
        <I18nProvider>
          <WorkpaperPreview paper={paper} language={language} />
        </I18nProvider>,
      )
      expect(html).toContain('NZD 9,900.00')
      expect(html).toContain('Original contract text.')
      expect(html).toContain(language === 'zh' ? '测试明细' : 'Test details')
      expect(html).toContain('data-testid="workpaper-preview"')
    }
    expect(paperText(null, 'zh')).toBe('—')
  } finally {
    if (previous) Object.defineProperty(navigator, 'language', previous)
    else Reflect.deleteProperty(navigator, 'language')
  }
})

test('下载文件名使用 RFC 5987 中文值, 无效编码回退', () => {
  expect(
    downloadName('attachment; filename="workpaper.xlsx"; filename*=UTF-8\'\'D-3%20%E4%B8%89%E5%8D%95.xlsx', 'fallback'),
  ).toBe('D-3 三单.xlsx')
  expect(downloadName("attachment; filename*=UTF-8''%invalid", 'fallback')).toBe('fallback')
  expect(downloadName('attachment; filename="D.xlsx"', 'fallback')).toBe('D.xlsx')
})
