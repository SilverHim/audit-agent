// 程序详情页执行记录下的 "未执行的规则" (需求 R300): ProcedureRun.rule_outcomes (需求 R130) 记下执行前检查命中而没有执行的
// 规则, 状态与理由; 执行了的规则不记. 纯函数, bun test 覆盖.
import type { ProcedureRun, RuleOutcome } from '@/client/types.gen'

export type RuleOutcomeStatus = RuleOutcome['status']

// 两种状态的说明顺序: 本该执行而缺数据或参数的 (not_executable, 要补齐后重跑) 在前, 按设计不适用的 (skipped) 在后
const STATUS_ORDER: RuleOutcomeStatus[] = ['not_executable', 'skipped']

// 一次执行没有执行的规则, 按记录的顺序 (程序 YAML 中规则的顺序); 全部执行了 (或旧记录没有这一项) 为空
export function notRunRules(run: Pick<ProcedureRun, 'rule_outcomes'>): RuleOutcome[] {
  return run.rule_outcomes ?? []
}

// 列表下方要说明含义的状态: 只说出现过的, 按 STATUS_ORDER
export function outcomeStatuses(outcomes: RuleOutcome[]): RuleOutcomeStatus[] {
  return STATUS_ORDER.filter((status) => outcomes.some((outcome) => outcome.status === status))
}

// 状态标签的色调: not_executable 为提醒色 (结果不完整, 要处理), skipped 为中性色 (按设计不适用)
export function outcomeTone(status: RuleOutcomeStatus): 'warn' | 'neutral' {
  return status === 'not_executable' ? 'warn' : 'neutral'
}
