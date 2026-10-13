export function isApiCode(error: unknown, code: string): boolean {
  return apiDetail(error)?.code === code
}

export function apiMessage(error: unknown): string {
  const detail = apiDetail(error)
  if (detail) return detail.message ? `${detail.code}: ${detail.message}` : detail.code
  if (error instanceof Error && error.message) return error.message
  return 'Request failed'
}

function apiDetail(error: unknown): { code: string; message?: string } | null {
  if (!error || typeof error !== 'object' || !('detail' in error)) return null
  const detail = (error as { detail: unknown }).detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail) || !('code' in detail)) return null
  const body = detail as { code?: unknown; message?: unknown }
  if (typeof body.code !== 'string') return null
  return { code: body.code, message: typeof body.message === 'string' ? body.message : undefined }
}
