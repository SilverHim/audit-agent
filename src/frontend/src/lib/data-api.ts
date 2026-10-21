import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import {
  dataAccountHistoryOptions,
  dataDataRequestsOptions,
  dataGetBatchOptions,
  dataGetBatchQueryKey,
  dataListBatchesOptions,
  dataListBatchesQueryKey,
  dataSummaryOptions,
  dataSummaryQueryKey,
  dataTablePageOptions,
  dataTableSchemasOptions,
  dataXeroTenantsOptions,
  dataXeroTenantsQueryKey,
  evidenceDocumentHistoryOptions,
} from '@/client/@tanstack/react-query.gen'
import {
  dataChangeImportSheet,
  dataCommitImport,
  dataConfirmBatch,
  dataDemoImport,
  dataEditBatchItem,
  dataMarkRequestUnavailable,
  dataReady,
  dataRetryBatch,
  dataSetAccountCategory,
  dataStartBatch,
  dataStartImport,
  dataXeroConnect,
  dataXeroDisconnect,
  dataXeroProfileCheck,
  dataXeroSync,
  dataXeroTenant,
  evidenceEditConfirmation,
  evidenceEditContract,
  evidenceEditRegistryExtract,
  evidenceSetDocumentDetails,
  evidenceSetDocumentType,
} from '@/client/sdk.gen'
import type {
  AccountCategoryIn,
  BatchItemEdit,
  CategoryVersion,
  DataRequest,
  DataSummary,
  DocumentDetailsIn,
  DocumentType,
  ExtractionEdit,
  ImportCommit,
  TableKind,
  XeroProfileCheckIn,
} from '@/client/types.gen'
import { invalidateEngagement } from '@/lib/engagement'
import type { Messages } from '@/locales/en'

// 数据管理接口 (后端需求 R10-R16, Xero 网页连接 R279) 用生成的客户端; 查询键都带 path.eid, 写操作后按项目整体失效.

export type {
  BatchIssue,
  BatchItem,
  CategoryVersion,
  DataCheck,
  DataRequest,
  DataSummary,
  ExtractionEdit,
  FieldSource,
  ImportBatch,
  ImportCommit,
  ImportPreview,
  TableColumn,
  TableField,
  TableKind,
  TablePlan,
  TableSchema,
  TableSummary,
} from '@/client/types.gen'

export type DateFormat = ImportCommit['date_format']
export type CategoryBasis = CategoryVersion['basis']
export type AccountCategory = AccountCategoryIn['category']

export const TABLE_KINDS: TableKind[] = [
  'accounts',
  'trial_balance',
  'journal_entries',
  'sales_invoices',
  'supplier_bills',
  'bank_accounts',
  'bank_transactions',
  'delivery_notes',
  'approval_log',
  'employees',
]

// R11 概览; 有进行中的处理时每 3 秒轮询, 处理完时刷新本项目其他数据 (文件状态, 补交请求, 总览)
export function useDataSummary(eid: string) {
  const queryClient = useQueryClient()
  const query = useQuery({
    ...dataSummaryOptions({ path: { eid } }),
    refetchInterval: (current) => (current.state.data?.processing ? 3000 : false),
  })
  const processing = query.data?.processing ?? 0
  const previous = useRef(processing)
  useEffect(() => {
    if (previous.current && !processing) void invalidateEngagement(queryClient, eid)
    previous.current = processing
  }, [processing, queryClient, eid])
  return query
}

// Xero 网页连接 (后端 R279). 连接: 取授权地址后整页跳到 Xero, 授权后由后端回调 302 回数据管理页 (?xero=...)
export function useXeroConnect(eid: string) {
  return useMutation({
    mutationFn: async () => (await dataXeroConnect({ path: { eid }, throwOnError: true })).data,
    onSuccess: ({ authorize_url }) => window.location.assign(authorize_url),
  })
}

// 授权给到的组织 (连接时存下的, 后端不调 Xero); 只在已连接时取. 没有连接时后端报 409, 不重试
export function useXeroTenants(eid: string, enabled: boolean) {
  return useQuery({ ...dataXeroTenantsOptions({ path: { eid } }), enabled, retry: false })
}

