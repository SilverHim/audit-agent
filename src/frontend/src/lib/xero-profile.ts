import type { EngagementOverview, XeroProfileCheck, XeroProfileCheckIn, XeroSource } from '@/client/types.gen'
import type { Messages } from '@/locales/en'

// Xero 组织信息与客户画像的核对 (后端 R291). 待处理: 有对不上的项且还没作出选择; 锁定的项目不提示
export function pendingCheck(xero: XeroSource | null | undefined, locked: boolean): XeroProfileCheck | null {
  const check = xero?.profile_check
  if (!check || locked || !(check.differences ?? []).length || check.resolution) return null
  return check
}

// ABN 按 2-3-3-3 分组显示; 不是 11 位时原样
export function formatAbn(abn: string | null | undefined): string {
  const digits = (abn ?? '').replace(/\D/g, '')
  if (digits.length !== 11) return abn ?? ''
  return `${digits.slice(0, 2)} ${digits.slice(2, 5)} ${digits.slice(5, 8)} ${digits.slice(8)}`
}

export type ProfileRow = { field: 'legal_name' | 'abn'; xero: string; profile: string; differs: boolean }

// 核对卡的两行: 法定名称与 ABN, 两边的值与是否对不上 (Xero 没给 ABN 时写明, 不算对不上)
export function profileRows(check: XeroProfileCheck, t: Messages): ProfileRow[] {
  const differs = new Set(check.differences ?? [])
  return [
    {
      field: 'legal_name',
      xero: check.xero_legal_name,
      profile: check.profile_legal_name,
      differs: differs.has('legal_name'),
    },
    {
      field: 'abn',
      xero: check.xero_abn ? formatAbn(check.xero_abn) : t.xeroProfile.noAbn,
      profile: formatAbn(check.profile_abn),
      differs: differs.has('abn'),
    },
  ]
}

// 选 "按 Xero 更新" 时本项目跟不跟着换 (后端 R291): 只有停在承接决定 (ACC-01) 时 (at) 本项目采用新版本; 承接阶段
// 还没到闸门 (before) 与已过承接 (after) 都只给客户写新版本, 说明文字不同 (未到闸门的可先稍后处理, 到闸门再更新)
export type AcceptanceStage = 'before' | 'at' | 'after'

export function acceptanceStage(
  overview: Pick<EngagementOverview, 'current_gate' | 'engagement'> | undefined,
): AcceptanceStage {
  if (overview?.current_gate === 'ACC-01') return 'at'
  return overview?.engagement.phase === 'P0_Acceptance' ? 'before' : 'after'
}

// 请求体; 保持原样没写理由时返回错误文案
export function profileChoice(
  check: XeroProfileCheck,
  decision: XeroProfileCheckIn['decision'],
  reason: string,
  t: Messages,
): XeroProfileCheckIn | string {
  const text = reason.trim()
  if (decision === 'keep' && !text) return t.errors.reasonRequired
  return { key: check.key, decision, reason: decision === 'keep' ? text : null }
}

// 数据管理页 Xero 卡片里的核对结果一行; 没有核对 (没同步过) 时为空
export function checkStatus(check: XeroProfileCheck | null | undefined, t: Messages): string | null {
  if (!check) return null
  if (check.resolution === 'kept') return t.xeroProfile.status.kept(check.reason ?? '')
  if (check.resolution === 'updated') {
    const version = check.updated_version ?? check.profile_version
    return check.adopted
      ? t.xeroProfile.status.adopted(version)
      : t.xeroProfile.status.clientOnly(version, check.profile_version)
  }
  return (check.differences ?? []).length ? t.xeroProfile.status.pending : t.xeroProfile.status.consistent
}
