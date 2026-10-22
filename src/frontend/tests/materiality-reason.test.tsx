import { expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { MaterialityReasonHint } from '../src/components/gate-form'
import { I18nProvider } from '../src/lib/i18n'

test('重要性空理由的建议不阻断提交, 隐藏时不留下表单控件', () => {
  const previous = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: 'en-AU', configurable: true })
  try {
    const html = renderToStaticMarkup(
      <I18nProvider>
        <MaterialityReasonHint visible />
      </I18nProvider>,
    )
    expect(html).toContain('role="note"')
    expect(html).toContain('ASA 320')
    expect(html).not.toContain('required')
    expect(
      renderToStaticMarkup(
        <I18nProvider>
          <MaterialityReasonHint visible={false} />
        </I18nProvider>,
      ),
    ).toBe('')
  } finally {
    if (previous) Object.defineProperty(navigator, 'language', previous)
    else Reflect.deleteProperty(navigator, 'language')
  }
})
