import type { EngagementOverview } from '@/client/types.gen'

// 工作台的当前事项 (后端 R152): 相关查询都已返回 (或出错) 之后才确定. 之前不显示任何事项卡, 免得各查询陆续返回时
// 先闪过一条复核任务 (补交资料, Xero 核对等还没读到) 又换掉: 那一下既晃眼, 也会给审计师并没打开的任务记一次呈递

// 一个查询的状态: 还没有数据也还没出错为 pending (TanStack Query 的 isPending; 后台刷新时不算)
export type Loading = { isPending: boolean }

export type WorkspaceQueries = {
  gate: Loading // 当前闸门的材料, 只在总览说停在闸门时才读
  tasks: Loading // 待决复核任务
  exceptions: Loading // 例外 (复核任务的金额与档位要用)
  deferrals: Loading // 稍后处理的标记
  requests: Loading // 补交资料请求
  summary: Loading // 数据概览 (Xero 组织信息核对)
}

// 当前事项能否确定: 总览已到, 其余查询都不在 pending; 停在闸门时闸门材料也已返回. 纯函数
export function workspaceReady(overview: EngagementOverview | undefined, queries: WorkspaceQueries): boolean {
  if (!overview) return false
  if (overview.current_gate && queries.gate.isPending) return false
  const { tasks, exceptions, deferrals, requests, summary } = queries
  return [tasks, exceptions, deferrals, requests, summary].every((query) => !query.isPending)
}

export type Candidate = { kind: string; key: string; deferred: boolean }

// 当前事项: 地址里 focus 指名的那一项 (补交资料各请求合成一项, focus 为任一请求都指它), 否则第一项没标稍后处理的;
// 都标了或没有事项为 null. items 已按 阶段决定 → 补交资料 → Xero 核对 → 复核任务 排好. 纯函数
export function pickCurrent<T extends Candidate>(items: T[], focus: string | undefined): T | null {
  return (
    items.find((item) => item.key === focus || (item.kind === 'requests' && focus?.startsWith('request:'))) ??
    items.find((item) => !item.deferred) ??
    null
  )
}
