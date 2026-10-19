import type { ConciergeCitation, GateId, Phase, TrailEntry } from '@/client/types.gen'

// 把审计留痕整理成对话流里的事件: 阶段变化, 程序完成 (相邻的合并), 人工决定 (理由原文), 稍后处理, 数据管理, 历史问答

type TimelineEvent =
  | { kind: 'created'; at: string }
  | { kind: 'phase'; at: string; phase: Phase }
  | { kind: 'completed'; at: string; ids: string[] }
  | { kind: 'decision'; at: string; target: string; gate: GateId | null; verdict: string; reason: string | null }
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

const GATES: GateId[] = ['ACC-01', 'PLN-01/03', 'FIN-01', 'RPT-01']

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

export function buildTimeline(entries: TrailEntry[]): TimelineItem[] {
  const sorted = [...entries].sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))
  const items: TimelineItem[] = []
  let completed: { at: string; key: string; ids: string[] } | null = null
  const flush = () => {
    if (completed?.ids.length) items.push({ kind: 'completed', ...completed })
    completed = null
  }

  if (sorted[0]?.created_at) items.push({ kind: 'created', key: 'created', at: sorted[0].created_at })

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
