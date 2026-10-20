// 证据弹窗显示客户画像字段 (lib/profile-evidence, 需求 R302) 与证据列表的 key (lib/presentation 的 pointerKey) 的测试,
// bun test 运行. 画像是合成的 (Numbat 的六项必填加上各类扩展字段), 形状同 GET /client-profile 的返回.
import { describe, expect, test } from 'bun:test'
import type { ClientProfile, ClientProfileView, EvidencePointer } from '../src/client/types.gen'
import { formatAbn } from '../src/lib/abn'
import { pointerKey } from '../src/lib/presentation'
import { type ProfileWords, profileEvidence, profileField } from '../src/lib/profile-evidence'

// 同组件给的: 是否, 以及按字段名的显示 (主体类型的标签, ABN 分组)
const WORDS: ProfileWords = {
  yes: 'Yes',
  no: 'No',
  display: {
    entity_type: (value) => ({ small_proprietary: 'Small proprietary company' })[value] ?? value,
    abn: formatAbn,
  },
}
const PARTY = {
  name: 'Wombat Holdings Pty Ltd',
  kind: 'company',
  relationship: 'parent entity',
  abn: null,
  acn: '900043030',
  notes: '',
} as const

function profile(version: number, changes: Partial<ClientProfile> = {}): ClientProfile {
  return {
    id: 'CLIENT-001',
    version,
    legal_name: 'Numbat Electronic Components Pty Ltd',
    abn: '68900042015',
    entity_type: 'small_proprietary',
    industry_anzsic: '3494',
    listed: false,
    directors: ['Jordan Avery', 'Priya Raman'],
    notes: '',
    financial_year_end: null,
    accounting_system: null,
    engagements: [],
    approval_limit_cents: null,
    key_personnel: [],
    related_parties: [],
    ownership: [],
    prior_risk_register: {},
    prior_misstatements: [],
    independence_flags: [],
    applicable_framework: null,
    governance: null,
    fees: null,
    service_organisations: [],
    prior_estimate_outcomes: [],
    retention: null,
    prior_period_audited: null,
    ...changes,
  }
}

// 第 1 版没有申报关联方, 第 2 版 (同客户的其他项目更新) 申报了一个; 最新版是第 2 版
const HISTORY = [profile(1), profile(2, { related_parties: [PARTY] })]

function view(adopted: number): Pick<ClientProfileView, 'history' | 'engagement_profile_version'> {
  return { history: HISTORY, engagement_profile_version: adopted }
}

function pointer(locator: string, sourceId = 'CLIENT-001'): Pick<EvidencePointer, 'source_id' | 'locator'> {
  return { source_id: sourceId, locator }
}

describe('采用的版本', () => {
  test('画像改出新版本后, 采用旧版本的项目照旧显示旧版本的内容 (验收 2)', () => {
    expect(profileEvidence(view(1), pointer('related_parties'), WORDS)).toEqual({
      kind: 'shown',
      version: 1,
      field: { kind: 'undeclared' },
    })
    expect(profileEvidence(view(2), pointer('related_parties'), WORDS)).toEqual({
      kind: 'shown',
      version: 2,
      field: {
        kind: 'rows',
        rows: [
          {
            pairs: [
              { key: 'name', value: 'Wombat Holdings Pty Ltd' },
              { key: 'kind', value: 'company' },
              { key: 'relationship', value: 'parent entity' },
              { key: 'abn', value: '—' },
              { key: 'acn', value: '900043030' },
              { key: 'notes', value: '—' },
            ],
          },
        ],
      },
    })
  })

  test('显示不了的三种情形各给原因', () => {
    expect(profileEvidence(view(3), pointer('directors'), WORDS)).toEqual({ kind: 'not_adopted' })
    expect(profileEvidence(view(1), pointer('directors', 'CLIENT-XERO-3C1332BB-Q1'), WORDS)).toEqual({
      kind: 'other_profile',
    })
    expect(profileEvidence(view(1), pointer('related_party_list'), WORDS)).toEqual({ kind: 'unknown_field' })
    expect(profileEvidence(view(1), pointer('toString'), WORDS)).toEqual({ kind: 'unknown_field' }) // 原型上的不算
  })
})

