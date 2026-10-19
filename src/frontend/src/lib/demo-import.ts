import type { DemoImportResult, DemoSummary } from '@/client/types.gen'
import type { Messages } from '@/locales/en'

// 数据管理页的 "导入演示资料" (后端需求 R269, R270): 按钮与上次结果的文字都按概览的 demo 一项给出, 前端不自己推流程位置

export type DemoStage = NonNullable<DemoSummary['suggested']>

export type DemoButton = {
  stage: DemoStage | null // 点了导入的阶段; 为空时按钮不可点
  label: string
  hint: string | null // 不可点时的说明
}

// 没有演示资料的数据集 (available 为假) 不显示按钮, 返回 null; 有资料而流程不在要导入的位置 (或这一阶段已导入过) 时
// 按钮在但不可点, 附说明
export function demoButton(demo: DemoSummary | undefined, t: Messages): DemoButton | null {
  if (!demo?.available) return null
  const stage = demo.suggested ?? null
  if (stage === null) return { stage, label: t.data.demo.button, hint: t.data.demo.noStage }
  return { stage, label: t.data.demo.importStage(t.data.demo.stages[stage]), hint: null }
}

// 上次导入的一行: 阶段, 写了几张表, 上传几份, 按清单改了类型的与待人工的文件; 什么都没写的为 "已导入过"; 作业中途
// 出错的写错误类别并提示再导入一次 (已写的保留, 接着做)
export function demoResultText(result: DemoImportResult, t: Messages): string {
  const stage = t.data.demo.stages[result.stage]
  if (result.status === 'already_imported') return t.data.demo.already(stage)
  if (result.status === 'failed') return t.data.demo.failed(stage, result.error ?? '')
  const parts = [t.data.demo.done(stage, result.tables?.length ?? 0, result.uploaded?.length ?? 0)]
  if (result.retyped?.length) parts.push(t.data.demo.retyped(result.retyped.join(', ')))
  if (result.needs_manual?.length) parts.push(t.data.demo.needsManual(result.needs_manual.join(', ')))
  return parts.join(t.data.demo.separator)
}
