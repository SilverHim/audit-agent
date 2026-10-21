import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { engagementsSetWorkingLanguageMutation } from '@/client/@tanstack/react-query.gen'
import { StatusPill } from '@/components/chrome'
import { invalidateEngagement } from '@/lib/engagement'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useOverview } from '@/lib/use-engagement'
import { currentLanguage, generatedNote, WORKING_LANGUAGES } from '@/lib/working-language'

// 工作语言 (需求 R339): "以英文生成 / 以中文生成" 的标注 (判断, 计划, 报告等处共用) 与项目页的修改入口

// 内容的生成语言与项目当前的工作语言不同时显示 "以英文生成" 或 "以中文生成": 按内容的 run_id 在总览的 run_languages
// 里取 (只列语言不同的执行), 查不到不标
export function GeneratedLanguage({ eid, runId }: { eid: string; runId: string | null | undefined }) {
  const { t } = useI18n()
  const shown = generatedNote(useOverview(eid), runId)
  if (!shown) return null
  return (
    <span data-testid="generated-language">
      <StatusPill tone="neutral">{t.workingLanguage.generatedIn[shown]}</StatusPill>
    </span>
  )
}

// 项目页改工作语言: 下拉即提交 (PUT working-language), 之后的执行用新语言, 已生成的内容不改; 项目锁定时不可改
export function WorkingLanguageSelect({ eid }: { eid: string }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const overview = useOverview(eid)
  const change = useMutation({
    ...engagementsSetWorkingLanguageMutation(),
    onSuccess: async (engagement) => {
      toast.success(t.workingLanguage.changed(t.workingLanguage.names[engagement.working_language ?? 'en']))
      await invalidateEngagement(queryClient, eid)
    },
    onError: (error) => toast.error(errorText(error, t)),
  })
  if (!overview) return null
  return (
    <label className="flex items-center gap-2 text-[12.5px] text-muted-foreground" title={t.workingLanguage.hint}>
      {t.workingLanguage.label}
      <select
        id="working-language"
        className="h-9 rounded-lg border border-input bg-card px-2 text-[13px] text-foreground"
        value={currentLanguage(overview)}
        disabled={overview.engagement.locked || change.isPending}
        onChange={(event) => change.mutate({ path: { eid }, body: { language: event.target.value } })}
      >
        {WORKING_LANGUAGES.map((item) => (
          <option key={item} value={item}>
            {t.workingLanguage.names[item]}
          </option>
        ))}
      </select>
    </label>
  )
}