describe('字段的内容 (验收 1)', () => {
  test('字段为空即未申报: null, 空串, 空列表, 空对象', () => {
    for (const value of [null, undefined, '', '  ', [], {}]) {
      expect(profileField('notes', value, WORDS)).toEqual({ kind: 'undeclared' })
    }
  })

  test('标量: 金额按 AUD, 布尔按是否, 有标签或格式的按组件给的显示, 其余原样', () => {
    expect(profileField('approval_limit_cents', 500000, WORDS)).toEqual({ kind: 'text', text: 'AUD 5,000.00' })
    expect(profileField('approval_limit_cents', 0, WORDS)).toEqual({ kind: 'text', text: 'AUD 0.00' }) // 0 不是未申报
    expect(profileField('listed', false, WORDS)).toEqual({ kind: 'text', text: 'No' })
    expect(profileField('prior_period_audited', true, WORDS)).toEqual({ kind: 'text', text: 'Yes' })
    expect(profileField('financial_year_end', '06-30', WORDS)).toEqual({ kind: 'text', text: '06-30' })
    expect(profileField('entity_type', 'small_proprietary', WORDS)).toEqual({
      kind: 'text',
      text: 'Small proprietary company',
    })
    expect(profileField('abn', '68900042015', WORDS)).toEqual({ kind: 'text', text: '68 900 042 015' })
  })

  test('列表按行: 标量一行一项, 对象一行一组键值 (嵌套的同名字段同样按显示)', () => {
    expect(profileField('directors', ['Jordan Avery', 'Priya Raman'], WORDS)).toEqual({
      kind: 'rows',
      rows: [{ text: 'Jordan Avery' }, { text: 'Priya Raman' }],
    })
    const holding = { holder: 'Jordan Avery', kind: 'person', shares: 850, pct: 0.85 }
    expect(profileField('ownership', [holding], WORDS)).toEqual({
      kind: 'rows',
      rows: [
        {
          pairs: [
            { key: 'holder', value: 'Jordan Avery' },
            { key: 'kind', value: 'person' },
            { key: 'shares', value: '850' },
            { key: 'pct', value: '85.00%' },
          ],
        },
      ],
    })
    expect(profileField('related_parties', [{ ...PARTY, abn: '68900042015' }], WORDS)).toMatchObject({
      rows: [{ pairs: expect.arrayContaining([{ key: 'abn', value: '68 900 042 015' }]) }],
    })
    const misstatement = { engagement: 'ENG-2025-001', area: 'cutoff', amount_cents: 1420000, corrected: true }
    expect(profileField('prior_misstatements', [misstatement], WORDS)).toEqual({
      kind: 'rows',
      rows: [
        {
          pairs: [
            { key: 'engagement', value: 'ENG-2025-001' },
            { key: 'area', value: 'cutoff' },
            { key: 'amount', value: 'AUD 14,200.00' },
            { key: 'corrected', value: 'Yes' },
          ],
        },
      ],
    })
  })

  test('对象按键值; 嵌套的列表与对象写成一行 (空的为 "—"), 数据为键的字典沿用字段名的单位', () => {
    const governance = {
      body: 'board',
      members: [
        { name: 'Jordan Avery', also_management: true },
        { name: 'Priya Raman', also_management: false },
      ],
      all_in_management: false,
    }
    expect(profileField('governance', governance, WORDS)).toEqual({
      kind: 'pairs',
      pairs: [
        { key: 'body', value: 'board' },
        { key: 'members', value: 'name: Jordan Avery, also management: Yes; name: Priya Raman, also management: No' },
        { key: 'all in management', value: 'No' },
      ],
    })
    expect(profileField('governance', { ...governance, members: [] }, WORDS)).toMatchObject({
      pairs: expect.arrayContaining([{ key: 'members', value: '—' }]),
    })
    const fees = {
      audit_fee_cents: 4500000,
      dependency_pct_by_year: { '2025': 0.12, '2026': 0.155 },
      referral_source_fees_pct_by_year: {}, // Fees 的缺省
    }
    expect(profileField('fees', fees, WORDS)).toEqual({
      kind: 'pairs',
      pairs: [
        { key: 'audit fee', value: 'AUD 45,000.00' },
        { key: 'dependency pct by year', value: '2025: 12.00%, 2026: 15.50%' },
        { key: 'referral source fees pct by year', value: '—' },
      ],
    })
    expect(profileField('prior_risk_register', { 'ENG-2025-001': ['revenue:high', 'cash:low'] }, WORDS)).toEqual({
      kind: 'pairs',
      pairs: [{ key: 'ENG-2025-001', value: 'revenue:high; cash:low' }],
    })
  })
})

test('证据列表的 key: 来源, ID, locator 与页都进 key, 只差一项的指针各算一项 (R128 第 2 轮审核意见 08)', () => {
  const parties: Pick<EvidencePointer, 'source_type' | 'source_id' | 'locator' | 'page'> = {
    source_type: 'client_profile',
    source_id: 'CLIENT-001',
    locator: 'related_parties',
    page: null,
  }
  const keys = [
    pointerKey(parties),
    pointerKey({ ...parties, locator: 'directors' }), // 同一画像的另一字段
    pointerKey({ ...parties, source_id: 'CLIENT-002' }),
    pointerKey({ ...parties, source_type: 'document' }),
    pointerKey({ ...parties, page: 1 }),
  ]
  expect(new Set(keys).size).toBe(keys.length)
  expect(keys[0]).toBe('client_profile:CLIENT-001:related_parties:')
})
