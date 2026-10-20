import type { Engagement, EngagementOverview } from '@/client/types.gen'

// 项目的工作语言 (需求 R339, Issue #51): 建项目时选 (缺省取界面语言), 项目页可改; 每次执行记下当时的语言, 判断, 计划
// 与报告的生成语言与当前工作语言不同时标注. 纯函数

export type WorkingLanguage = NonNullable<Engagement['working_language']>

export const WORKING_LANGUAGES: WorkingLanguage[] = ['zh', 'en']

// 项目当前的工作语言; 后端缺省 en (之前建的项目)
export function currentLanguage(overview: EngagementOverview | undefined): WorkingLanguage {
  return overview?.engagement.working_language ?? 'en'
}

// 一次执行的生成语言 (总览的 run_languages, 执行 ID → ProcedureRun.language); 没有 run_id 或查不到为 null
export function runLanguage(
  overview: EngagementOverview | undefined,
  runId: string | null | undefined,
): WorkingLanguage | null {
  return (runId && overview?.run_languages?.[runId]) || null
}

// 要标注的生成语言: 与当前工作语言不同时为生成语言, 相同或不知道 (总览还没取到, 查不到执行) 时为 null
export function generatedNote(
  generated: WorkingLanguage | null | undefined,
  overview: EngagementOverview | undefined,
): WorkingLanguage | null {
  if (!generated || !overview) return null
  return generated === currentLanguage(overview) ? null : generated
}
