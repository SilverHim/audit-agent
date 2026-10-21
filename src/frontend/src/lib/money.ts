const FORMATS = new Map<string, Intl.NumberFormat>()

// 账表中的整数仍为主单位乘 100; 显示位数按项目币种, 中英文界面一致.
export function formatMoney(cents: number | null | undefined, currency = 'AUD'): string {
  if (cents == null) return '—'
  let format = FORMATS.get(currency)
  if (!format) {
    const digits = new Intl.NumberFormat('en-AU', { style: 'currency', currency }).resolvedOptions()
      .maximumFractionDigits
    format = new Intl.NumberFormat('en-AU', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    FORMATS.set(currency, format)
  }
  return `${currency} ${format.format(cents / 100)}`
}

export function formatMoneyShort(cents: number | null | undefined, currency = 'AUD'): string {
  if (cents == null) return '—'
  return `${currency} ${new Intl.NumberFormat('en-AU', { maximumFractionDigits: 0 }).format(cents / 100)}`
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function moneyToCents(amount: string): number | null {
  const value = Number(amount.replaceAll(',', '').trim())
  if (!amount.trim() || !Number.isFinite(value)) return null
  const cents = Math.round(value * 100)
  return Number.isSafeInteger(cents) ? cents : null
}

// 旧调用的兼容别名; 项目界面使用 useMoney 提供的格式器.
export const formatAud = formatMoney
export const formatAudShort = formatMoneyShort
export const audToCents = moneyToCents
