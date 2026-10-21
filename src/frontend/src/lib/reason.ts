import type { Messages } from '@/locales/en'

// 人填的理由怎么显示 (需求 R360): 理由一律选填, 为空 (null, 空串或只有空白) 时显示 "未填理由"; 填了的照原文,
// quoted 时加引号. 决定, 错报, 更正通知, 数据修改的历史与留痕各处都用它, 不各写一套
export function reasonText(reason: string | null | undefined, t: Messages, quoted = false): string {
  if (!reason?.trim()) return t.reason.none
  return quoted ? `“${reason}”` : reason
}
