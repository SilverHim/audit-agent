import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { evidenceListDocumentsOptions, manualListManualTasksOptions } from '@/client/@tanstack/react-query.gen'
import { evidenceUploadDocuments } from '@/client/sdk.gen'
import type { EvidencePointer, ManualTaskView, ReviewRecordIn } from '@/client/types.gen'
import { invalidateEngagement } from '@/lib/engagement'
import type { Messages } from '@/locales/en'

// 人工队列 (后端需求 R94): 人工程序派给人的 manual 任务. 证据文件经已有的上传接口 (POST /documents) 上传, 结果经
// POST /review-records 以 manual_result 提交; 证据数, 页码, 执行日期与结构化结果由后端按任务的请求核对

export type { ManualTaskView } from '@/client/types.gen'

// 刚上传的文件在后台分类, 分类成可作证据的类型后才出现在各项的 documents 里 (详情页随文件登记的刷新重取)
export function useManualTasks(eid: string) {
  return useQuery(manualListManualTasksOptions({ path: { eid } }))
}

// 证据文件经已有的上传接口; 返回各文件的登记 (received, 由 A3 在后台分类)
export function useUploadDocuments(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (files: File[]) =>
      (await evidenceUploadDocuments({ path: { eid }, body: { files }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 本项目全部文件的登记, 看刚上传的分类到哪一步; watching 中有还在分类的 (received) 时每 3 秒重取
export function useDocumentStatus(eid: string, watching: string[]) {
  return useQuery({
    ...evidenceListDocumentsOptions({ path: { eid } }),
    enabled: watching.length > 0,
    refetchInterval: (query) =>
      query.state.data?.some((item) => watching.includes(item.id) && item.status === 'received') ? 3000 : false,
  })
}

// 结构化结果的一个字段, 由结果模型的 JSON schema 得出: 认得的类型给专门的输入框, 其余写 JSON
export type FieldKind = 'string' | 'number' | 'integer' | 'date' | 'boolean' | 'enum' | 'json'
export type ResultField = { name: string; title: string; kind: FieldKind; required: boolean; options: string[] }
export type FieldValue = string | boolean

type Schema = {
  type?: string
  format?: string
  title?: string
  enum?: unknown[]
  anyOf?: Schema[]
  $ref?: string
  properties?: Record<string, Schema>
  required?: string[]
  $defs?: Record<string, Schema>
}

// 字段的类型: $ref 指向 $defs (Pydantic 的枚举); anyOf 去掉 null 后只剩一种的按那一种 (Optional 字段). 标题不从这里取
function resolve(schema: Schema, root: Schema): Schema {
  if (schema.$ref) return resolve(root.$defs?.[schema.$ref.split('/').pop() ?? ''] ?? {}, root)
  const variants = (schema.anyOf ?? []).filter((item) => item.type !== 'null')
  if (schema.anyOf && variants.length === 1) return resolve(variants[0], root)
  return schema
}

// 没有 title 的字段 ($ref 与 Optional[$ref], Pydantic 不生成) 按字段名起标题, 同 Pydantic 的写法: audit_basis → Audit Basis
function titleOf(name: string): string {
  return name
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ')
}

function kindOf(schema: Schema): FieldKind {
  if (schema.enum?.every((item) => typeof item === 'string')) return 'enum'
  if (schema.type === 'string') return schema.format === 'date' ? 'date' : 'string'
  if (schema.type === 'integer' || schema.type === 'number' || schema.type === 'boolean') return schema.type
  return 'json'
}

export function resultFields(schema: ManualTaskView['result_schema']): ResultField[] {
  if (!schema) return []
  const root = schema as Schema
  const required = new Set(root.required ?? [])
  return Object.entries(root.properties ?? {}).map(([name, raw]) => {
    const field = resolve(raw, root)
    return {
      name,
      title: raw.title ?? titleOf(name),
      kind: kindOf(field),
      required: required.has(name),
      options: field.enum?.map(String) ?? [],
    }
  })
}

// 表单的值 → details; 必填没填, 数字或 JSON 写不对时返回错误文案. 没填的选填字段不带, 勾选框总带
export function parseDetails(
  fields: ResultField[],
  values: Record<string, FieldValue>,
  t: Messages,
): Record<string, unknown> | string {
  const details: Record<string, unknown> = {}
  for (const field of fields) {
    const value = values[field.name]
    if (field.kind === 'boolean') {
      details[field.name] = value === true
      continue
    }
    const text = typeof value === 'string' ? value.trim() : ''
    if (!text) {
      if (field.required) return t.manualPage.errors.required(field.title)
      continue
    }
    if (field.kind === 'integer' || field.kind === 'number') {
      const number = Number(text)
      if (!Number.isFinite(number) || (field.kind === 'integer' && !Number.isInteger(number)))
        return t.manualPage.errors.number(field.title)
      details[field.name] = number
    } else if (field.kind === 'json') {
      try {
        details[field.name] = JSON.parse(text)
      } catch {
        return t.manualPage.errors.json(field.title)
      }
    } else {
      details[field.name] = text
    }
  }
  return details
}

export type EvidenceChoice = { documentId: string; page: number }
export type ManualDraft = {
  conclusion: string
  reason: string
  performedOn: string // YYYY-MM-DD
  evidence: EvidenceChoice[]
  values: Record<string, FieldValue>
}

export function emptyManualDraft(): ManualDraft {
  return { conclusion: '', reason: '', performedOn: '', evidence: [], values: {} }
}

// 选的文件与页 → 证据指针: type 取后端给的该文件作证据时的种类 (evidence_types; 必填, 交真实的种类而不是占位值),
// 可靠性取文件登记; 写入时后端照旧按文件登记改写, 以后端为准. 已不在可选文件里的不算; 列出的文件没有种类是接口出错,
// 直接报错, 不兜底
export function evidencePointers(view: ManualTaskView, evidence: EvidenceChoice[]): EvidencePointer[] {
  return evidence.flatMap(({ documentId, page }) => {
    const document = view.documents.find((item) => item.id === documentId)
    if (!document) return []
    const type = view.evidence_types[documentId]
    if (!type) throw new Error(`${documentId} 没有证据种类 (evidence_types)`)
    return [
      {
        type,
        source_type: 'document',
        source_id: document.id,
        locator: document.file_name,
        page,
        reliability: document.reliability,
      },
    ]
  })
}

// 人工结果的决定; 缺结论, 执行日期, 证据不够或结构化结果写不对时返回错误文案 (理由选填, 需求 R360). details 给了时用它 (如 FIN-08
// 逐条错报的登记, lib/misstatements.responsesDetails), 不再按结果模型的字段解析
export function manualRecordBody(
  view: ManualTaskView,
  draft: ManualDraft,
  t: Messages,
  given?: Record<string, unknown>,
): ReviewRecordIn | string {
  const correctionDecision = view.request.result_model === 'ManagementResponses' && given !== undefined
  const conclusion = draft.conclusion.trim() || (correctionDecision ? t.fin08.recorded : '')
  const now = new Date()
  const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
  const performedOn = draft.performedOn || (correctionDecision ? today : '')
  const reason = draft.reason.trim()
  if (!conclusion) return t.errors.conclusionRequired
  if (!performedOn) return t.manualPage.errors.performedOn
  const evidence = evidencePointers(view, draft.evidence)
  if (evidence.length < view.request.min_evidence) return t.manualPage.errors.evidence(view.request.min_evidence)
  const details = given ?? parseDetails(resultFields(view.result_schema), draft.values, t)
  if (typeof details === 'string') return details
  return {
    task_id: view.task.id,
    target_version: view.task.target_version,
    decision: 'manual_result',
    reason,
    manual_result: { conclusion, performed_on: performedOn, evidence, details },
  }
}

// 材料的值: 字符串原样, 其余写成 JSON
export function materialText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}
