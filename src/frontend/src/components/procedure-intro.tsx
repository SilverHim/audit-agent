import type { ReactNode } from 'react'
import type { ProcedureRun } from '@/client/types.gen'
import { useI18n } from '@/lib/i18n'
import { useMoney } from '@/lib/use-money'
import { procedureIntro } from '@/locales/procedure-intros'

// 程序介绍 (程序页侧栏与程序详情页): 审计目标, 适用条件, 所需资料, 执行方法, 产出与复核要求.
// 本项目取值按最近一次执行的参数快照; 还没执行时不显示
export function ProcedureIntroView({ procedureId, runs }: { procedureId: string; runs?: ProcedureRun[] }) {
  const { currency } = useMoney()
  const { t, lang } = useI18n()
  const words = t.proceduresPage.intro
  const intro = procedureIntro(procedureId, lang)
  if (!intro) return <p className="m-0 text-[13px] text-muted-foreground">{words.missing}</p>
  const latest = runs?.reduce<ProcedureRun | undefined>(
    (last, run) => (last && last.run_no >= run.run_no ? last : run),
    undefined,
  )
  const project = latest && intro.project ? intro.project(latest.params_snapshot, currency) : null
  return (
    <div className="flex flex-col gap-3.5 text-[13.5px] leading-relaxed">
      <Part title={words.purpose}>
        <p className="m-0">{intro.purpose}</p>
      </Part>
      <Part title={words.when}>
        <p className="m-0">{intro.when}</p>
        {project ? (
          <p className="m-0 rounded-[10px] bg-accent px-3 py-2">
            <span className="font-semibold">{words.thisEngagement}</span>
            {project}
          </p>
        ) : null}
      </Part>
      {intro.inputs ? (
        <Part title={words.inputs}>
          <p className="m-0">{intro.inputs}</p>
        </Part>
      ) : null}
      <Part title={words.logic}>
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {intro.logic.map((item) => (
            <li key={item.name} className="flex flex-col gap-0.5 border-[#dce0e6] border-l-2 pl-2.5">
              <span className="font-semibold">{item.name}</span>
              <span>{item.detail}</span>
            </li>
          ))}
        </ul>
      </Part>
      <Part title={words.result}>
        <p className="m-0">{intro.result}</p>
      </Part>
    </div>
  )
}

function Part({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="font-semibold text-[12px] text-muted-foreground">{title}</span>
      {children}
    </div>
  )
}
