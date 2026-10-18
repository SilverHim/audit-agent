import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import {
  dataAccountHistoryOptions,
  dataDataRequestsOptions,
  dataSummaryOptions,
  dataTablePageOptions,
  dataTableSchemasOptions,
} from '@/client/@tanstack/react-query.gen'
import {
  dataChangeImportSheet,
  dataCommitImport,
  dataReady,
  dataSetAccountCategory,
  dataStartImport,
  evidenceEditConfirmation,
  evidenceEditContract,
  evidenceSetDocumentType,
  evidenceUploadDocuments,
} from '@/client/sdk.gen'
import type {
  AccountCategoryIn,
  CategoryVersion,
  DocumentType,
  ExtractionEdit,
  ImportCommit,
  TableKind,
} from '@/client/types.gen'
import { invalidateEngagement } from '@/lib/engagement'
import { call } from '@/lib/new-api'

// 数据管理接口 (后端需求 R10-R16) 用生成的客户端; 查询键都带 path.eid, 写操作后按项目整体失效.
// Xero 接口 (R12) 还没合入, 暂用过渡调用

export type {
  CategoryVersion,
  DataCheck,
  DataRequest,
  DataSummary,
  ExtractionEdit,
  ImportCommit,
  ImportPreview,
  TableColumn,
  TableKind,
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
  'bank_accounts',
  'bank_transactions',
  'delivery_notes',
  'approval_log',
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

// R12 Xero
export function useXeroConnect(eid: string) {
  return useMutation({
    mutationFn: () => call<{ authorize_url: string }>('get', '/api/v1/engagements/{eid}/data/xero/connect', { eid }),
    onSuccess: ({ authorize_url }) => window.location.assign(authorize_url),
  })
}

export function useXeroSync(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => call<unknown>('post', '/api/v1/engagements/{eid}/data/xero/sync', { eid }),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

export function useXeroDisconnect(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => call<unknown>('delete', '/api/v1/engagements/{eid}/data/xero', { eid }),
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R13 账表导入. 字段定义不随项目变化, 取到后不再刷新
export function useTableSchemas(enabled: boolean) {
  return useQuery({ ...dataTableSchemasOptions(), enabled, staleTime: Number.POSITIVE_INFINITY })
}

export async function startImport(eid: string, file: File, requestId?: string) {
  const { data } = await dataStartImport({
    path: { eid },
    body: { files: [file], request_id: requestId },
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

// R14 文件
export function useUploadDocuments(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ files, requestId }: { files: File[]; requestId?: string }) =>
      (await evidenceUploadDocuments({ path: { eid }, body: { files, request_id: requestId }, throwOnError: true }))
        .data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

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

// 抽取结果修改: 合同走 /contracts/{id}, 回函走 /confirmations/{id}
export function useEditExtraction(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      target,
      id,
      body,
    }: {
      target: 'contracts' | 'confirmations'
      id: string
      body: ExtractionEdit
    }) =>
      target === 'contracts'
        ? (await evidenceEditContract({ path: { eid, contract_id: id }, body, throwOnError: true })).data
        : (await evidenceEditConfirmation({ path: { eid, confirmation_id: id }, body, throwOnError: true })).data,
    onSettled: () => invalidateEngagement(queryClient, eid),
  })
}

// R15 补交资料
export function useDataRequests(eid: string) {
  return useQuery(dataDataRequestsOptions({ path: { eid } }))
}

// 最近一次体检有不通过的项时须带理由 (否则 409 DATA_CHECKS_FAILED)
export function useDataReady(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (reason?: string) =>
      (await dataReady({ path: { eid }, body: reason ? { reason } : undefined, throwOnError: true })).data,
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
