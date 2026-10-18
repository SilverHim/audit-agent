import type { Messages } from '@/locales/en'

// 后端错误体为 {detail: {code, message, ...}}; 部分错误码在 detail 里另带字段, 如 DATA_NOT_READY 的 missing_tables
type ApiDetail = { code: string; message?: string } & Record<string, unknown>

export function isApiCode(error: unknown, code: string): boolean {
  return apiDetail(error)?.code === code
}

export function apiMessage(error: unknown): string {
  const detail = apiDetail(error)
  if (detail) return detail.message ? `${detail.code}: ${detail.message}` : detail.code
  if (error instanceof Error && error.message) return error.message
  return 'Request failed'
}

// 有本地化文案的错误码按界面语言提示, 其余同 apiMessage
export function errorText(error: unknown, t: Messages): string {
  const detail = apiDetail(error)
  if (!detail) return apiMessage(error)
  if (detail.code === 'DATA_NOT_READY') {
    const missing = Array.isArray(detail.missing_tables) ? detail.missing_tables : []
    return t.apiErrors.dataNotReady(missing.map((kind) => t.data.tables[String(kind)] ?? String(kind)))
  }
  return t.apiErrors.codes[detail.code] ?? apiMessage(error)
}

// DATA_CHECKS_FAILED 附带的未通过体检项编号
export function failedChecks(error: unknown): string[] {
  const detail = apiDetail(error)
  if (detail?.code !== 'DATA_CHECKS_FAILED' || !Array.isArray(detail.failed_checks)) return []
  return detail.failed_checks.map(String)
}

// IMPORT_PARSE_ERROR 附带的前 20 处解析失败
export type ParseFailure = { row: number; column: string; value: string }

export function parseFailures(error: unknown): ParseFailure[] | null {
  const detail = apiDetail(error)
  if (detail?.code !== 'IMPORT_PARSE_ERROR' || !Array.isArray(detail.errors)) return null
  return detail.errors as ParseFailure[]
}

function apiDetail(error: unknown): ApiDetail | null {
  if (!error || typeof error !== 'object' || !('detail' in error)) return null
  const detail = (error as { detail: unknown }).detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail) || !('code' in detail)) return null
  const body = detail as Record<string, unknown>
  if (typeof body.code !== 'string') return null
  return { ...body, code: body.code, message: typeof body.message === 'string' ? body.message : undefined }
}
