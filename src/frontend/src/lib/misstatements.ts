import type {
  CorrectionNoticeIn,
  Decision,
  ManualTaskView,
  Misstatement,
  MisstatementStatus,
  MisstatementType,
  MisstatementUpdate,
  ReviewTaskView,
} from '@/client/types.gen'
import { audToCents } from '@/lib/money'
import type { Messages } from '@/locales/en'

// 错报生命周期的前端计算 (后端需求 R156, R159, R161, R162; 页面需求 R163): 生命周期的显示, 更正通知, FIN-08 的登记,
// 复核表单的错报更新载荷. 都是纯函数, 校验以后端为准, 这里只挡显而易见的漏填

// ── 生命周期 (错报页与 FIN-01 闸门材料共用) ──

// 剩余未更正的金额: 验证过 (residual_cents 有值) 取它, 之前为金额减已更正额
export function remainingCents(item: Misstatement): number {
  return item.residual_cents ?? item.amount_cents - (item.corrected_cents ?? 0)
}

// 科目代码另列与否: 科目名已带代码 (Xero 的 "Office equipment (710)") 时不重复, 否则为代码; 没有代码为 null
export function separateCode(item: { account: string; account_code?: string | null }): string | null {
  const code = item.account_code
  return code && !item.account.includes(`(${code})`) ? code : null
}

// 已验证的更正凭证号: corrected 或 partially_corrected 且有验证判断时的凭证号 (同后端 verified_adjustment_entries 的口径);
// verifying 时的凭证号还没验证, 不算
export function verifiedEntries(item: Misstatement): string[] {
  const verified = item.status === 'corrected' || item.status === 'partially_corrected'
  return verified && item.verified_by_judgment_id ? [...(item.adjustment_entry_nos ?? [])] : []
}

// ── 等待更正: 更正通知 (POST /correction-notices) ──

export type NoticeDraft = {
  kind: CorrectionNoticeIn['kind']
  misstatementIds: string[] // 勾选的待更正错报
  entryNos: string[] // 勾选的更正分录 (只用于客户已过账)
  reason: string
}

// 更正通知的请求体; 漏选或缺理由时返回错误文案. 客户已过账须勾选错报与凭证号, 理由可空; 停止等待须写理由, 不带凭证号
export function correctionNoticeBody(draft: NoticeDraft, t: Messages): CorrectionNoticeIn | string {
  if (!draft.misstatementIds.length) return t.correction.errors.misstatements
  const reason = draft.reason.trim()
  if (draft.kind === 'stop_waiting') {
    if (!reason) return t.errors.reasonRequired
    return { kind: 'stop_waiting', misstatement_ids: draft.misstatementIds, adjustment_entry_nos: [], reason }
  }
  if (!draft.entryNos.length) return t.correction.errors.entries
  return {
    kind: 'adjustments_posted',
    misstatement_ids: draft.misstatementIds,
    adjustment_entry_nos: draft.entryNos,
    reason: reason || null,
  }
}

// ── FIN-08 的登记 (人工任务的结果 ManagementResponses, 需求 R161): 材料中的错报每条一行 ──

// 材料里列的一条错报 (A8 prepare 的 LISTED_FIELDS, JSON 形式)
export type ListedMisstatement = {
  id: string
  version: number
  account: string
  account_code: string | null
  amount_cents: number
  direction: Misstatement['direction']
  type: MisstatementType
  fraud_indicative: boolean | null
  source_conclusion: string | null
}

// 一行登记的表单值: 两个时刻为 datetime-local 的文本 (本机时区); 是否同意更正为三态, 缺省未选 (null): 管理层拒绝更正
// 的一路 (ASA 450 第 9 段) 不能被缺省值悄悄带过, 未选的行同漏填一样提示
export type ResponseRow = {
  communicatedAt: string
  communicatedTo: string
  respondedAt: string
  response: string
  agrees: boolean | null
}

