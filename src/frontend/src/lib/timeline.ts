import type { ConciergeCitation, CorrectionNotice, GateId, Phase, TrailEntry, TrailKind } from '@/client/types.gen'
import { WORKING_LANGUAGES, type WorkingLanguage } from '@/lib/working-language'

// 把审计留痕整理成对话流里的事件: 阶段变化, 程序完成 (相邻的合并), 人工决定 (理由原文), 更正通知, 工作语言的修改,
// 稍后处理, 数据管理, 历史问答

type TimelineEvent =
  | { kind: 'created'; at: string }
  | { kind: 'phase'; at: string; phase: Phase }
  | { kind: 'completed'; at: string; ids: string[] }
  | { kind: 'decision'; at: string; target: string; gate: GateId | null; verdict: string; reason: string | null }
  | {
      kind: 'correction'
      at: string
      notice: CorrectionNotice['kind']
      ids: string[]
      entries: string[]
      reason: string | null
    }
  | { kind: 'currency'; at: string; from: string; to: string }
  | { kind: 'language'; at: string; from: WorkingLanguage; to: WorkingLanguage }
  | { kind: 'deferral'; at: string; target: string; deferred: boolean; request: string | null; xero: boolean }
  | {
      kind: 'qa'
      at: string
      id: string
      question: string
      answer: string
      citations: ConciergeCitation[]
      aborted: boolean
    }
  | {
      kind: 'data'
      at: string
      id: string
      action: string
      table: string | null
      document: string | null
      records: number | null
      file: string | null
      organisation: string | null // Xero 连接, 选组织与断开的组织名
      revoked: boolean | null // Xero 断开时 Xero 侧是否已撤销授权
      reason: string | null // Xero 同步失败的原因代码 (expired, xero_error, forbidden, settings_key, mapping, write); 保持客户画像的理由
      skipped: Record<string, string> | null // Xero 同步没写的表 → 原因代码 (paid_off, statements_forbidden)
      version: number | null // 按 Xero 组织信息写出的客户画像版本 (xero_profile_updated)
      profileVersion: number | null // 保持原样时本项目采用的画像版本 (xero_profile_kept)
      adopted: boolean | null // 按 Xero 更新时本项目是否采用了新版本
    }

// key: 列表渲染用的 key, 取条目来源留痕的 ID (合并的程序完成取第一条), 刷新前后不变. 同类条目的时间可能相同
// (同一事务写入) 或为空, 不能用时间拼 key. 留痕缺 ID 时退回它在按时间排序后的序号.
export type TimelineItem = TimelineEvent & { key: string }

const GATES: GateId[] = ['ACC-01', 'PLN-01/03', 'PLN-15', 'FIN-01', 'RPT-01']

// 任务 ID 形如 T-J-REV-02-1-SI-2026-0612-v1, 取判断对象 SI-2026-0612
export function subjectOfTask(taskId: string): string {
  const match = taskId.match(/^T-[A-Z]+-([A-Z]+-\d+)-(\d+)-(.+)-v\d+$/)
  return match?.[3] ?? taskId
}

// Xero 同步完成留痕 (xero_synced) 的 skipped: 表种类 → 原因代码; 不是对象或为空时为 null
function skippedTables(value: unknown): Record<string, string> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const entries = Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === 'string')
  return entries.length ? Object.fromEntries(entries) : null
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.length ? value : null
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

const NOTICE_KINDS: CorrectionNotice['kind'][] = ['adjustments_posted', 'stop_waiting']

function language(value: unknown): WorkingLanguage | null {
  return WORKING_LANGUAGES.find((item) => item === value) ?? null
}

// 工作台按页取留痕: 先取最近的一页 (GET /trail?newest=true), "更早" 以已取到的最早一条为游标 (before) 往前翻一页.
// 只取 buildTimeline 会变成事件的几类 (TIMELINE_KINDS), 呈递, 大模型调用等不显示的留痕不占每页的名额
export const TRAIL_PAGE = 100
export const TIMELINE_KINDS: TrailKind[] = ['transition', 'decision', 'deferral', 'data', 'conversation']

// 往前一页的游标: 本页取满时为本页最早一条的 ID; 不满说明已取到最早一条, 没有更早的了
export function earlierCursor(page: TrailEntry[], size: number = TRAIL_PAGE): string | undefined {
  return page.length >= size ? (page[0]?.id ?? undefined) : undefined
}

// 已取到的各页 (最近的一页在前, 每页内按写入顺序) 拼成按写入顺序的留痕
export function trailInOrder(pages: TrailEntry[][]): TrailEntry[] {
  return [...pages].reverse().flat()
}

