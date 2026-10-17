import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { DocumentRef } from '@/client/types.gen'
import { getToken } from '@/lib/auth'
import { invalidateEngagement } from '@/lib/engagement'
import { call, NotAvailable, orEmpty } from '@/lib/new-api'

// 数据管理接口 (后端需求 R10-R16). 类型按需求文档写; 后端合入并重新生成客户端后改用 src/client 的生成函数.
// 现有接口 (documents, contracts, confirmations, evidence) 直接用生成的客户端

export type TableKind =
  | 'accounts'
  | 'trial_balance'
  | 'journal_entries'
  | 'sales_invoices'
  | 'bank_accounts'
  | 'bank_transactions'
  | 'delivery_notes'
  | 'approval_log'

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

export type XeroStatus = 'connected' | 'not_connected' | 'expired'
export type TableStatus = 'received' | 'converting' | 'not_received'
export type CheckStatus = 'passed' | 'failed' | 'missing_data' | 'pending'
export type CheckId =
  | 'debits_equal_credits'
  | 'journals_agree_tb'
  | 'count_agrees_source'
  | 'mapping_complete'
  | 'preparer_approver_available'
  | 'tb_agrees_statements'

export type TableSummary = {
  kind: TableKind
  source: 'xero' | 'import' | null
  file_name: string | null
  records: number
  records_detail: { current: number; prior: number } | null
  period_from: string | null
  period_to: string | null
  status: TableStatus
  updated_at: string | null
}

export type DataCheck = {
  id: CheckId
  status: CheckStatus
  params: Record<string, unknown>
  request_id: string | null
}

export type DataSummary = {
  sources: {
    xero: { status: XeroStatus; organisation: string | null; last_synced_at: string | null; scope: TableKind[] }
  }
  tables: TableSummary[]
  checks: DataCheck[]
  processing: number
  open_requests: number
}

export function dataKey(eid: string, part: string) {
  return [{ _id: `data-${part}`, path: { eid } }] as const
}

function invalidateData(queryClient: ReturnType<typeof useQueryClient>, eid: string) {
  return Promise.all([
    queryClient.invalidateQueries({
      predicate: (query) => String((query.queryKey[0] as { _id?: string })?._id).startsWith('data-'),
    }),
    invalidateEngagement(queryClient, eid),
  ])
}

// R11 概览; 接口未上线时为 null
export function useDataSummary(eid: string) {
  return useQuery({
    queryKey: dataKey(eid, 'summary'),
    queryFn: () => orEmpty(call<DataSummary | null>('get', '/api/v1/engagements/{eid}/data/summary', { eid }), null),
    refetchInterval: (query) => (query.state.data?.processing ? 3000 : false),
  })
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
    onSettled: () => invalidateData(queryClient, eid),
  })
}

export function useXeroDisconnect(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => call<unknown>('delete', '/api/v1/engagements/{eid}/data/xero', { eid }),
    onSettled: () => invalidateData(queryClient, eid),
  })
}

// R13 账表导入
export type DateFormat = 'dmy' | 'ymd' | 'mdy'
export type FieldType = 'text' | 'date' | 'datetime' | 'amount' | 'bool'
export type TableSchema = { kind: TableKind; fields: Array<{ name: string; required: boolean; type: FieldType }> }

export type ImportPreview = {
  import_id: string
  file_name: string
  sheets: string[]
  sheet: string
  row_count: number
  columns: string[]
  sample: Record<string, string>
  suggested_kind: TableKind | null
  suggested_mapping: Record<string, string | null>
  suggested_date_format: DateFormat
}

export type ImportCommit = {
  kind: TableKind
  sheet: string
  date_format: DateFormat
  mapping: Record<string, string | null>
}

// 字段定义不随项目变化, 取到后不再刷新; 没取到 (空) 时下次打开导入窗口重取
export function useTableSchemas(enabled: boolean) {
  return useQuery({
    queryKey: [{ _id: 'data-schemas' }],
    queryFn: () => orEmpty(call<TableSchema[]>('get', '/api/v1/data/schemas', {}), []),
    enabled,
    staleTime: (query) => (query.state.data?.length ? Number.POSITIVE_INFINITY : 0),
  })
}

async function upload<T>(url: string, files: File[], fields: Record<string, string> = {}): Promise<T> {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  for (const [key, value] of Object.entries(fields)) form.append(key, value)
  const response = await fetch(url, {
    method: 'POST',
    headers: { Authorization: `Bearer ${getToken() ?? ''}` },
    body: form,
  })
  const payload: unknown = await response.json().catch(() => null)
  const code = (payload as { detail?: { code?: string } } | null)?.detail?.code
  if (response.status === 405 || (response.status === 404 && !code)) throw new NotAvailable(url)
  if (!response.ok) throw payload ?? new Error(`HTTP ${response.status}`)
  return payload as T
}

