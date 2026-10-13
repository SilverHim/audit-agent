export function formatAud(cents: number | null | undefined): string {
  if (cents == null) return '—'
  return new Intl.NumberFormat('en-AU', { style: 'currency', currency: 'AUD' }).format(cents / 100)
}

export function formatPct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(2)}%`
}

export function audToCents(aud: string): number | null {
  const value = Number(aud)
  if (!Number.isFinite(value)) return null
  return Math.round(value * 100)
}