export const RESPONSES_MODEL = 'ManagementResponses' // FIN-08 模板的 manual.result_model

export function emptyResponseRow(): ResponseRow {
  return { communicatedAt: '', communicatedTo: '', respondedAt: '', response: '', agrees: null }
}

// 任务的材料是否按错报逐条登记 (结果模型为 ManagementResponses); 是时返回材料中的错报, 否则为 null
export function listedMisstatements(view: ManualTaskView): ListedMisstatement[] | null {
  if (view.request.result_model !== RESPONSES_MODEL) return null
  const listed = (view.request.material as { misstatements?: ListedMisstatement[] }).misstatements
  return Array.isArray(listed) ? listed : []
}

// datetime-local 的文本 → 带时区的 ISO 时刻 (按本机时区解读); 填不对为 null
function instant(text: string): string | null {
  if (!text) return null
  const time = new Date(text)
  return Number.isNaN(time.getTime()) ? null : time.toISOString()
}

export type ResponsesResult = { details: { responses: unknown[] } } | { errors: Record<string, string> }

// 逐行登记 → details {responses}; 每行的必填与先后在这里先查, 有问题的行各给一条文案 (按错报 ID), 都没问题才给 details.
// 依据的版本取材料中的; 回复不能早于沟通 (后端 ManagementResponse 同样校验)
export function responsesDetails(
  listed: ListedMisstatement[],
  rows: Record<string, ResponseRow>,
  t: Messages,
): ResponsesResult {
  const errors: Record<string, string> = {}
  const responses = listed.map((item) => {
    const row = rows[item.id] ?? emptyResponseRow()
    const communicated = instant(row.communicatedAt)
    const responded = instant(row.respondedAt)
    const missing = [
      communicated ? null : t.fin08.fields.communicatedAt,
      row.communicatedTo.trim() ? null : t.fin08.fields.communicatedTo,
      responded ? null : t.fin08.fields.respondedAt,
      row.response.trim() ? null : t.fin08.fields.response,
      row.agrees === null ? t.fin08.fields.agrees : null,
    ].filter((label): label is string => label !== null)
    if (missing.length) errors[item.id] = t.fin08.errors.missing(missing.join(t.fin08.separator))
    else if (communicated && responded && responded < communicated) errors[item.id] = t.fin08.errors.order
    return {
      misstatement_id: item.id,
      base_version: item.version,
      communicated_at: communicated,
      communicated_to: row.communicatedTo.trim(),
      responded_at: responded,
      response: row.response.trim(),
      agrees_to_correct: row.agrees,
    }
  })
  return Object.keys(errors).length ? { errors } : { details: { responses } }
}

// ── 认定错报时的科目代码 (从本项目科目表选, 需求 R163) ──

// 科目表的行 (账表接口 GET /data/tables/accounts 的 rows) → 下拉的选项, 按代码; 当前值 (如 AI 草稿带入的) 不在表里时
// 也列上, 免得选项里没有它而被悄悄清空 (提交时后端查它在本项目科目表中)
export function accountOptions(
  rows: Array<Record<string, unknown>>,
  current: string,
): Array<{ code: string; name: string | null }> {
  const options = rows
    .filter((row) => typeof row.code === 'string')
    .map((row) => ({ code: String(row.code), name: typeof row.name === 'string' ? row.name : null }))
  if (current && !options.some((item) => item.code === current)) options.push({ code: current, name: null })
  return options.sort((a, b) => a.code.localeCompare(b.code, undefined, { numeric: true }))
}

// ── 复核表单的错报更新载荷 (需求 R159): 带载荷类别的程序 (FIN-03 分类, FIN-09 验证) 中以人工判断取代 AI 判断的决定 ──

