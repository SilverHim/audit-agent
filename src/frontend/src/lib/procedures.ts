import type { ProcedureCatalogEntry, ProcedureSummary } from '@/client/types.gen'

// 系统程序库与项目状态分别读取: 未纳入项目的模板不伪装成 planned, 也不创建项目实例.
export type CatalogProcedure = ProcedureCatalogEntry & {
  project: ProcedureSummary | undefined
  enabled: boolean
}

export function catalogProcedures(
  catalog: ProcedureCatalogEntry[],
  project: ProcedureSummary[],
  features: string[],
): CatalogProcedure[] {
  const byId = new Map(project.map((item) => [item.id, item]))
  return catalog.map((item) => ({
    ...item,
    project: byId.get(item.id),
    enabled: !item.feature || features.includes(item.feature),
  }))
}
