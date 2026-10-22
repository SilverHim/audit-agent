import type {
  EvidenceRequest,
  EvidenceRequestIn,
  EvidenceUnavailableIn,
  ManagementRepresentationIn,
} from '@/client/types.gen'
import { parseRelatedIds } from '@/lib/document-details'
import type { Messages } from '@/locales/en'

// 补充证据请求的前端取值 (后端需求 R369, 页面 R370): 状态, 修改请求的请求体, 标无法取得, 登记管理层说明与"由补充证据
// 重判"的来龙去脉. 都是纯函数, 校验以后端为准, 这里只挡显而易见的漏填.

// 请求状态: 等证据 (open), 已收到 (received), 无法取得 (unavailable); 旧数据没有状态按等证据
export type RequestStatus = NonNullable<EvidenceRequest['status']>
export type RequestedFrom = NonNullable<EvidenceRequest['requested_from']>

export const REQUESTED_FROM: RequestedFrom[] = ['client', 'bank', 'other', 'management']

export function requestStatus(request: EvidenceRequest): RequestStatus {
  return request.status ?? 'open'
}

// 还在等证据的请求可以改与标无法取得
export function requestOpen(request: EvidenceRequest): boolean {
  return requestStatus(request) === 'open'
}

export function requestRelated(request: EvidenceRequest): string[] {
  return request.related_object_ids ?? []
}

export function requestDocuments(request: EvidenceRequest): string[] {
  return request.document_ids ?? []
}

// 向谁要, 旧数据没填按客户
export function requestFrom(request: EvidenceRequest): RequestedFrom {
  return request.requested_from ?? 'client'
}

// 修改请求的表单初值
export type RequestEditForm = {
  description: string
  requestedFrom: RequestedFrom
  relatedIds: string // 逗号或换行分隔, 同文件补登记的关联对象
}

export function requestEditForm(request: EvidenceRequest): RequestEditForm {
  return {
    description: request.description,
    requestedFrom: requestFrom(request),
    relatedIds: requestRelated(request).join(', '),
  }
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((item, index) => item === b[index])
}

// 修改请求的请求体: 只带改了的项; 要什么不能为空; 一项都没改时为错误文案
export function editRequestBody(
  request: EvidenceRequest,
  form: RequestEditForm,
  t: Messages,
): EvidenceRequestIn | string {
  const description = form.description.trim()
  if (!description) return t.evidenceRequests.errors.description
  const related = parseRelatedIds(form.relatedIds)
  const body: EvidenceRequestIn = {}
  if (description !== request.description) body.description = description
  if (form.requestedFrom !== requestFrom(request)) body.requested_from = form.requestedFrom
  if (!sameList(related, requestRelated(request))) body.related_object_ids = related
  if (!Object.keys(body).length) return t.evidenceRequests.errors.noChange
  return body
}

// 标无法取得: 理由选填, 空为 null
export function unavailableBody(reason: string): EvidenceUnavailableIn {
  const trimmed = reason.trim()
  return { reason: trimmed || null }
}

// 登记管理层说明 (后端 R369): 说明人, 时间与内容都必填
export type RepresentationDraft = { person: string; statedOn: string; content: string }

export function emptyRepresentation(): RepresentationDraft {
  return { person: '', statedOn: '', content: '' }
}

export function representationBody(draft: RepresentationDraft, t: Messages): ManagementRepresentationIn | string {
  const person = draft.person.trim()
  const statedOn = draft.statedOn.trim()
  const content = draft.content.trim()
  const labels = t.managementRepresentation
  const missing: string[] = []
  if (!person) missing.push(labels.person)
  if (!statedOn) missing.push(labels.statedOn)
  if (!content) missing.push(labels.content)
  if (missing.length) return labels.errors.required(missing.join(t.evidenceRequests.separator))
  return { person, stated_on: statedOn, content }
}

// 请求状态的变化: 轮询到 open 变成 received/unavailable 时, 调用方刷新复核队列 (原任务回到待处理)
export function requestStatuses(requests: EvidenceRequest[]): Map<string, string> {
  return new Map(requests.map((request) => [request.id, requestStatus(request)]))
}

export function requestStatusChanged(
  previous: ReadonlyMap<string, string> | null,
  requests: EvidenceRequest[],
): boolean {
  if (!previous) return false
  return requests.some((request) => {
    const before = previous.get(request.id)
    return before !== undefined && before !== requestStatus(request)
  })
}

// 判断详情里"由补充证据请求重判"的来龙去脉: 请求的 rejudged_judgment_id 等于该判断时取它; 没有为 null (后端字段为
// 后续预留, 现在恒为空)
export function rejudgedRequest(requests: EvidenceRequest[], judgmentId: string): EvidenceRequest | null {
  return requests.find((request) => request.rejudged_judgment_id === judgmentId) ?? null
}