// 这次决定须带哪一类载荷: 须带载荷的决定由后端给 (ReviewTaskView.update_decisions, 按 decisions.UPDATE_BY_HUMAN),
// 类别取 misstatement_update_kind; 不须带时为 null. 其余决定确认的是 AI 判断本身, 不能另填
export function updateKindFor(view: ReviewTaskView, decision: Decision | null): MisstatementUpdate['kind'] | null {
  const kind = view.misstatement_update_kind ?? null
  if (!kind || !decision) return null
  return (view.update_decisions ?? []).includes(decision) ? kind : null
}

export const VERIFIED_STATUSES: MisstatementStatus[] = ['corrected', 'partially_corrected', 'communicated']

export type UpdateDraft = {
  type: MisstatementType
  fraud: boolean
  fraudReason: string
  status: MisstatementStatus
  entryNos: string // 凭证号, 逗号或空白分隔
  corrected: string // AUD
  residual: string // AUD
}

export function emptyUpdate(): UpdateDraft {
  return {
    type: 'factual',
    fraud: false,
    fraudReason: '',
    status: 'corrected',
    entryNos: '',
    corrected: '',
    residual: '',
  }
}

// AI 判断带的载荷填进表单 (推翻时以它为起点改)
export function updateFromPayload(payload: MisstatementUpdate): UpdateDraft {
  const cents = (value: number | null | undefined) => (value == null ? '' : (value / 100).toFixed(2))
  return {
    type: payload.type ?? 'factual',
    fraud: payload.fraud_indicative ?? false,
    fraudReason: payload.fraud_reason ?? '',
    status: payload.status ?? 'corrected',
    entryNos: (payload.adjustment_entry_nos ?? []).join(', '),
    corrected: cents(payload.corrected_cents),
    residual: cents(payload.residual_cents),
  }
}

// 载荷所指的错报与依据的版本: 目标是判断时取它带的载荷; 例外与失败任务取任务的对象 (subject_id) 在错报列表中的当前版本.
// 找不到为 null (错报列表还没取到, 或对象不是错报)
export function updateBase(
  view: ReviewTaskView,
  misstatements: Misstatement[],
): { misstatement_id: string; base_version: number } | null {
  const target = view.target
  const carried = 'conclusion' in target ? target.misstatement_update : null
  if (carried) return { misstatement_id: carried.misstatement_id, base_version: carried.base_version }
  const found = misstatements.find((item) => item.id === view.task.subject_id)
  return found ? { misstatement_id: found.id, base_version: found.version ?? 1 } : null
}

// 表单 → 载荷; 缺依据或必填, 金额写不对时返回错误文案. 分类: 类型与是否舞弊迹象, 有舞弊迹象须写理由 (来源与审计目标
// 由后端按规则补); 验证: 状态, 凭证号, 已更正与剩余金额. 没写的可选字段为 null (表示不改): 凭证号为空不发 [];
// 是否须重新沟通不由人填, 恒为 null, 由后端按状态推出
export function misstatementUpdate(
  kind: MisstatementUpdate['kind'],
  base: { misstatement_id: string; base_version: number } | null,
  draft: UpdateDraft,
  t: Messages,
): MisstatementUpdate | string {
  if (!base) return t.updateForm.errors.base
  if (kind === 'classification') {
    const reason = draft.fraudReason.trim()
    if (draft.fraud && !reason) return t.updateForm.errors.fraudReason
    return { ...base, kind, type: draft.type, fraud_indicative: draft.fraud, fraud_reason: draft.fraud ? reason : null }
  }
  const amount = (text: string) => (text.trim() ? audToCents(text) : null)
  const corrected = amount(draft.corrected)
  const residual = amount(draft.residual)
  if ((draft.corrected.trim() && corrected == null) || (draft.residual.trim() && residual == null))
    return t.updateForm.errors.amount
  const entries = draft.entryNos.split(/[\s,]+/).filter(Boolean)
  return {
    ...base,
    kind,
    status: draft.status,
    adjustment_entry_nos: entries.length ? entries : null,
    corrected_cents: corrected,
    residual_cents: residual,
    needs_recommunication: null,
  }
}
