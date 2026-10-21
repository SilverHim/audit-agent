import type { Activity, ActivityFailure, EngagementOverview } from '@/client/types.gen'
import { procedureName } from '@/lib/review'
import type { Messages } from '@/locales/en'

// 系统忙时说明正在做什么 (需求 R362): 总览的当前活动与最近一次失败 (后端 R361) 拼成状态条与变灰按钮旁的说明,
// 已用时间与进度停滞. 状态条 (components/activity-bar) 与按钮旁的说明 (BusyNote) 都从这里取, 不各写一套

export type { Activity, ActivityFailure } from '@/client/types.gen'

export const ELAPSED_AFTER_MS = 30_000 // 超过 30 秒显示已用时间
export const STALL_AFTER_MS = 120_000 // 进度 (updated_at) 2 分钟没变提示仍在进行

// 在执行而后端没列活动时 (如图在两段程序之间推进) 的一项: 只说系统在处理, 不带进度
const WORKING: Activity = {
  kind: 'data_edit',
  actor: 'system',
  description: '',
  started_at: '',
  updated_at: '',
}

// 当前活动: 总览列的; 没列而在执行时为 WORKING 一项, 空闲为空
export function currentActivities(overview: EngagementOverview | undefined): Activity[] {
  if (!overview) return []
  if (overview.activities?.length) return overview.activities
  return overview.running ? [WORKING] : []
}

export function busy(overview: EngagementOverview | undefined): boolean {
  return currentActivities(overview).length > 0
}

function actorName(actor: Activity['actor'], t: Messages): string {
  return t.activity.actors[actor] ?? t.activity.actors.system
}

// 活动的说明: 按结构化字段 (种类, 执行者, 进度, 当前对象) 用界面语言拼. 逐份处理的写 "7/19" (如 "数据管理智能体正在
// 识别文件 7/19"); 程序执行写程序编号与名称 (procedureNames) 及本阶段的进度. WORKING 为 "系统正在处理"; 认不出的种类
// (前端比后端旧) 在中文界面照后端的描述 (中文) 兜底, 英文界面不显示它
export function activityText(activity: Activity, t: Messages): string {
  if (activity === WORKING) return t.activity.working
  const kind = t.activity.kinds[activity.kind]
  if (!kind) return (t.activity.backendDescription && activity.description) || t.activity.working
  const actor = actorName(activity.actor, t)
  const total = activity.total ?? 0
  if (activity.kind === 'procedure') {
    const id = activity.current ?? activity.subject_id ?? ''
    const head = kind.doing(actor, id ? `${id} ${procedureName(t, id)}` : '')
    return total > 0 ? `${head}${t.activity.phaseProgress(activity.done ?? 0, total)}` : head
  }
  const head = kind.doing(actor, '')
  return total > 0 ? `${head} ${activity.done ?? 0}/${total}` : head
}

// 当前对象 (在处理的文件名); 程序执行的当前对象是程序编号, 已写进说明
export function activityCurrent(activity: Activity): string | null {
  return activity.kind === 'procedure' ? null : activity.current || null
}

// 因忙变灰的按钮旁的说明: 正在进行的活动与进度, 完成后可继续; 不忙为 null (锁定与执行出错另有提示)
export function busyText(overview: EngagementOverview | undefined, t: Messages): string | null {
  const [first] = currentActivities(overview)
  return first ? t.activity.busy(activityText(first, t)) : null
}

export function durationText(ms: number, t: Messages): string {
  const seconds = Math.floor(Math.max(0, ms) / 1000)
  return t.activity.duration(Math.floor(seconds / 60), seconds % 60)
}

export function activityKey(activity: Activity): string {
  return `${activity.kind}:${activity.actor}:${activity.subject_id ?? activity.current ?? ''}:${activity.started_at}`
}

// 状态条一项活动的时间提示: 进度 (updated_at) 2 分钟没变时 "仍在进行, 已用时 x 分钟", 否则超过 30 秒显示已用时间,
// 不到或时间不明为 null
export function timingText(activity: Activity, now: number, t: Messages): string | null {
  const started = Date.parse(activity.started_at)
  if (Number.isNaN(started)) return null
  const elapsed = now - started
  const updated = Date.parse(activity.updated_at)
  if (!Number.isNaN(updated) && now - updated >= STALL_AFTER_MS) return t.activity.stalled(Math.floor(elapsed / 60_000))
  return elapsed > ELAPSED_AFTER_MS ? t.activity.elapsed(durationText(elapsed, t)) : null
}

// 失败的说明: 哪类作业失败与原因 (原文)
export function failureText(failure: ActivityFailure, t: Messages): string {
  const kind = t.activity.kinds[failure.kind]
  return t.activity.failed(kind ? kind.name : t.activity.working, failure.reason)
}

// "知道了" 关掉的失败按种类与时刻认 (同种作业再开始时后端清除, 新失败取代旧的)
export function failureKey(failure: ActivityFailure): string {
  return `${failure.kind}:${failure.subject_id ?? ''}:${failure.failed_at}`
}
