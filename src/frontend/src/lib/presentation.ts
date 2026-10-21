import type { EvidencePointer, PresentedFailure, ReviewTaskView, TaskPresentation } from '@/client/types.gen'
import { isJudgment } from '@/lib/review'

// 判断呈递 (后端 R152, GEN-11) 在前端的几条判断: 何时呈递, 视图属于哪条任务, 判断卡用哪种版式, 何时省掉两方的建议,
// 失败原因一栏显示哪几段

// 同一次挂载里同一条任务只呈递一次 (StrictMode 的第二次 effect, 重新渲染与轮询刷新都不再调), 换一条任务再呈递.
// last 为这次挂载里上次呈递的键 (没有为 null). 纯函数
export function presentationKey(eid: string, taskId: string): string {
  return `${eid}:${taskId}`
}

export function needsPresentation(last: string | null, eid: string, taskId: string): boolean {
  return last !== presentationKey(eid, taskId)
}

// 返回的呈递视图只用于它自己的任务: 换任务时上一条的视图还在, 不拿来显示这一条. 纯函数
export function presentationFor(data: TaskPresentation | undefined, taskId: string): TaskPresentation | undefined {
  return data?.task.id === taskId ? data : undefined
}

// 判断卡的版式: 目标不是判断 (例外或执行记录) 为失败与规则发现卡; 争议任务的呈递回来后 (有复核方) 两方并排;
// 其余 (含呈递还没回来或失败) 为原来的判断卡, 只用复核队列的数据. 纯函数
export type CardLayout = 'target' | 'dispute' | 'judgment'

export function cardLayout(view: ReviewTaskView, presentation: TaskPresentation | undefined): CardLayout {
  if (!isJudgment(view.target)) return 'target'
  return presentation?.sides?.reviewer ? 'dispute' : 'judgment'
}

// 两方并排时, 各栏是否显示本方的建议动作与错报草稿: 功能 collab 打开且任务待决时, 决定卡的 "两方的建议" (R150) 已并排
// 显示, 这里不重复; 已决定的任务不出决定卡, collab 关闭时决定卡没有这一栏, 都在两栏里显示. 纯函数
export function showSideProposals(collab: boolean, view: ReviewTaskView): boolean {
  return !(collab && view.task.status === 'pending')
}

// 失败原因一栏 (呈递的 failure) 依次显示的几段: 无归因的例外先给规则与明细 (rule); 大模型调用没通过的几项 (call:
// 调用次数与没通过的校验; errors: 错误原文) 带对象的失败与无归因的例外 (需求 R355) 都给, 同一种显示. 整次执行失败
// (run) 的原因已在目标上, 不重复, 为 null. 纯函数
export type FailureParts = {
  rule: { id: string | null; detail: Record<string, unknown> } | null
  call: { attempts: number; level: NonNullable<PresentedFailure['level_failed']> } | null
  errors: string[]
}

export function failureParts(failure: PresentedFailure | null | undefined): FailureParts | null {
  if (!failure || failure.source === 'run') return null
  const rule = failure.source === 'exception' ? { id: failure.rule_id ?? null, detail: failure.detail ?? {} } : null
  const call =
    failure.attempts != null && failure.level_failed
      ? { attempts: failure.attempts, level: failure.level_failed }
      : null
  return { rule, call, errors: failure.errors ?? [] }
}

// 证据指针列表的 React key: 来源, ID, locator 与页. 后端去重键 (judgment_engine.evidence_key) 不同的指针 key 都不同:
// 同一文件的不同页, 同一画像的不同字段 (需求 R128) 各算一项; 同一页的不同 locator 也分开 (PointerList 本来如此). 纯函数
export function pointerKey(pointer: Pick<EvidencePointer, 'source_type' | 'source_id' | 'locator' | 'page'>): string {
  return `${pointer.source_type}:${pointer.source_id}:${pointer.locator}:${pointer.page ?? ''}`
}

// 证据弹窗里字段名的显示: 去掉 _cents 后缀, 下划线换空格 (数据行与客户画像的字段共用). 纯函数
export function humanize(key: string): string {
  return key.replace(/_cents$/, '').replaceAll('_', ' ')
}
