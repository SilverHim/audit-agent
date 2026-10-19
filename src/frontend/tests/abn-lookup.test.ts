// ABN 在线核对结果的文字 (lib/abn-lookup.ts) 的测试, bun test 运行. 登记信息是合成的 (同后端 tests/fixtures/abr).
import { describe, expect, test } from 'bun:test'
import type { AbnLookupView, AbnRecord } from '../src/client/types.gen'
import { lookupSummary } from '../src/lib/abn-lookup'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const WOMBAT: AbnRecord = {
  abn: '44900043101',
  acn: '900043101',
  entity_name: 'WOMBAT ANALYTICS PTY LTD',
  status: 'Active',
  entity_type: 'Australian Private Company',
}
const found = (record: Partial<AbnRecord>, nameMatches: boolean | null = true): AbnLookupView => ({
  status: 'found',
  record: { ...WOMBAT, ...record },
  name_matches: nameMatches,
})

describe('lookupSummary', () => {
  test('查到: 名称, 状态, 主体类型与 ACN 一行, 一致时不警示', () => {
    expect(lookupSummary(found({}), zh)).toEqual({
      text: 'ABN Lookup：WOMBAT ANALYTICS PTY LTD · 有效 · Australian Private Company · ACN 900043101',
      warnings: [],
    })
  })

  test.each([
    ['已注销', found({ status: 'Cancelled' }), [zh.abnLookup.cancelled]],
    ['名称不一致', found({}, false), [zh.abnLookup.nameMismatch]],
    ['两样都有', found({ status: 'Cancelled' }, false), [zh.abnLookup.cancelled, zh.abnLookup.nameMismatch]],
    ['查不到', { status: 'not_found' } as AbnLookupView, [zh.abnLookup.notFound]],
  ])('%s时警示', (_name, result, warnings) => {
    expect(lookupSummary(result, zh).warnings).toEqual(warnings)
  })

  test('名称不公开时写明, 不比较; 没有 ACN 时不写', () => {
    const { text, warnings } = lookupSummary(found({ entity_name: '', acn: null }, null), en)
    expect(text).toBe('ABN Lookup: name not public · Active · Australian Private Company')
    expect(warnings).toEqual([])
  })

  test('名称还在防抖时先不提示不一致', () => {
    expect(lookupSummary(found({}, false), zh, false).warnings).toEqual([])
  })

  test('查不了时按原因说明, 不警示', () => {
    for (const reason of ['not_configured', 'unreachable', 'rejected', 'service_error'] as const) {
      expect(lookupSummary({ status: 'unavailable', reason }, zh)).toEqual({
        text: zh.abnLookup.unavailable[reason],
        warnings: [],
      })
    }
  })
})
