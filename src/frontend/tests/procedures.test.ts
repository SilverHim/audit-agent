import { expect, test } from 'bun:test'
import type { ProcedureCatalogEntry, ProcedureSummary } from '../src/client/types.gen'
import { catalogProcedures } from '../src/lib/procedures'

const catalog = [
  { id: 'ACC-04', feature: null },
  { id: 'REV-02', feature: null },
  { id: 'GEN-08', feature: 'related_parties' },
] as ProcedureCatalogEntry[]

test('程序库显示未纳入和未启用的程序, 项目状态只属于已有实例', () => {
  const project = [{ id: 'ACC-04', status: 'completed' }] as ProcedureSummary[]
  const rows = catalogProcedures(catalog, project, ['critic'])
  expect(rows.map((p) => [p.id, p.enabled, p.project?.status])).toEqual([
    ['ACC-04', true, 'completed'],
    ['REV-02', true, undefined],
    ['GEN-08', false, undefined],
  ])
  expect(catalogProcedures(catalog, [], ['related_parties']).map((p) => p.enabled)).toEqual([true, true, true])
  expect(project).toHaveLength(1)
})
