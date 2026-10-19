// Xero 组织信息核对卡的换算 (lib/xero-profile) 的测试, bun test 运行 (不在 tsconfig 的 src 里, 不经 tsc).
// 数据是合成的: Xero 一边照 Demo Company (AU) 快照的组织信息, 画像一边照 Xero 演示数据集设计第 2 节的合成画像.
import { describe, expect, test } from 'bun:test'
import type { TrailEntryView, XeroProfileCheck, XeroSource } from '../src/client/types.gen'
import { buildTimeline } from '../src/lib/timeline'
import { acceptanceStage, checkStatus, pendingCheck, profileChoice, profileRows } from '../src/lib/xero-profile'
import { zh } from '../src/locales/zh'

const CHECK: XeroProfileCheck = {
  key: 'XP-0123456789ab',
  xero_name: 'Demo Company (AU)',
  xero_legal_name: 'Demo Company (AU)',
  xero_abn: '11111111138',
  profile_version: 1,
  profile_legal_name: 'Demo Company (AU) Pty Ltd',
  profile_abn: '25900043021',
  differences: ['legal_name', 'abn'],
  checked_at: '2026-10-17T00:00:00Z',
}

function source(check: XeroProfileCheck | null): XeroSource {
  return { status: 'connected', profile_check: check } as XeroSource
}

describe('pendingCheck', () => {
  test('有对不上的项且没处理才提示; 锁定的项目, 一致, 已处理与没同步过都不提示', () => {
    expect(pendingCheck(source(CHECK), false)).toBe(CHECK)
    expect(pendingCheck(source(CHECK), true)).toBeNull()
    expect(pendingCheck(source({ ...CHECK, differences: [] }), false)).toBeNull()
    expect(pendingCheck(source({ ...CHECK, resolution: 'kept', reason: 'x' }), false)).toBeNull()
    expect(pendingCheck(source(null), false)).toBeNull()
    expect(pendingCheck(undefined, false)).toBeNull()
  })
})

describe('profileRows', () => {
  test('两行按 differences 标红, ABN 分组显示', () => {
    expect(profileRows({ ...CHECK, differences: ['legal_name'] }, zh)).toEqual([
      { field: 'legal_name', xero: 'Demo Company (AU)', profile: 'Demo Company (AU) Pty Ltd', differs: true },
      { field: 'abn', xero: '11 111 111 138', profile: '25 900 043 021', differs: false },
    ])
  })

  test('Xero 没给 ABN 时写明, 不标红', () => {
    const [, abn] = profileRows({ ...CHECK, xero_abn: null, differences: ['legal_name'] }, zh)
    expect(abn).toEqual({ field: 'abn', xero: zh.xeroProfile.noAbn, profile: '25 900 043 021', differs: false })
  })
})

describe('profileChoice', () => {
  test('保持原样要理由 (去首尾空白), 更新不带理由; 都绑定看到的 key', () => {
    expect(profileChoice(CHECK, 'keep', '   ', zh)).toBe(zh.errors.reasonRequired)
    expect(profileChoice(CHECK, 'keep', '  演示画像按设计覆盖 Xero 的登记号 ', zh)).toEqual({
      key: CHECK.key,
      decision: 'keep',
      reason: '演示画像按设计覆盖 Xero 的登记号',
    })
    expect(profileChoice(CHECK, 'update', 'ignored', zh)).toEqual({ key: CHECK.key, decision: 'update', reason: null })
  })
})

describe('checkStatus', () => {
  test('数据管理页 Xero 卡片的一行', () => {
    expect(checkStatus(null, zh)).toBeNull()
    expect(checkStatus({ ...CHECK, differences: [] }, zh)).toBe(zh.xeroProfile.status.consistent)
    expect(checkStatus(CHECK, zh)).toBe(zh.xeroProfile.status.pending)
    expect(checkStatus({ ...CHECK, resolution: 'kept', reason: '合成画像' }, zh)).toBe(
      zh.xeroProfile.status.kept('合成画像'),
    )
    const updated = { ...CHECK, resolution: 'updated' as const, updated_version: 2 }
    expect(checkStatus({ ...updated, adopted: true }, zh)).toBe(zh.xeroProfile.status.adopted(2))
    expect(checkStatus({ ...updated, adopted: false }, zh)).toBe(zh.xeroProfile.status.clientOnly(2, 1))
  })
})

describe('acceptanceStage', () => {
  test('停在 ACC-01 为 at; 承接阶段未到闸门为 before; 过了承接为 after', () => {
    const at = { current_gate: 'ACC-01', engagement: { phase: 'P0_Acceptance' } }
    expect(acceptanceStage(at as never)).toBe('at')
    expect(acceptanceStage({ current_gate: null, engagement: { phase: 'P0_Acceptance' } } as never)).toBe('before')
    expect(acceptanceStage({ current_gate: 'PLN-01/03', engagement: { phase: 'P1_Planning' } } as never)).toBe('after')
    expect(acceptanceStage({ current_gate: null, engagement: { phase: 'P2_Executing' } } as never)).toBe('after')
    expect(acceptanceStage(undefined)).toBe('after')
  })
})

describe('buildTimeline 的 Xero 核对', () => {
  test('选择的版本与是否采用, 稍后处理标成这一类事项 (target_id 是核对的 key)', () => {
    const entry = (id: string, kind: string, payload: Record<string, unknown>) =>
      ({ id, kind, summary: '', created_at: `2026-10-19T0${id}:00:00Z`, payload }) as unknown as TrailEntryView
    const items = buildTimeline([
      entry('1', 'deferral', { target_type: 'xero_profile', target_id: CHECK.key, deferred: true }),
      entry('2', 'data', { action: 'xero_profile_kept', key: CHECK.key, profile_version: 1, reason: '合成画像' }),
      entry('3', 'data', {
        action: 'xero_profile_updated',
        key: 'XP-x',
        profile_version: 1,
        version: 2,
        adopted: true,
      }),
    ])
    expect(items.filter((item) => item.kind === 'deferral')).toMatchObject([{ xero: true, request: null }])
    expect(items.filter((item) => item.kind === 'data')).toMatchObject([
      { action: 'xero_profile_kept', profileVersion: 1, reason: '合成画像', version: null, adopted: null },
      { action: 'xero_profile_updated', profileVersion: 1, version: 2, adopted: true },
    ])
  })
})
