import { useQuery } from '@tanstack/react-query'
import { CircleCheck, TriangleAlert } from 'lucide-react'
import { clientsAbnLookupOptions } from '@/client/@tanstack/react-query.gen'
import type { AbnLookupView } from '@/client/types.gen'
import { abnProblem, formatAbn, normalizeAbn } from '@/lib/abn'
import { lookupSummary } from '@/lib/abn-lookup'
import { useI18n } from '@/lib/i18n'
import { useDebounced } from '@/lib/use-debounced'

const STALE_MS = 5 * 60_000 // 同一 ABN 与名称 5 分钟内不再请求; 后端另按 ABN 缓存, 改名称只重新比较

// ABN 在线核对 (需求 R289): 校验位通过后, 输入停顿约半秒再经后端查 ABR ABN Lookup (GET /clients/abn-lookup).
// 校验位不对时不查 (返回 null 由表单显示原有提示)
export function useAbnLookup(abn: string, legalName: string) {
  const digits = normalizeAbn(abn)
  const valid = abnProblem(digits) === null
  const settledAbn = useDebounced(valid ? digits : '')
  const settledName = useDebounced(legalName.trim())
  const query = useQuery({
    ...clientsAbnLookupOptions({ query: { abn: settledAbn, legal_name: settledName } }),
    enabled: settledAbn !== '',
    staleTime: STALE_MS,
    retry: false,
    // 只改了名称时先留着同一 ABN 的上次结果 (登记信息不变), 免得每改一字就闪成 "正在查询"
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[0].query?.abn === settledAbn ? previous : undefined,
  })
  const pending = valid && (settledAbn !== digits || query.isPending)
  // 名称是否一致只对当前填写的名称: 名称还在防抖或结果是上次名称的, 先不提示
  const nameSettled = settledName === legalName.trim() && !query.isPlaceholderData
  return { valid, digits, pending, nameSettled, result: pending ? undefined : query.data, failed: query.isError }
}

// ABN 下方的核对结果: 校验位正确一行, 再加 ABN Lookup 的登记信息 (名称, 状态, 主体类型, ACN); 已注销, 查不到或
// 名称与填写的法定名称不一致时警示 (不拦提交, 承接时 ACC-04 照常核查); 查不了时说明原因, 建项目后转人工确认
export function AbnLookupNote({ abn, legalName }: { abn: string; legalName: string }) {
  const { t } = useI18n()
  const { valid, digits, pending, nameSettled, result, failed } = useAbnLookup(abn, legalName)
  if (!valid) return null
  return (
    <span className="flex flex-col gap-0.5">
      <span className="inline-flex items-center gap-1">
        <CircleCheck className="size-3.5 shrink-0 text-ok" aria-hidden="true" />
        {t.abnLookup.checksumOk(formatAbn(digits))}
      </span>
      <LookupLine pending={pending} result={result} failed={failed} nameSettled={nameSettled} />
    </span>
  )
}

function LookupLine({
  pending,
  result,
  failed,
  nameSettled,
}: {
  pending: boolean
  result?: AbnLookupView
  failed: boolean
  nameSettled: boolean
}) {
  const { t } = useI18n()
  if (failed) return <span>{t.abnLookup.unavailable.service_error}</span>
  if (pending || !result) return <span>{t.abnLookup.checking}</span>
  const { text, warnings } = lookupSummary(result, t, nameSettled)
  return (
    <>
      {text ? <span>{text}</span> : null}
      {warnings.map((warning) => (
        <span key={warning} className="inline-flex items-center gap-1 text-warn">
          <TriangleAlert className="size-3.5 shrink-0" aria-hidden="true" />
          {warning}
        </span>
      ))}
    </>
  )
}
