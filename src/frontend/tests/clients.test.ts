// 新建项目表单的客户下拉 (lib/clients.ts, 需求 R287) 的测试, bun test 运行. 客户取自仓库里的数据集画像 (合成).
import { describe, expect, test } from 'bun:test'
import type { ClientSummary } from '../src/client/types.gen'
import { clientNote, clientOptionLabel, isDemoClient } from '../src/lib/clients'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const DEMO: ClientSummary = {
  client_id: 'CLIENT-XERO-3C1332BB-Q1',
  legal_name: 'Demo Company (AU) Pty Ltd',
  abn: '25900043021',
  origin: 'dataset',
  latest_version: null,
  datasets: [
    { dataset: 'xero/20261017-q1', period_start: '2026-07-01', period_end: '2026-09-30', default: true, demo: true },
  ],
}
// 默认数据集不带演示资料时 (如把 DEMO_DATASET 设成没有补造目录的快照) 也不标演示: 标记只看 demo
const MOCK: ClientSummary = {
  ...DEMO,
  client_id: 'CLIENT-XERO-MOCK',
  legal_name: 'Demo Company (AU)',
  abn: '11111111138',
  datasets: [{ dataset: 'xero/20261017-mock', default: true, demo: false }],
}
const NEW: ClientSummary = { ...DEMO, client_id: 'CLIENT-M-0a1b2c3d4e5f', origin: 'manual', datasets: [] }

describe('clientOptionLabel', () => {
  test('演示公司的选项标 "演示"', () => {
    expect(clientOptionLabel(DEMO, zh)).toBe('Demo Company (AU) Pty Ltd · ABN 25 900 043 021 · 演示')
    expect(clientOptionLabel(DEMO, en)).toBe('Demo Company (AU) Pty Ltd · ABN 25 900 043 021 · Demo')
  })

  test('没有演示资料的数据集客户与新建的客户不标', () => {
    expect(clientOptionLabel(MOCK, zh)).toBe('Demo Company (AU) · ABN 11 111 111 138')
    expect(clientOptionLabel(NEW, en)).toBe('Demo Company (AU) Pty Ltd · ABN 25 900 043 021')
    expect([DEMO, MOCK, NEW].map(isDemoClient)).toEqual([true, false, false])
  })
})

describe('clientNote', () => {
  test('演示公司说可载入演示资料, 新建的客户说登记核查转人工, 其余与没选中时不说', () => {
    expect(clientNote(DEMO, zh)).toBe(zh.projectsPage.demoNote)
    expect(clientNote(NEW, en)).toBe(en.projectsPage.noDataset)
    expect(clientNote(MOCK, zh)).toBeNull()
    expect(clientNote(undefined, zh)).toBeNull()
  })
})
