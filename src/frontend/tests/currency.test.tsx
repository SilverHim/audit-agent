import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'
import { CurrencyForm, validCurrency } from '../src/components/currency-settings'
import { RecordTable } from '../src/components/evidence-link'
import { ExceptionDetail } from '../src/components/exception-detail'
import { I18nProvider } from '../src/lib/i18n'
import { formatMoney, formatMoneyShort, moneyToCents } from '../src/lib/money'
import { buildTimeline } from '../src/lib/timeline'
import { MoneyProvider, useMoney } from '../src/lib/use-money'
import { procedureIntro } from '../src/locales/procedure-intros'

let originalLanguage: PropertyDescriptor | undefined
beforeEach(() => {
  originalLanguage = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: 'en-AU', configurable: true })
})
afterEach(() => {
  if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
  else Reflect.deleteProperty(navigator, 'language')
})

function Amount() {
  const { formatMoney } = useMoney()
  return <span>{formatMoney(82500)}</span>
}

describe('项目币种', () => {
  test('格式与后端一致, 仍按主单位乘 100 存储', () => {
    expect(formatMoney(123456, 'NZD')).toBe('NZD 1,234.56')
    expect(formatMoney(-123450, 'JPY')).toBe('JPY -1,235')
    expect(formatMoney(123456, 'KWD')).toBe('KWD 1,234.560')
    expect(formatMoney(null, 'USD')).toBe('—')
    expect(formatMoneyShort(123456, 'USD')).toBe('USD 1,235')
    expect(moneyToCents('1,234.56')).toBe(123456)
    expect(moneyToCents('')).toBeNull()
    expect(moneyToCents('Infinity')).toBeNull()
  })

  test('同一页面上的两个项目树各用自己的币种, 子树退出后恢复父树', () => {
    const html = renderToStaticMarkup(
      <MoneyProvider currency="NZD">
        <Amount />
        <MoneyProvider currency="JPY">
          <Amount />
        </MoneyProvider>
        <Amount />
      </MoneyProvider>,
    )
    expect(html).toBe('<span>NZD 825.00</span><span>JPY 825</span><span>NZD 825.00</span>')
  })

  test('证据原始行与例外明细继承项目币种', () => {
    const html = renderToStaticMarkup(
      <I18nProvider>
        <MoneyProvider currency="USD">
          <RecordTable row={{ amount_cents: 4853 }} />
          <ExceptionDetail detail={{ difference_cents: 4853 }} />
        </MoneyProvider>
      </I18nProvider>,
    )
    expect(html.match(/USD 48.53/g)).toHaveLength(2)
    expect(html).not.toContain('AUD')
  })

  test('程序介绍的执行参数显式按项目币种', () => {
    const text = procedureIntro('FIN-04', 'en')?.project?.(
      { materiality_cents: 75000, clearly_trivial_cents: 1000 },
      'NZD',
    )
    expect(text).toContain('NZD 750.00')
    expect(text).not.toContain('AUD')
  })

  test('币种控件显示来源与不作换算提示, 锁定时禁用', () => {
    const html = renderToStaticMarkup(
      <I18nProvider>
        <CurrencyForm currency="NZD" source="xero" disabled onConfirm={() => {}} />
      </I18nProvider>,
    )
    expect(html).toContain('NZD')
    expect(html).toContain('disabled')
    expect(html).toContain('Xero')
    expect(validCurrency('NZD')).toBe(true)
    expect(validCurrency('nzd')).toBe(false)
    expect(validCurrency('AU')).toBe(false)
  })
})

test('币种确认在对话时间线单列, 不按普通数据导入解释', () => {
  const items = buildTimeline(
    [
      {
        id: 'TR-CURRENCY',
        engagement_id: 'ENG-TEST',
        kind: 'data',
        actor: 'U-TEST',
        summary: 'Synthetic currency confirmation',
        created_at: '2026-10-22T00:00:00Z',
        payload: { action: 'currency', from: 'AUD', to: 'NZD', source: 'auditor' },
      },
    ],
    false,
  )
  expect(items).toEqual([{ kind: 'currency', key: 'TR-CURRENCY', at: '2026-10-22T00:00:00Z', from: 'AUD', to: 'NZD' }])
})
