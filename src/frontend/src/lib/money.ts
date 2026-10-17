const AMOUNT = new Intl.NumberFormat('en-AU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

// 金额统一显示为 "AUD 142,000.00", 中英文界面一致
export function formatAud(cents: number | null | undefined): string {
  if (cents == null) return '—'
  return `AUD ${AMOUNT.format(cents / 100)}`
}

// 不带小数的简写, 用于侧栏等空间紧的地方
export function formatAudShort(cents: number | null | undefined): string {
  if (cents == null) return '—'
  return `AUD ${new Intl.NumberFormat('en-AU', { maximumFractionDigits: 0 }).format(cents / 100)}`
}

export function formatPct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(2)}%`
}

export function audToCents(aud: string): number | null {
  const value = Number(aud.replaceAll(',', '').trim())
  if (!aud.trim() || !Number.isFinite(value)) return null
  return Math.round(value * 100)
}
