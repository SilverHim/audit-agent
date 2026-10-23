import type { ClientSummary } from '@/client/types.gen'
import { formatAbn } from '@/lib/abn'
import type { Messages } from '@/locales/en'

// 新建项目只提供精简演示, 同时保留没有数据集的手工客户.
export function creationClients(clients: ClientSummary[]): ClientSummary[] {
  return clients.flatMap((client) => {
    if (!client.datasets.length) return [client]
    const datasets = client.datasets.filter((dataset) => dataset.dataset === 'demo-compact')
    return datasets.length ? [{ ...client, datasets }] : []
  })
}

// 新建项目表单的客户下拉 (后端 GET /clients, 需求 R287): 有数据集带演示资料 (可在数据管理页按阶段导入) 的客户是演示
// 公司, 后端把它排在第一; 选项文字为 "名称 · ABN", 演示公司另标 "演示"
export function isDemoClient(client: ClientSummary): boolean {
  return client.datasets.some((dataset) => dataset.demo)
}

export function clientOptionLabel(client: ClientSummary, t: Messages): string {
  const label = `${client.legal_name} · ABN ${formatAbn(client.abn)}`
  return isDemoClient(client) ? `${label} · ${t.projectsPage.demoTag}` : label
}

// 选中客户下方的一行说明: 没有数据集的客户 (新建的) 说登记核查留待人工确认, 演示公司说建项目后可在数据管理页载入
// 演示资料, 其余没有; 还没选中时也没有
export function clientNote(client: ClientSummary | undefined, t: Messages): string | null {
  if (!client) return null
  if (!client.datasets.length) return t.projectsPage.noDataset
  return isDemoClient(client) ? t.projectsPage.demoNote : null
}