export function startImport(eid: string, file: File, requestId?: string) {
  return upload<ImportPreview>(
    `/api/v1/engagements/${encodeURIComponent(eid)}/data/imports`,
    [file],
    requestId ? { request_id: requestId } : {},
  )
}

export function changeImportSheet(eid: string, importId: string, sheet: string) {
  return call<ImportPreview>(
    'put',
    '/api/v1/engagements/{eid}/data/imports/{import_id}/sheet',
    { eid, import_id: importId },
    { sheet },
  )
}

export function useCommitImport(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ importId, body }: { importId: string; body: ImportCommit }) =>
      call<{ kind: TableKind; records: number }>(
        'post',
        '/api/v1/engagements/{eid}/data/imports/{import_id}/commit',
        { eid, import_id: importId },
        body,
      ),
    onSettled: () => invalidateData(queryClient, eid),
  })
}

// R14 文件
export function useUploadDocuments(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ files, requestId }: { files: File[]; requestId?: string }) =>
      upload<DocumentRef[]>(
        `/api/v1/engagements/${encodeURIComponent(eid)}/documents`,
        files,
        requestId ? { request_id: requestId } : {},
      ),
    onSettled: () =>
      Promise.all([
        invalidateData(queryClient, eid),
        queryClient.invalidateQueries({ queryKey: [{ _id: 'evidenceListDocuments', path: { eid } }], exact: false }),
      ]),
  })
}

export function useSetDocumentType(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ documentId, type, reason }: { documentId: string; type: string; reason: string }) =>
      call<unknown>(
        'put',
        '/api/v1/engagements/{eid}/documents/{document_id}/type',
        { eid, document_id: documentId },
        { type, reason },
      ),
    onSettled: () => invalidateData(queryClient, eid),
  })
}

export type ExtractionEdit = { field: string; value: string | number | null; reason: string }

// 抽取结果修改: 合同走 /contracts/{id}, 回函走 /confirmations/{id}
export function useEditExtraction(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ target, id, body }: { target: 'contracts' | 'confirmations'; id: string; body: ExtractionEdit }) =>
      call<unknown>('put', `/api/v1/engagements/{eid}/${target}/{id}`, { eid, id }, body),
    onSettled: () =>
      queryClient.invalidateQueries({
        predicate: (query) => /evidenceList(Contracts|Confirmations)/.test(JSON.stringify(query.queryKey)),
      }),
  })
}

// R15 补交资料
export type DataRequest = {
  id: string
  kind: 'table' | 'document'
  table_kind: TableKind | null
  document_type: string | null
  title: string
  used_for: string[]
  reason_code: string
  reason_params: Record<string, unknown>
  format_hint: string | null
  status: 'open' | 'received' | 'withdrawn'
  raised_by: string
  created_at: string
}

export function useDataRequests(eid: string) {
  return useQuery({
    queryKey: dataKey(eid, 'requests'),
    queryFn: () => orEmpty(call<DataRequest[]>('get', '/api/v1/engagements/{eid}/data/requests', { eid }), []),
  })
}

export function useDataReady(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => call<unknown>('post', '/api/v1/engagements/{eid}/data/ready', { eid }),
    onSettled: () => invalidateData(queryClient, eid),
  })
}

// R16 查看与修改
export type TableColumn = { name: string; type: FieldType }
export type TablePage = { total: number; columns: TableColumn[]; rows: Array<Record<string, unknown>> }
export type CategoryBasis = 'xero_type' | 'reporting_code' | 'name' | 'auditor' | null

export function useDataTable(eid: string, kind: TableKind, query: { q?: string; offset: number; limit: number }) {
  return useQuery({
    queryKey: [{ _id: 'data-table', path: { eid, kind }, query }],
    queryFn: () =>
      orEmpty(
        call<TablePage | null>('get', '/api/v1/engagements/{eid}/data/tables/{kind}', { eid, kind }, undefined, {
          q: query.q || undefined,
          offset: query.offset,
          limit: query.limit,
        }),
        null,
      ),
  })
}

export type CategoryVersion = {
  version: number
  category: string | null
  basis: CategoryBasis
  changed_by: string
  changed_at: string
  reason: string | null
}

export function useAccountHistory(eid: string, code: string | null) {
  return useQuery({
    queryKey: [{ _id: 'data-account-history', path: { eid, code } }],
    queryFn: () =>
      orEmpty(
        call<CategoryVersion[]>('get', '/api/v1/engagements/{eid}/data/accounts/{code}/history', {
          eid,
          code: code ?? '',
        }),
        [],
      ),
    enabled: Boolean(code),
  })
}

export function useSetAccountCategory(eid: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ code, category, reason }: { code: string; category: string; reason: string }) =>
      call<unknown>('put', '/api/v1/engagements/{eid}/data/accounts/{code}', { eid, code }, { category, reason }),
    onSettled: () => invalidateData(queryClient, eid),
  })
}

// 标准分类, 与后端 records.AccountCategory 同序
export const CATEGORIES_BY_TYPE: Record<string, string[]> = {
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
