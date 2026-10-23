import { afterEach, beforeEach, expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { CorrectionEditor } from '../src/components/correction-editor'
import { EvidenceFollowupSummary } from '../src/components/evidence-followup-summary'
import { I18nProvider } from '../src/lib/i18n'
import { MoneyProvider } from '../src/lib/use-money'

const originalLanguage = Object.getOwnPropertyDescriptor(navigator, 'language')
beforeEach(() => Object.defineProperty(navigator, 'language', { value: 'en-AU', configurable: true }))
afterEach(() => {
  if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
  else Reflect.deleteProperty(navigator, 'language')
})

test('意见决定前显示等待证据与范围受限, 金额按项目币种', () => {
  const html = renderToStaticMarkup(
    <I18nProvider>
      <MoneyProvider currency="NZD">
        <EvidenceFollowupSummary
          requests={[
            {
              id: 'ER-1',
              version: 1,
              subject_id: 'XJ-397',
              procedure_id: 'GEN-02',
              task_id: 'T-1',
              judgment_id: 'J-1',
              description: 'Opening bank statement',
              requested_by: 'U-1',
              requested_at: '2026-10-22T00:00:00Z',
            },
          ]}
          limitations={[
            {
              id: 'SL-1',
              version: 1,
              request_id: 'ER-2',
              judgment_id: 'J-2',
              procedure_id: 'GEN-02',
              subject_id: 'XJ-575',
              amount_cents: 29695,
              assertion: 'Occurrence',
              recorded_by: 'U-1',
              recorded_at: '2026-10-22T00:00:00Z',
            },
          ]}
        />
      </MoneyProvider>
    </I18nProvider>,
  )
  expect(html).toContain('XJ-397')
  expect(html).toContain('Opening bank statement')
  expect(html).toContain('XJ-575')
  expect(html).toContain('NZD 296.95')
})

test('更正建议有科目、借贷、摘要和增删控件', () => {
  const html = renderToStaticMarkup(
    <I18nProvider>
      <CorrectionEditor
        entries={[
          {
            account_code: '710',
            account_name: 'Equipment',
            credit_cents: 75000,
            description: 'Reverse the equipment entry',
          },
        ]}
        accounts={[{ code: '710', name: 'Equipment' }]}
        onChange={() => {}}
      />
    </I18nProvider>,
  )
  expect(html).toContain('correction-editor')
  expect(html).toContain('aria-label="Credit 1"')
  expect(html).toContain('value="750"')
  expect(html).toContain('Add entry')
  expect(html).toContain('Remove entry')
})
