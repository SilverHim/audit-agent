import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '@/client/client.gen'
import type { AcceptanceScorecard, AuditException, ClientProfile, EvidencePointer } from '@/client/types.gen'
import { getToken } from '@/lib/auth'
import { invalidateEngagement } from '@/lib/engagement'

// 后端需求 R1, R2, R5-R8 的接口. 类型按需求文档写; 后端合入并重新生成客户端后改用 src/client 的生成函数.
// 接口尚未上线时 (404, 或同路径只有别的方法时的 405) 返回空结果, 页面照常可用

export class NotAvailable extends Error {}

export async function call<T>(
  method: 'get' | 'post' | 'put' | 'delete',
  url: string,
  path: Record<string, string>,
  body?: unknown,
  query?: Record<string, string | number | undefined>,
) {
  const options = { url, path, query, body, headers: body ? { 'Content-Type': 'application/json' } : undefined }
  const result =
    method === 'get'
      ? await client.get(options)
      : method === 'post'
        ? await client.post(options)
        : method === 'put'
          ? await client.put(options)
          : await client.delete(options)
  const status = result.response?.status
  const code = (result.error as { detail?: { code?: string } } | undefined)?.detail?.code
  if (status === 405 || (status === 404 && !code)) throw new NotAvailable(url)
  if (result.error) throw result.error
  return result.data as T
}

export async function orEmpty<T>(promise: Promise<T>, empty: T): Promise<T> {
  try {
    return await promise
  } catch (error) {
    if (error instanceof NotAvailable) return empty
    throw error
  }
}

// 账号注册 (U01): 成功即返回访问令牌. 后端合入后改用生成的 authRegister
export type RegisterBody = { username: string; full_name: string; password: string }

export function registerAccount(body: RegisterBody) {
  return call<{ access_token: string; token_type: string }>('post', '/api/v1/auth/register', {}, body)
}

// R1 稍后处理
export type DeferralTarget = 'review_task' | 'gate' | 'data_request'
export type Deferral = { target_type: DeferralTarget; target_id: string; deferred_by: string; deferred_at: string }

// 对话与待决定页用同一个键标识事项: gate:ACC-01, task:{任务 ID} 或 request:{补交请求 ID}
const DEFERRAL_PREFIX: Record<DeferralTarget, string> = { gate: 'gate', review_task: 'task', data_request: 'request' }

export function deferralKey(item: Pick<Deferral, 'target_type' | 'target_id'>): string {
  return `${DEFERRAL_PREFIX[item.target_type]}:${item.target_id}`
}

export function deferralsKey(eid: string) {
  return [{ _id: 'deferralsListDeferrals', path: { eid } }] as const
}

export function useDeferrals(eid: string) {
  return useQuery({
    queryKey: deferralsKey(eid),
    queryFn: () => orEmpty(call<Deferral[]>('get', '/api/v1/engagements/{eid}/deferrals', { eid }), []),
  })
}

export function useSetDeferral(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { target_type: DeferralTarget; target_id: string; deferred: boolean }) =>
      call<{ id: string }>('post', '/api/v1/engagements/{eid}/deferrals', { eid }, input),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R2 客户资料
export type ClientProfileView = {
  profile: ClientProfile
  history: ClientProfile[]
  scorecard: AcceptanceScorecard | null
  exceptions: AuditException[]
  editable: boolean
}

export function useClientProfile(eid: string) {
  return useQuery({
    queryKey: [{ _id: 'engagementsGetClientProfile', path: { eid } }],
    queryFn: () =>
      orEmpty(call<ClientProfileView | null>('get', '/api/v1/engagements/{eid}/client-profile', { eid }), null),
  })
}

// R5 可选客户
export type DatasetSummary = { dataset: string; legal_name: string; abn: string }

export function useDatasets() {
  return useQuery({
    queryKey: [{ _id: 'datasetsListDatasets' }],
    queryFn: () => orEmpty(call<DatasetSummary[]>('get', '/api/v1/datasets', {}), []),
  })
}

// R6 模型设置
export type LlmSlot = 'primary' | 'review'
export type LlmSlotView = {
  provider: string
  base_url: string
  model: string
  has_key: boolean
  key_last4: string | null
  updated_at: string | null
  updated_by: string | null
}
export type LlmSettingsView = { primary: LlmSlotView; review: LlmSlotView }
export type LlmSlotIn = { provider: string; base_url: string; model: string; api_key: string | null }
export type LlmTestResult = { ok: boolean; model: string | null; latency_ms: number; message: string }

export function useLlmSettings() {
  return useQuery({
    queryKey: [{ _id: 'settingsGetLlmSettings' }],
    queryFn: () => orEmpty(call<LlmSettingsView | null>('get', '/api/v1/settings/llm', {}), null),
  })
}

export function useSaveLlmSlot() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ slot, body }: { slot: LlmSlot; body: LlmSlotIn }) =>
      call<LlmSlotView>('put', '/api/v1/settings/llm/{slot}', { slot }, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [{ _id: 'settingsGetLlmSettings' }] }),
  })
}

export function useTestLlmSlot() {
  return useMutation({
    mutationFn: ({ slot, body }: { slot: LlmSlot; body: LlmSlotIn }) =>
      call<LlmTestResult>('post', '/api/v1/settings/llm/{slot}/test', { slot }, body),
  })
}

// R7, R8 审计助理问答
export type CitationKind = 'judgment' | 'procedure' | 'evidence' | 'trail'
export type ConciergeCitation = { kind: CitationKind; id: string; label: string; pointer?: EvidencePointer | null }
export type ConversationTurn = {
  question: string
  answer: string
  lang: 'zh' | 'en'
  citations: ConciergeCitation[]
  trail_id?: string
  created_at?: string | null
}

export function conversationKey(eid: string) {
  return [{ _id: 'conversationListConversation', path: { eid } }] as const
}

export function useConversation(eid: string) {
  return useQuery({
    queryKey: conversationKey(eid),
    queryFn: () => orEmpty(call<ConversationTurn[]>('get', '/api/v1/engagements/{eid}/conversation', { eid }), []),
  })
}

type StreamHandlers = {
  onDelta: (text: string) => void
  onCitations: (citations: ConciergeCitation[]) => void
  onTool?: (label: string) => void
}

// 事件流: tool (正在查什么, 可多次), delta (多次), citations, done; 出错时 error. EventSource 不能带登录令牌, 用 fetch 读
export async function streamConcierge(
  eid: string,
  body: { question: string; lang: 'zh' | 'en' },
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<{ trail_id: string | null }> {
  const response = await fetch(`/api/v1/engagements/${encodeURIComponent(eid)}/conversation/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken() ?? ''}`,
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
    signal,
  })
  if (response.status === 404) throw new NotAvailable('conversation/stream')
  if (!response.ok || !response.body) {
    const detail: unknown = await response.json().catch(() => null)
    throw detail ?? new Error(`HTTP ${response.status}`)
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  let trailId: string | null = null
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const raw = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
      let event = 'message'
      const data: string[] = []
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      }
      const payload: unknown = data.length ? JSON.parse(data.join('\n')) : null
      if (event === 'tool') handlers.onTool?.((payload as { label: string }).label)
      else if (event === 'delta') handlers.onDelta((payload as { text: string }).text)
      else if (event === 'citations') handlers.onCitations((payload as { citations: ConciergeCitation[] }).citations)
      else if (event === 'done') trailId = (payload as { trail_id?: string }).trail_id ?? null
      else if (event === 'error') throw { detail: payload }
    }
  }
  return { trail_id: trailId }
}
