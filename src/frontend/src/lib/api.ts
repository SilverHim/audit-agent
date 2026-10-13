import { QueryClient } from '@tanstack/react-query'
import { client } from '@/client/client.gen'
import { clearToken, getToken } from '@/lib/auth'

// 生成的客户端用相对路径 /api/v1/..., 开发时由 Vite 代理到 FastAPI
client.interceptors.request.use((request) => {
  const token = getToken()
  if (token) request.headers.set('Authorization', `Bearer ${token}`)
  return request
})

client.interceptors.response.use((response) => {
  const loggingIn = response.url.includes('/auth/login')
  if (response.status === 401 && !loggingIn) {
    clearToken()
    if (!window.location.pathname.startsWith('/login')) window.location.assign('/login')
  }
  return response
})

export const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})
