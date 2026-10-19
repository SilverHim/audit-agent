import type { ReviewTaskView, TaskPresentation } from '@/client/types.gen'
import { isJudgment } from '@/lib/review'

// 判断呈递 (后端 R152, GEN-11) 在前端的几条判断: 何时呈递, 视图属于哪条任务, 判断卡用哪种版式, 何时省掉两方的建议

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
