import { createFileRoute, redirect } from '@tanstack/react-router'
import { type XeroNotice, xeroNotice } from '@/lib/xero'

// 首页转到项目列表; Xero 授权回调认不出项目时回到这里 (?xero=error&reason=...), 参数一并带过去提示
export const Route = createFileRoute('/')({
  validateSearch: (search: Record<string, unknown>): XeroNotice => xeroNotice(search),
  beforeLoad: ({ search }) => {
    throw redirect({ to: '/engagements', search })
  },
})
