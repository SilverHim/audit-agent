import type { Engagement, EngagementOverview } from '@/client/types.gen'

// 项目的工作语言 (需求 R339, Issue #51): 建项目时选 (缺省取界面语言), 项目页可改; 每次执行记下当时的语言, 判断, 计划
// 与报告的生成语言与当前工作语言不同时标注. 纯函数

export type WorkingLanguage = NonNullable<Engagement['working_language']>

export const WORKING_LANGUAGES: WorkingLanguage[] = ['zh', 'en']

// 项目当前的工作语言; 后端缺省 en (之前建的项目)
export function currentLanguage(overview: EngagementOverview | undefined): WorkingLanguage {
  return overview?.engagement.working_language ?? 'en'
}

// 要标注的生成语言: 总览的 run_languages 只列生成语言与当前工作语言不同的执行 (执行 ID → ProcedureRun.language),
// 取到即标; 没有 run_id (如迁移前的报告稿), 查不到 (语言相同) 或总览还没取到时为 null, 不标
export function generatedNote(
  overview: EngagementOverview | undefined,
  runId: string | null | undefined,
): WorkingLanguage | null {
  return (runId && overview?.run_languages?.[runId]) || null
}

// 生成内容 (判断, 计划, 报告正文) 的语言, 给 lang 属性 (需求 R340): 执行记录的语言 (与当前工作语言不同的在
// run_languages 里, 否则就是当前工作语言); 没有 run_id (迁移前的报告稿) 为 en, 那时的输出都是英文
export function contentLanguage(
  overview: EngagementOverview | undefined,
  runId: string | null | undefined,
): WorkingLanguage {
  if (!runId) return 'en'
  return generatedNote(overview, runId) ?? currentLanguage(overview)
}
