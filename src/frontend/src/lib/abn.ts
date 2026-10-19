// ABN 的格式与校验位, 与后端 services/abn.py 同一算法 (ABR 公布, https://abr.business.gov.au/Help/AbnFormat):
// 11 位数字, 第一位减 1 后各位依次乘权重 10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 和能被 89 整除即有效.
// 前端只为就地提示, 以后端的校验为准
const WEIGHTS = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]

export type AbnProblem = 'format' | 'checksum'

export function normalizeAbn(abn: string): string {
  return abn.replace(/\s+/g, '')
}

export function abnProblem(abn: string): AbnProblem | null {
  const digits = normalizeAbn(abn)
  if (!/^[0-9]{11}$/.test(digits)) return 'format'
  const sum = [...digits].reduce(
    (total, ch, index) => total + (Number(ch) - (index === 0 ? 1 : 0)) * (WEIGHTS[index] ?? 0),
    0,
  )
  return sum % 89 === 0 ? null : 'checksum'
}

export function formatAbn(abn: string): string {
  const digits = normalizeAbn(abn)
  return digits.length === 11
    ? `${digits.slice(0, 2)} ${digits.slice(2, 5)} ${digits.slice(5, 8)} ${digits.slice(8)}`
    : abn
}
