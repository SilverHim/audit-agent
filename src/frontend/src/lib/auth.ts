// 登录令牌存在 localStorage；隐私模式下读写可能抛错，一律按未登录处理
const KEY = 'access_token'

export function getToken(): string | null {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(KEY, token)
  } catch {
    // 忽略：本次会话内仍可用内存中的状态
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    // 忽略
  }
}
