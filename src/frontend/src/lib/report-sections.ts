import type { ReportSection } from '@/client/types.gen'

// 审计报告各段的段名只显示一次 (需求 R352). 意见段与形成意见的基础段的正文首行是段名 (RPT-03 校验首行为该意见类型
// 的标题, 按报告的语言取 report.yaml 的 headings, 需求 R341); 报告模板 (knowledge/templates/report.yaml) 的各段按接口文档第 8 节的约定首行为标题,
// 签名块除外. 这几段取正文首行作段名, 不另显示界面的段名; 签名块正文不带段名, 用界面的段名. 正文为空的段 (如现在
// 模板里留空的其他法律法规要求) 也用界面的段名

const HEADED: ReadonlySet<ReportSection> = new Set([
  'opinion',
  'basis_for_opinion',
  'directors_responsibilities',
  'auditor_responsibilities',
  'other_legal_and_regulatory',
])

export type ReportPart = { heading: string; body: string }

// 一段的段名与正文: 自带段名的段取首行, 正文为其后各行 (去掉开头的空行); 其余取界面的段名 label. 正文为空时为 "—"
export function reportPart(section: ReportSection, text: string, label: string): ReportPart {
  if (HEADED.has(section) && text.trim()) {
    const [heading = '', ...rest] = text.trim().split('\n')
    return { heading, body: rest.join('\n').replace(/^\s*\n/, '') || '—' }
  }
  return { heading: label, body: text || '—' }
}
