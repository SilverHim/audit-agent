import { QueryClient } from '@tanstack/react-query'
import { client } from '@/client/client.gen'
import { getToken } from '@/lib/auth'

// 生成的客户端用相对路径 /api/v1/...，开发时由 Vite 代理到 FastAPI
client.interceptors.request.use((request) => {
  const token = getToken()
  if (token) request.headers.set('Authorization', `Bearer ${token}`)
  return request
})

export const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
})
