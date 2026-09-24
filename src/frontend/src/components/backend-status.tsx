import { useQuery } from '@tanstack/react-query'
import { healthHealthOptions } from '@/client/@tanstack/react-query.gen'
import { Badge } from '@/components/ui/badge'

// 顶栏的后端连通状态，也是生成客户端 + Vite 代理的冒烟检查
export function BackendStatus() {
  const { isSuccess, isError } = useQuery(healthHealthOptions())
  if (isSuccess) return <Badge variant="secondary">API online</Badge>
  if (isError) return <Badge variant="destructive">API offline</Badge>
  return <Badge variant="outline">API …</Badge>
}