// 最新一条留痕的 ID (跳过 skip 中的, 如本页刚问的问答): 有新事件时才变, 往前翻页不变 (工作台据此滚到当前事项;
// 不能看最后一项的 key: 跨页的 "程序完成" 合并项取第一条的 ID, 翻出更早一页时会变)
export function newestTrailId(pages: TrailEntry[][], skip: ReadonlySet<string> = new Set()): string | undefined {
  for (const page of pages) {
    const found = page.findLast((entry) => entry.id && !skip.has(entry.id))
    if (found?.id) return found.id
  }
  return undefined
}

// complete: 是否已取到最早一条留痕; 没取到时不加 "项目已创建" (它取自最早一条的时间)
export function buildTimeline(entries: TrailEntry[], complete = true): TimelineItem[] {
  const sorted = [...entries].sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
  const items: TimelineItem[] = []
  let completed: { at: string; key: string; ids: string[] } | null = null
  const flush = () => {
    if (completed?.ids.length) items.push({ kind: 'completed', ...completed })
    completed = null
  }

  if (complete && sorted[0]?.created_at) items.push({ kind: 'created', key: 'created', at: sorted[0].created_at })

  for (const [index, entry] of sorted.entries()) {
    const at = entry.created_at ?? ''
    const key = entry.id ?? `#${index}`
    const payload = (entry.payload ?? {}) as Record<string, unknown>
    const kind = entry.kind as string
    if (kind === 'transition') {
      if (payload.object === 'engagement' && str(payload.to)) {
        flush()
        items.push({ kind: 'phase', key, at, phase: payload.to as Phase })
      } else if (payload.object === 'procedure' && payload.to === 'completed' && str(payload.id)) {
        if (!completed) completed = { at, key, ids: [] }
        const id = payload.id as string
        if (!completed.ids.includes(id)) completed.ids.push(id)
      }
      continue
    }
    if (kind === 'decision') {
      flush()
      // 更正通知 (需求 R162): 载荷带通知种类, 所列错报与凭证号; 摘要不按 "对象 决定" 的格式, 不拆
      // 改工作语言 (需求 R339, decisions.language_trail): 载荷带改前与改后的值, 显示为一行事件
      const [from, to] = [language(payload.from), language(payload.to)]
      if (payload.action === 'working_language' && from && to) {
        items.push({ kind: 'language', key, at, from, to })
        continue
      }
      const notice = NOTICE_KINDS.find((item) => item === payload.kind)
      if (notice) {
        const [ids, entries] = [strings(payload.misstatement_ids), strings(payload.adjustment_entry_nos)]
        items.push({ kind: 'correction', key, at, notice, ids, entries, reason: str(payload.reason) })
        continue
      }
      const parts = entry.summary.trim().split(/\s+/)
      const verdict = parts.at(-1) ?? ''
      const head = parts.slice(0, -1).join(' ')
      const gate = GATES.find((item) => item === head) ?? null
      const target = gate ?? (str(payload.task_id) ? subjectOfTask(payload.task_id as string) : head)
      items.push({ kind: 'decision', key, at, target, gate, verdict, reason: str(payload.reason) })
      continue
    }
    if (kind === 'deferral') {
      flush()
      const id = str(payload.target_id) ?? ''
      const target = payload.target_type === 'review_task' ? subjectOfTask(id) : id
      // 补交请求 ID 固定为 REQ-{表种类或文件类型}, 显示时换成资料名
      const request = payload.target_type === 'data_request' ? id.replace(/^REQ-/, '') : null
      // Xero 组织信息核对的 target_id 是核对的 key, 显示时换成事项名
      const xero = payload.target_type === 'xero_profile'
      items.push({ kind: 'deferral', key, at, target, deferred: payload.deferred === true, request, xero })
      continue
    }
    if (kind === 'data') {
      flush()
      if (payload.action === 'currency' && str(payload.from) && str(payload.to)) {
        items.push({ kind: 'currency', key, at, from: String(payload.from), to: String(payload.to) })
        continue
      }
      items.push({
        kind: 'data',
        key,
        at,
        id: entry.id ?? at,
        action: str(payload.action) ?? '',
        table: str(payload.table_kind),
        document: str(payload.document_type),
        records: typeof payload.records === 'number' ? payload.records : null,
        file: str(payload.file_name),
        organisation: str(payload.organisation),
        revoked: typeof payload.revoked === 'boolean' ? payload.revoked : null,
        reason: str(payload.reason),
        skipped: skippedTables(payload.skipped),
        version: typeof payload.version === 'number' ? payload.version : null,
        profileVersion: typeof payload.profile_version === 'number' ? payload.profile_version : null,
        adopted: typeof payload.adopted === 'boolean' ? payload.adopted : null,
      })
      continue
    }
    if (kind === 'conversation') {
      flush()
      items.push({
        kind: 'qa',
        key,
        at,
        id: entry.id ?? at,
        question: str(payload.question) ?? entry.summary,
        answer: str(payload.answer) ?? '',
        citations: Array.isArray(payload.citations) ? (payload.citations as ConciergeCitation[]) : [],
        aborted: payload.aborted === true,
      })
    }
  }
  flush()
  return items
}
