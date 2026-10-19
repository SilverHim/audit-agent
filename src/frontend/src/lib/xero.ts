import { useEffect } from 'react'
import { toast } from 'sonner'
import type { TableKind, XeroSyncResult } from '@/client/types.gen'
import type { Messages } from '@/locales/en'

// Xero 授权回调 (后端 GET /api/v1/integrations/xero/callback) 302 回前端时带的参数: 成功为 xero=connected,
// 失败为 xero=error&reason=<原因代码> (services.xero_link.CALLBACK_REASONS). 认得出项目时回该项目的数据管理页,
// 认不出 (state 不认识, 项目不存在) 时回首页, 首页转到项目列表时带上这两个参数
export type XeroNotice = { xero?: string; reason?: string }

export function xeroNotice(search: Record<string, unknown>): XeroNotice {
  const notice: XeroNotice = {}
  if (typeof search.xero === 'string') notice.xero = search.xero
  if (typeof search.reason === 'string') notice.reason = search.reason
  return notice
}

// 回调结果的提示: 没有回调参数为 null; 成功 ok 为 true; 失败按原因代码给文案, 不认识的代码用通用文案
export function xeroNoticeText(notice: XeroNotice, t: Messages): { ok: boolean; text: string } | null {
  if (!notice.xero) return null
  if (notice.xero === 'connected') return { ok: true, text: t.data.xero.callback.connected }
  const reasons = t.data.xero.callback.reasons
  return { ok: false, text: (notice.reason && reasons[notice.reason]) || t.data.xero.failed }
}

// 回调参数的提示: 提示一次后由 clear 清掉地址里的参数 (数据管理页与项目列表共用).
// 同一 id 只显示一条 (开发模式下 StrictMode 会把这个 effect 跑两次)
export function useXeroCallbackNotice(notice: XeroNotice, t: Messages, clear: () => void) {
  const { xero, reason } = notice
  useEffect(() => {
    const text = xeroNoticeText({ xero, reason }, t)
    if (!text) return
    if (text.ok) toast.success(text.text, { id: 'xero-callback' })
    else toast.error(text.text, { id: 'xero-callback' })
    clear()
  }, [xero, reason, t, clear])
}

// 同步没写的表, 同一原因的合成一项 (如 "银行账户、银行流水: 未开通付费接口"); 按表的顺序. 卡片的上次结果与
// 工作区时间线共用; 认不出的原因 (后端读成 unknown) 写 "原因不明"
export function skippedByReason(
  skipped: XeroSyncResult['skipped'] | Record<string, string>,
  t: Messages,
  lang: 'zh' | 'en',
): { tables: string; reason: string }[] {
  const byReason = new Map<string, TableKind[]>()
  for (const [kind, reason] of Object.entries(skipped ?? {}) as [TableKind, string][]) {
    byReason.set(reason, [...(byReason.get(reason) ?? []), kind])
  }
  const joiner = lang === 'zh' ? '、' : ', '
  return [...byReason].map(([reason, kinds]) => ({
    tables: kinds.map((kind) => t.data.tables[kind] ?? kind).join(joiner),
    reason: t.data.events.syncSkipped[reason] ?? t.data.xero.syncFailedUnknown,
  }))
}

// 同步失败的原因代码 → 文案; 认不出的 (后端读成 unknown) 与缺的写 "原因不明"
export function syncFailureText(reason: XeroSyncResult['reason'], t: Messages): string {
  return (reason && t.data.events.syncFailure[reason]) || t.data.xero.syncFailedUnknown
}
