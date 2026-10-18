import { useQuery } from '@tanstack/react-query'
import { usersMeOptions } from '@/client/@tanstack/react-query.gen'
import { getToken } from '@/lib/auth'
import { useI18n } from '@/lib/i18n'

// 操作人字段 (uploaded_by, changed_by 等) 是用户 ID 或智能体编号: 当前用户显示姓名, 智能体显示名称, 其他原样
export function useActorName(): (actor: string | null | undefined) => string | null {
  const { t } = useI18n()
  const me = useQuery({ ...usersMeOptions(), enabled: Boolean(getToken()) })
  return (actor) => {
    if (!actor) return null
    if (actor === me.data?.id) return me.data.full_name
    return t.agents[actor] ?? actor
  }
}
