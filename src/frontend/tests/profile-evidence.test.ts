// 证据弹窗显示客户画像字段 (lib/profile-evidence, 需求 R302) 与证据列表的 key (lib/presentation 的 pointerKey) 的测试,
// bun test 运行. 画像是合成的 (Numbat 的六项必填加上各类扩展字段), 形状同 GET /client-profile 的返回.
import { describe, expect, test } from 'bun:test'
import type { ClientProfile, ClientProfileView, EvidencePointer } from '../src/client/types.gen'
import { pointerKey } from '../src/lib/presentation'
import { adoptedProfile, profileEvidence, profileField } from '../src/lib/profile-evidence'

const WORDS = { yes: 'Yes', no: 'No' }
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
  test('取 history 中本项目采用的那一版, 不取最新版', () => {
    expect(adoptedProfile(view(1))?.version).toBe(1)
    expect(adoptedProfile(view(2))?.version).toBe(2)
  })

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

  test('没有版本号的旧画像按第 1 版', () => {
    const { version: _, ...old } = profile(1)
    expect(adoptedProfile({ history: [old], engagement_profile_version: 1 })?.id).toBe('CLIENT-001')
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

  test('标量: 金额按 AUD, 布尔按是否, 其余原样', () => {
    expect(profileField('approval_limit_cents', 500000, WORDS)).toEqual({ kind: 'text', text: 'AUD 5,000.00' })
    expect(profileField('approval_limit_cents', 0, WORDS)).toEqual({ kind: 'text', text: 'AUD 0.00' }) // 0 不是未申报
    expect(profileField('listed', false, WORDS)).toEqual({ kind: 'text', text: 'No' })
    expect(profileField('prior_period_audited', true, WORDS)).toEqual({ kind: 'text', text: 'Yes' })
    expect(profileField('financial_year_end', '06-30', WORDS)).toEqual({ kind: 'text', text: '06-30' })
  })

  test('列表按行: 标量一行一项, 对象一行一组键值', () => {
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

  test('对象按键值; 嵌套的列表与对象写成一行, 数据为键的字典沿用字段名的单位', () => {
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
    const fees = { audit_fee_cents: 4500000, dependency_pct_by_year: { '2025': 0.12, '2026': 0.155 } }
    expect(profileField('fees', fees, WORDS)).toEqual({
      kind: 'pairs',
      pairs: [
        { key: 'audit fee', value: 'AUD 45,000.00' },
        { key: 'dependency pct by year', value: '2025: 12.00%, 2026: 15.50%' },
      ],
    })
    expect(profileField('prior_risk_register', { 'ENG-2025-001': ['revenue:high', 'cash:low'] }, WORDS)).toEqual({
      kind: 'pairs',
      pairs: [{ key: 'ENG-2025-001', value: 'revenue:high; cash:low' }],
    })
  })
})

describe('证据列表的 key (R128 第 2 轮审核意见 08)', () => {
  const base = { type: 'voucher', source_type: 'client_profile', source_id: 'CLIENT-001', page: null } as const

  test('同一画像的不同字段各算一项', () => {
    const parties = pointerKey({ ...base, locator: 'related_parties' })
    expect(parties).toBe('client_profile:CLIENT-001:related_parties:')
    expect(pointerKey({ ...base, locator: 'directors' })).not.toBe(parties)
  })

  test('文件按页与 locator 分开, 没有页时页为空', () => {
    const page = { source_type: 'document', source_id: 'DOC-1', locator: 'clause 5.1' } as const
    const keys = [
      pointerKey({ ...page, page: 1 }),
      pointerKey({ ...page, page: 2 }),
      pointerKey({ ...page, locator: 'clause 6', page: 1 }),
      pointerKey({ ...page, page: null }),
    ]
    expect(new Set(keys).size).toBe(4)
    expect(keys[0]).toBe('document:DOC-1:clause 5.1:1')
  })
})