export function useXeroChooseTenant(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (tenantId: string) =>
      (await dataXeroTenant({ path: { eid }, body: { tenant_id: tenantId }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 同步在后台进行: 202 返回要写的表, 进度看概览 (这些表为 converting, processing 计数)
export function useXeroSync(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => (await dataXeroSync({ path: { eid }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 导入演示资料的一个阶段 (后端 R269, R270): 202 返回 started 的结果, 进度看概览 (processing), 做完的结果看概览的 demo.last
export function useDemoImport(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (stage: string) =>
      (await dataDemoImport({ path: { eid }, body: { stage }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// Xero 组织信息与客户画像核对的选择 (后端 R291): 保持原样 (理由选填) 或按 Xero 更新画像; 返回处理后的核对
export function useXeroProfileCheck(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: XeroProfileCheckIn) =>
      (await dataXeroProfileCheck({ path: { eid }, body, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 断开: revoked 为 false 时 Xero 侧没撤销成功 (本地令牌已删), 要提示到 Xero 后台手动移除.
// 成功后先把概览里的连接置为未连接 (组织列表随之停用) 并丢掉组织列表, 再整体失效, 不再去取已删连接的组织
export function useXeroDisconnect(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => (await dataXeroDisconnect({ path: { eid }, throwOnError: true })).data,
    onSuccess: () => {
      queryClient.setQueryData<DataSummary>(dataSummaryQueryKey({ path: { eid } }), (summary) =>
        summary ? { ...summary, sources: { ...summary.sources, xero: { status: 'not_connected' } } } : summary,
      )
      queryClient.removeQueries({ queryKey: dataXeroTenantsQueryKey({ path: { eid } }) })
    },
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R13 账表导入. 字段定义不随项目变化, 取到后不再刷新
export function useTableSchemas(enabled: boolean) {
  return useQuery({ ...dataTableSchemasOptions(), enabled, staleTime: Number.POSITIVE_INFINITY })
}

export async function startImport(eid: string, file: File) {
  const { data } = await dataStartImport({
    path: { eid },
    body: { files: [file] },
    throwOnError: true,
  })
  return data
}

export async function changeImportSheet(eid: string, importId: string, sheet: string) {
  const { data } = await dataChangeImportSheet({
    path: { eid, import_id: importId },
    body: { sheet },
    throwOnError: true,
  })
  return data
}

export function useCommitImport(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ importId, body }: { importId: string; body: ImportCommit }) =>
      (await dataCommitImport({ path: { eid, import_id: importId }, body, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R277 智能导入: 上传一批 (表格与其他文件混着), 查确认单 (识别中每 2 秒轮询), 调整单项, 确认导入.
// 数据管理页的 "上传资料" 与补交卡片上的上传都走它; 确认前不写财务表, 文件暂存
export const UPLOAD_ACCEPT = '.csv,.xlsx,.xlsm,.xls,.pdf,.doc,.docx,.png,.jpg,.jpeg'

export function useStartBatch(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ files, requestId }: { files: File[]; requestId?: string }) =>
      (await dataStartBatch({ path: { eid }, body: { files, request_id: requestId }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 还没写入的批次 (关掉确认单后从这里回去); 有在识别的时每 3 秒刷新
export function useOpenBatches(eid: string) {
  return useQuery({
    ...dataListBatchesOptions({ path: { eid } }),
    refetchInterval: (current) => (current.state.data?.some((batch) => batch.status === 'processing') ? 3000 : false),
  })
}

export function useBatch(eid: string, batchId: string | null) {
  return useQuery({
    ...dataGetBatchOptions({ path: { eid, batch_id: batchId ?? '' } }),
    enabled: Boolean(batchId),
    refetchInterval: (current) => (current.state.data?.status === 'processing' ? 2000 : false),
  })
}

// 调整单项返回整张确认单 (重复的表等问题跨项重算), 直接替换缓存; 改了文件类型的批次回到 processing, 照常轮询.
// "待确认的上传" 列表随之失效 (状态可能变成识别中, 列表据此开始轮询)
export function useEditBatchItem(eid: string, batchId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ itemId, body }: { itemId: string; body: BatchItemEdit }) =>
      (await dataEditBatchItem({ path: { eid, batch_id: batchId, item_id: itemId }, body, throwOnError: true })).data,
    onSuccess: (batch) => queryClient.setQueryData(dataGetBatchQueryKey({ path: { eid, batch_id: batchId } }), batch),
    onSettled: () => queryClient.invalidateQueries({ queryKey: dataListBatchesQueryKey({ path: { eid } }) }),
  })
}

// 识别作业出错 (failed) 时重新识别, 从没做完的文件接着做; 返回置回 processing 的批次, 之后照常轮询
export function useRetryBatch(eid: string, batchId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => (await dataRetryBatch({ path: { eid, batch_id: batchId }, throwOnError: true })).data,
    onSuccess: (batch) => queryClient.setQueryData(dataGetBatchQueryKey({ path: { eid, batch_id: batchId } }), batch),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

export function useConfirmBatch(eid: string, batchId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => (await dataConfirmBatch({ path: { eid, batch_id: batchId }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R14 文件
export function useSetDocumentType(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ documentId, type, reason }: { documentId: string; type: DocumentType; reason: string }) =>
      (
        await evidenceSetDocumentType({
          path: { eid, document_id: documentId },
          body: { type, reason },
          throwOnError: true,
        })
      ).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 文件的补登记 (后端 R90): 只带改了的项; 历史取后端的文件登记各版 (GET /documents/{id}/history)
export function useDocumentDetails(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ documentId, body }: { documentId: string; body: DocumentDetailsIn }) =>
      (
        await evidenceSetDocumentDetails({
          path: { eid, document_id: documentId },
          body,
          throwOnError: true,
        })
      ).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

export function useDocumentHistory(eid: string, documentId: string) {
  return useQuery(evidenceDocumentHistoryOptions({ path: { eid, document_id: documentId } }))
}

// 抽取结果修改: 合同走 /contracts/{id}, 回函走 /confirmations/{id}
export function useEditExtraction(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      target,
      id,
      body,
    }: {
      target: 'contracts' | 'confirmations' | 'registry-extracts'
      id: string
      body: ExtractionEdit
    }) => {
      if (target === 'contracts')
        return (await evidenceEditContract({ path: { eid, contract_id: id }, body, throwOnError: true })).data
      if (target === 'registry-extracts')
        return (await evidenceEditRegistryExtract({ path: { eid, document_id: id }, body, throwOnError: true })).data
      return (await evidenceEditConfirmation({ path: { eid, confirmation_id: id }, body, throwOnError: true })).data
    },
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R15 补交资料
export function useDataRequests(eid: string) {
  return useQuery(dataDataRequestsOptions({ path: { eid } }))
}

// 补交请求的资料名称, 按表种类或文件类型本地化
export function requestTitle(request: DataRequest, t: Messages): string {
  if (request.table_kind) return t.data.tables[request.table_kind] ?? request.title
  if (request.document_type) return t.data.docTypes[request.document_type] ?? request.title
  return request.title
}

// R280 客户无此类资料 (理由选填, 需求 R360)
export function useMarkUnavailable(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ requestId, reason }: { requestId: string; reason: string }) =>
      (await dataMarkRequestUnavailable({ path: { eid, request_id: requestId }, body: { reason }, throwOnError: true }))
        .data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 最近一次体检有不通过的项时从对话框确认, 带上理由 (选填, 可为空串; 需求 R360); 直接确认时不带
export function useDataReady(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (reason?: string) =>
      (await dataReady({ path: { eid }, body: reason === undefined ? undefined : { reason }, throwOnError: true }))
        .data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R16 查看与修改
export function useDataTable(eid: string, kind: TableKind, query: { q?: string; offset: number; limit: number }) {
  return useQuery(
    dataTablePageOptions({
      path: { eid, kind },
      query: { q: query.q || undefined, offset: query.offset, limit: query.limit },
    }),
  )
}

export function useAccountHistory(eid: string, code: string | null) {
  return useQuery({ ...dataAccountHistoryOptions({ path: { eid, code: code ?? '' } }), enabled: Boolean(code) })
}

export function useSetAccountCategory(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ code, category, reason }: { code: string; category: AccountCategory; reason: string }) =>
      (await dataSetAccountCategory({ path: { eid, code }, body: { category, reason }, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// 标准分类, 与后端 records.AccountCategory 同序
export const CATEGORIES_BY_TYPE: Record<string, AccountCategory[]> = {
  asset: [
    'cash',
    'receivables',
    'inventory',
    'prepayments',
    'other_current_assets',
    'property_plant_equipment',
    'intangible_assets',
    'other_non_current_assets',
  ],
  liability: [
    'payables',
    'accruals',
    'tax_payable',
    'employee_provisions',
    'borrowings',
    'other_current_liabilities',
    'other_non_current_liabilities',
  ],
  equity: ['share_capital', 'retained_earnings', 'other_equity'],
  revenue: ['revenue', 'other_income'],
  expense: [
    'cost_of_sales',
    'employee_expenses',
    'depreciation_amortisation',
    'finance_costs',
    'operating_expenses',
    'income_tax',
  ],
}
