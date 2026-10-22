import type {
  CorrectionEntry,
  Direction,
  MisstatementDraft,
  MisstatementUpdate,
  ProposedAction,
  Verdict,
} from '@/client/types.gen'

// 定论的类型取自生成的前端客户端 (Judgment.verdict)
export type { Verdict }

// 判断的定论 (后端需求 R365, 界面需求 R368): 不是错报 / 是错报 / 暂时判断不了, 与建议动作一一对应. 定论颜色与更正
// 分录的显示也在这里算, 判断卡片, 复核卡片, 程序详情与列表共用, 保证口径一致.

// 定论的颜色类别: 不是错报绿色 (ok), 是错报红色 (risk), 暂时判断不了黄色 (warn)
export type VerdictTone = 'ok' | 'risk' | 'warn'

export const VERDICT_TONES: Record<Verdict, VerdictTone> = {
  not_misstatement: 'ok',
  misstatement: 'risk',
  undetermined: 'warn',
}

// 建议动作 → 定论: 契约里两者一一对应 (accept, close 为不是错报; misstatement 为是错报; more_evidence 为暂时判断不了)
const ACTION_VERDICTS: Record<ProposedAction, Verdict> = {
  accept: 'not_misstatement',
  close: 'not_misstatement',
  misstatement: 'misstatement',
  more_evidence: 'undetermined',
}

// 定论回退用的最小字段: 判断与复核载荷都有, 页面另传 misstatement_update 判断要不要显示
export type VerdictSource = {
  verdict?: Verdict | null
  proposed_action: ProposedAction
  misstatement_update?: MisstatementUpdate | null
}

// 定论: verdict 为空时按 proposed_action 回退 (人工判断, FIN-03/FIN-09 的错报更新判断与旧数据, 需求 R368)
export function effectiveVerdict(source: Pick<VerdictSource, 'verdict' | 'proposed_action'>): Verdict {
  return source.verdict ?? ACTION_VERDICTS[source.proposed_action]
}

// 定论行显示哪个定论 (需求 R368): FIN-03/FIN-09 的错报更新判断与旧数据没有 verdict, 它判的不是 "是不是错报", 不显示;
// 其余 (含人工判断与旧判断) 按 proposed_action 回退显示. 返回 null 时不显示定论行
export function displayVerdict(source: VerdictSource): Verdict | null {
  if (!source.verdict && source.misstatement_update) return null
  return effectiveVerdict(source)
}

// 建议动作对应的定论 (争议两方只有 proposed_action): 没有动作时为 null
export function actionVerdict(action: ProposedAction | null | undefined): Verdict | null {
  return action ? ACTION_VERDICTS[action] : null
}

// 是错报时的金额与方向 (需求 R368: 同一行显示, 高估或低估): 取 AI 建议的错报草稿, 没有草稿时为 null
export type VerdictFigure = { amount_cents: number; direction: Direction }

export function verdictFigure(
  source: Pick<VerdictSource, 'verdict' | 'proposed_action'> & {
    misstatement_draft?: MisstatementDraft | null
  },
): VerdictFigure | null {
  const draft = source.misstatement_draft
  if (effectiveVerdict(source) !== 'misstatement' || !draft) return null
  return { amount_cents: draft.amount_cents, direction: draft.direction }
}

// 更正分录 (需求 R367, R368): 判断的草稿或错报没带 (旧数据, 人工判断没填) 时为空
export function correctionEntries(
  source: { correction?: CorrectionEntry[] | null } | null | undefined,
): CorrectionEntry[] {
  return source?.correction ?? []
}

// 更正分录的方向: 一行只记借方或贷方一方 (后端模型校验), 借方非零为借方, 否则为贷方
export type CorrectionSide = 'debit' | 'credit'

export function correctionSide(entry: CorrectionEntry): CorrectionSide {
  return (entry.debit_cents ?? 0) !== 0 ? 'debit' : 'credit'
}

// 该行记的金额 (借方或贷方)
export function correctionCents(entry: CorrectionEntry): number {
  return correctionSide(entry) === 'debit' ? (entry.debit_cents ?? 0) : (entry.credit_cents ?? 0)
}

// 一行更正分录的渲染 key: 按科目, 两方金额与摘要定, 同一张分录里不重复
export function correctionKey(entry: CorrectionEntry): string {
  return `${entry.account_code}:${entry.debit_cents ?? 0}:${entry.credit_cents ?? 0}:${entry.description}`
}
