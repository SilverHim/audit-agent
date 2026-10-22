import { cn } from 'cn'
import type { CorrectionEntry, Judgment, ProposedAction } from '@/client/types.gen'
import { Code, SectionLabel } from '@/components/chrome'
import { useI18n } from '@/lib/i18n'
import { useMoney } from '@/lib/use-money'
import {
  actionVerdict,
  correctionCents,
  correctionKey,
  correctionSide,
  displayVerdict,
  VERDICT_TONES,
  type VerdictTone,
  verdictFigure,
} from '@/lib/verdict'

// 判断的定论与 AI 建议的更正分录 (需求 R368): 判断卡片, 复核卡片, 程序详情与列表共用.
// 定论大字号分颜色: 不是错报绿色, 是错报红色, 暂时判断不了黄色; 是错报的同一行给金额与方向.

const TEXT_TONES: Record<VerdictTone, string> = { ok: 'text-ok', risk: 'text-risk', warn: 'text-warn' }
const SOFT_TONES: Record<VerdictTone, string> = { ok: 'bg-ok-soft', risk: 'bg-risk-soft', warn: 'bg-warn-soft' }

// 判断卡的定论行: 最上面的大字, 是错报时同一行给金额与方向; verdict 为空时按 proposed_action 回退, FIN-03/FIN-09
// 的错报更新判断没有 verdict 时不显示 (lib/verdict.displayVerdict). 币种取项目记账币种 (useMoney)
export function VerdictBanner({ judgment, className }: { judgment: Judgment; className?: string }) {
  const { t } = useI18n()
  const { formatMoney } = useMoney()
  const verdict = displayVerdict(judgment)
  if (!verdict) return null
  const figure = verdictFigure(judgment)
  const tone = VERDICT_TONES[verdict]
  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-x-4 gap-y-2 rounded-[10px] px-3.5 py-2.5',
        SOFT_TONES[tone],
        className,
      )}
      data-testid="judgment-verdict"
      data-verdict={verdict}
    >
      <span className="font-semibold text-[12px] text-muted-foreground tracking-wide">{t.judgment.verdict}</span>
      <span className={cn('font-bold text-[22px] leading-tight', TEXT_TONES[tone])} data-testid="verdict-label">
        {t.judgment.verdicts[verdict]}
      </span>
      {figure ? (
        <span className="flex flex-wrap items-baseline gap-x-2 font-semibold text-[16px]" data-testid="verdict-figure">
          <Code>{formatMoney(figure.amount_cents)}</Code>
          <span className={TEXT_TONES[tone]}>{t.directions[figure.direction]}</span>
        </span>
      ) : null}
    </div>
  )
}

// 列表里的定论一格 (待决定列表, 复核队列): 颜色与判断卡一致; 是错报的同一格给金额与方向; 不是判断时为破折号
export function VerdictCell({ judgment }: { judgment: Judgment | null }) {
  const { t } = useI18n()
  const { formatMoney } = useMoney()
  if (!judgment) return <span>—</span>
  const verdict = displayVerdict(judgment)
  if (!verdict) return <span>—</span>
  const figure = verdictFigure(judgment)
  const tone = VERDICT_TONES[verdict]
  return (
    <span className="flex flex-wrap items-baseline gap-x-1.5" data-testid="verdict-cell" data-verdict={verdict}>
      <span className={cn('font-semibold text-[14.5px]', TEXT_TONES[tone])}>{t.judgment.verdicts[verdict]}</span>
      {figure ? (
        <span className="text-[13px] text-muted-foreground">
          {formatMoney(figure.amount_cents)} · {t.directions[figure.direction]}
        </span>
      ) : null}
    </span>
  )
}

// 争议两方或复核载体的建议动作, 按定论的说法显示 (不是错报 / 是错报 / 暂时判断不了); 没有建议动作时由调用方负责
export function ProposalVerdict({ action }: { action: ProposedAction | null }) {
  const { t } = useI18n()
  const verdict = actionVerdict(action)
  if (!verdict) return <span className="font-semibold text-muted-foreground">{t.card.sides.none}</span>
  return (
    <span className={cn('font-semibold', TEXT_TONES[VERDICT_TONES[verdict]])} data-testid="proposal-verdict">
      {t.judgment.verdicts[verdict]}
    </span>
  )
}

// AI 建议的更正分录 (需求 R367, R368): 每行科目代码与名称, 借方或贷方金额, 摘要. 没有分录时整块不渲染;
// 金额按项目记账币种格式化
export function CorrectionList({ entries, className }: { entries: CorrectionEntry[]; className?: string }) {
  const { t } = useI18n()
  const { formatMoney } = useMoney()
  if (!entries.length) return null
  return (
    <div className={cn('flex flex-col gap-1.5', className)} data-testid="correction-entries">
      <SectionLabel>{t.correctionEntry.title}</SectionLabel>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[14px]">
        {entries.map((entry) => {
          const side = correctionSide(entry)
          return (
            <li
              key={correctionKey(entry)}
              className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border px-3 py-2"
            >
              <span className="flex flex-wrap items-baseline gap-1.5">
                <Code className="font-semibold">{entry.account_code}</Code>
                <span>{entry.account_name}</span>
              </span>
              <span className="flex items-baseline gap-1.5 font-mono text-[13.5px]">
                <span className="text-muted-foreground">{t.correctionEntry[side]}</span>
                <span className="font-semibold">{formatMoney(correctionCents(entry))}</span>
              </span>
              {entry.description ? (
                <span className="text-[13px] text-secondary-foreground">{entry.description}</span>
              ) : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
