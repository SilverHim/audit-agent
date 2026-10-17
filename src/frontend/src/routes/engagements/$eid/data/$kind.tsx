import { createFileRoute, Link, notFound, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { Upload } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Code } from '@/components/chrome'
import { ImportDialog } from '@/components/import-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  CATEGORIES_BY_TYPE,
  type CategoryBasis,
  TABLE_KINDS,
  type TableColumn,
  type TableKind,
  useAccountHistory,
  useDataSummary,
  useDataTable,
  useSetAccountCategory,
} from '@/lib/data-api'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { NotAvailable } from '@/lib/new-api'

type Search = { q?: string; page?: number; selected?: string }

export const Route = createFileRoute('/engagements/$eid/data/$kind')({
  beforeLoad: ({ params }) => {
    if (!TABLE_KINDS.includes(params.kind as TableKind)) throw notFound()
  },
  validateSearch: (search: Record<string, unknown>): Search => ({
    q: typeof search.q === 'string' && search.q ? search.q : undefined,
    page: typeof search.page === 'number' && search.page > 1 ? search.page : undefined,
    selected: typeof search.selected === 'string' ? search.selected : undefined,
  }),
  component: DataTablePage,
})

const LIMIT = 50

// 余额按会计惯例: 贷方 (负数) 加括号
function money(cents: unknown): string {
  if (typeof cents !== 'number') return '—'
  const value = (Math.abs(cents) / 100).toLocaleString('en-AU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return cents < 0 ? `(${value})` : value
}

type AccountRow = {
  code: string
  name: string
  type: string
  category: string | null
  category_basis: CategoryBasis
  balance_current_cents: number | null
  balance_prior_cents: number | null
  edited: boolean
}

function DataTablePage() {
  const { eid, kind: rawKind } = Route.useParams()
  const kind = rawKind as TableKind
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { t } = useI18n()
  const page = search.page ?? 1
  const table = useDataTable(eid, kind, { q: search.q, offset: (page - 1) * LIMIT, limit: LIMIT })
  const summary = useDataSummary(eid).data?.tables.find((item) => item.kind === kind)
  const [query, setQuery] = useState(search.q ?? '')
  const [importing, setImporting] = useState(false)

  // 输入停顿 300 毫秒后再写进地址栏
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if ((search.q ?? '') !== query)
        void navigate({ search: (prev) => ({ ...prev, q: query || undefined, page: undefined }) })
    }, 300)
    return () => window.clearTimeout(timer)
  }, [query, search.q, navigate])

  const data = table.data
  const total = data?.total ?? 0
  const source =
    summary?.source === 'xero' ? t.data.xero.name : summary?.source === 'import' ? (summary.file_name ?? '') : null

  return (
    <>
      <header className="flex flex-col gap-2.5 border-b bg-card px-4 pt-4 sm:px-7">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex min-w-0 flex-col">
            <span className="text-[12.5px]">
              <Link to="/engagements/$eid/data" params={{ eid }} search={{}}>
                {t.data.title}
              </Link>{' '}
              <span className="text-muted-foreground">/</span>
            </span>
            <h1 className="m-0 font-semibold text-xl tracking-tight">{t.data.tables[kind]}</h1>
            {source !== null && data ? (
              <span className="text-[13px] text-muted-foreground">{t.dataTable.meta(source, total)}</span>
            ) : null}
          </div>
          <Button type="button" variant="outline" size="lg" onClick={() => setImporting(true)}>
            <Upload className="size-4" aria-hidden="true" />
            {t.dataTable.importNew}
          </Button>
        </div>
        <nav aria-label={t.data.financialData} className="-mb-px flex gap-0.5 overflow-x-auto">
          {TABLE_KINDS.map((item) => (
            <Link
              key={item}
              to="/engagements/$eid/data/$kind"
              params={{ eid, kind: item }}
              search={{}}
              className="whitespace-nowrap border-transparent border-b-2 px-3 py-2.5 text-[13.5px] text-muted-foreground no-underline"
              activeProps={{ className: 'border-primary! font-semibold text-foreground!', 'aria-current': 'page' }}
            >
              {t.data.tables[item]}
            </Link>
          ))}
        </nav>
      </header>

      <div className="flex flex-wrap items-start gap-5 px-4 py-5 sm:px-7">
        <section className="flex min-w-0 flex-[999_1_560px] flex-col gap-3">
          <label className="sr-only" htmlFor="data-search">
            {t.dataTable.search}
          </label>
          <Input
            id="data-search"
            type="search"
            className="h-[38px] max-w-[420px]"
            placeholder={kind === 'accounts' ? t.dataTable.searchAccounts : t.dataTable.search}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {table.error ? (
            <p className="text-destructive text-sm">{apiMessage(table.error)}</p>
          ) : !data?.rows.length ? (
            <p className="rounded-xl border bg-card px-4 py-4 text-[13px] text-muted-foreground">
              {table.isPending ? t.app.loading : t.dataTable.none}
            </p>
          ) : kind === 'accounts' ? (
            <AccountsTable
              rows={data.rows as AccountRow[]}
              selected={search.selected}
              onSelect={(code) => void navigate({ search: (prev) => ({ ...prev, selected: code }) })}
            />
          ) : (
            <GenericTable columns={data.columns} rows={data.rows} />
          )}
          {total > LIMIT ? (
            <div className="flex flex-wrap items-center gap-2.5">
              <Button
                type="button"
                variant="outline"
                disabled={page <= 1}
                onClick={() =>
                  void navigate({ search: (prev) => ({ ...prev, page: page - 1 > 1 ? page - 1 : undefined }) })
                }
              >
                {t.trailPage.previous}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={page * LIMIT >= total}
                onClick={() => void navigate({ search: (prev) => ({ ...prev, page: page + 1 }) })}
              >
                {t.trailPage.next}
              </Button>
              <span className="text-[13px] text-muted-foreground">
                {t.dataTable.range((page - 1) * LIMIT + 1, Math.min(page * LIMIT, total), total)}
              </span>
            </div>
          ) : null}
        </section>

        {kind === 'accounts' && data?.rows.length ? (
          <AccountAside eid={eid} row={(data.rows as AccountRow[]).find((row) => row.code === search.selected)} />
        ) : null}
      </div>

      <ImportDialog eid={eid} open={importing} kind={kind} onOpenChange={setImporting} />
    </>
  )
}

function AccountsTable({
  rows,
  selected,
  onSelect,
}: {
  rows: AccountRow[]
  selected: string | undefined
  onSelect: (code: string) => void
}) {
  const { t } = useI18n()
  const cell = 'border-line-soft border-b px-3 py-2'
  return (
    <div className="overflow-x-auto rounded-xl border bg-card">
      <table className="w-full min-w-[680px] border-collapse text-[13.5px]">
        <thead>
          <tr className="bg-[#fafbfc] text-left text-[12px] text-muted-foreground">
            <th scope="col" className="border-b px-4 py-2.5 font-semibold">
              {t.dataTable.cols.code}
            </th>
            <th scope="col" className="border-b px-3 py-2.5 font-semibold">
              {t.dataTable.cols.name}
            </th>
            <th scope="col" className="border-b px-3 py-2.5 font-semibold">
              {t.dataTable.cols.type}
            </th>
            <th scope="col" className="border-b px-3 py-2.5 font-semibold">
              {t.dataTable.cols.category}
            </th>
            <th scope="col" className="border-b px-3 py-2.5 text-right font-semibold">
              {t.dataTable.cols.current}
            </th>
            <th scope="col" className="border-b px-4 py-2.5 text-right font-semibold">
              {t.dataTable.cols.prior}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const on = row.code === selected
            return (
              <tr key={row.code} className={on ? 'bg-accent shadow-[inset_3px_0_0_var(--primary)]' : 'bg-card'}>
                <td className={cn(cell, 'px-4 font-mono text-[13px]')}>{row.code}</td>
                <td className={cell}>
                  <button
                    type="button"
                    aria-pressed={on}
                    onClick={() => onSelect(row.code)}
                    className="border-0 bg-transparent p-0 text-left text-accent-foreground"
                  >
                    {row.name}
                  </button>
                </td>
                <td className={cn(cell, 'text-[#4a5262]')}>{t.dataTable.accountTypes[row.type] ?? row.type}</td>
                <td className={cell}>
                  <span className="inline-flex items-center gap-1.5">
                    {row.category ? (t.dataTable.categories[row.category] ?? row.category) : t.dataTable.unmapped}
                    {row.edited ? (
                      <span className="rounded-full bg-standard-soft px-[7px] font-medium text-[12px] text-standard">
                        {t.dataTable.edited}
                      </span>
                    ) : null}
                  </span>
                </td>
                <td className={cn(cell, 'text-right font-mono text-[13px]')}>{money(row.balance_current_cents)}</td>
                <td className={cn(cell, 'px-4 text-right font-mono text-[#4a5262] text-[13px]')}>
                  {money(row.balance_prior_cents)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function AccountAside({ eid, row }: { eid: string; row: AccountRow | undefined }) {
  const { t, formatDateTime } = useI18n()
  const history = useAccountHistory(eid, row?.code ?? null)
  const save = useSetAccountCategory(eid)
  const [category, setCategory] = useState<string | null>(null)
  const [reason, setReason] = useState('')

  // 换选科目时清空未保存的修改
  // biome-ignore lint/correctness/useExhaustiveDependencies: 只在选中科目变化时重置
  useEffect(() => {
    setCategory(null)
    setReason('')
  }, [row?.code])

  if (!row) {
    return (
      <aside className="flex-[1_1_340px] rounded-[14px] border bg-card px-5 py-5 text-[13.5px] text-muted-foreground">
        {t.dataTable.selectAccount}
      </aside>
    )
  }
  const draft = category ?? row.category ?? ''
  const canSave = draft !== (row.category ?? '') && reason.trim().length > 0
  return (
    <aside
      aria-label={row.name}
      className="flex min-w-0 flex-[1_1_340px] flex-col gap-4 rounded-[14px] border bg-card px-[22px] py-5"
    >
      <div className="flex flex-col gap-0.5">
        <span className="font-mono text-[12.5px] text-muted-foreground">
          {row.code} · {t.dataTable.accountTypes[row.type] ?? row.type}
        </span>
        <h2 className="m-0 font-semibold text-[17px]">{row.name}</h2>
        <span className="text-[#4a5262] text-[13px]">
          {t.dataTable.balances(money(row.balance_current_cents), money(row.balance_prior_cents))}
        </span>
      </div>
      <label className="flex flex-col gap-1.5 font-semibold text-[13px]">
        {t.dataTable.cols.category}
        <select
          className="h-10 rounded-[9px] border border-input bg-card px-2.5 font-normal text-[14px]"
          value={draft}
          onChange={(event) => setCategory(event.target.value)}
        >
          {row.category ? null : <option value="">{t.dataTable.unmapped}</option>}
          {(CATEGORIES_BY_TYPE[row.type] ?? []).map((item) => (
            <option key={item} value={item}>
              {t.dataTable.categories[item] ?? item}
            </option>
          ))}
        </select>
        {row.category_basis ? (
          <span className="font-normal text-[12.5px] text-muted-foreground">
            {t.dataTable.basis[row.category_basis]}
          </span>
        ) : null}
      </label>
      <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
        <label htmlFor="account-reason">
          {t.dataTable.reason} <span className="text-risk">*</span>
        </label>
        <Textarea
          id="account-reason"
          rows={3}
          className="font-normal text-[14px]"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </div>
      <div className="flex justify-end gap-2.5">
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            setCategory(null)
            setReason('')
          }}
        >
          {t.dataTable.cancel}
        </Button>
        <Button
          type="button"
          disabled={!canSave || save.isPending}
          onClick={() =>
            save.mutate(
              { code: row.code, category: draft, reason: reason.trim() },
              {
                onSuccess: () => {
                  toast.success(t.dataTable.saved)
                  setCategory(null)
                  setReason('')
                },
                onError: (error) =>
                  toast.error(error instanceof NotAvailable ? t.importDialog.unavailable : apiMessage(error)),
              },
            )
          }
        >
          {t.dataTable.save}
        </Button>
      </div>
      {history.data?.length ? (
        <div className="flex flex-col gap-2 border-line-soft border-t pt-3.5">
          <h3 className="m-0 font-semibold text-[14px]">{t.dataTable.history}</h3>
          {history.data.map((version) => (
            <div key={version.version} className="flex flex-col gap-0.5 text-[13px]">
              <span className="flex justify-between gap-2">
                <span className="font-semibold">
                  {t.dataTable.version(version.version)} · {version.changed_by}
                </span>
                <span className="text-muted-foreground">{formatDateTime(version.changed_at)}</span>
              </span>
              <span className="text-[#2d3443]">
                {version.category
                  ? (t.dataTable.categories[version.category] ?? version.category)
                  : t.dataTable.unmapped}
                {version.basis ? ` · ${t.dataTable.basis[version.basis]}` : ''}
              </span>
              {version.reason ? <span className="text-[#4a5262]">“{version.reason}”</span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </aside>
  )
}

function GenericTable({ columns, rows }: { columns: TableColumn[]; rows: Array<Record<string, unknown>> }) {
  const { t, formatDate, formatDateTime } = useI18n()
  const show = (column: TableColumn, value: unknown) => {
    if (value === null || value === undefined || value === '') return '—'
    if (column.type === 'amount') return money(value)
    if (column.type === 'date') return formatDate(String(value))
    if (column.type === 'datetime') return formatDateTime(String(value))
    if (column.type === 'bool') return value ? '✓' : '—'
    return String(value)
  }
  const numeric = (column: TableColumn) => column.type === 'amount'
  return (
    <div className="overflow-x-auto rounded-xl border bg-card">
      <table className="w-full border-collapse text-[13.5px]">
        <thead>
          <tr className="bg-[#fafbfc] text-left text-[12px] text-muted-foreground">
            {columns.map((column) => (
              <th
                key={column.name}
                scope="col"
                className={cn('whitespace-nowrap border-b px-3 py-2.5 font-semibold', numeric(column) && 'text-right')}
              >
                {t.importDialog.fields[column.name] ?? column.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={String(row.id ?? index)}>
              {columns.map((column) => (
                <td
                  key={column.name}
                  className={cn(
                    'whitespace-nowrap border-line-soft border-b px-3 py-2',
                    numeric(column) && 'text-right font-mono text-[13px]',
                  )}
                >
                  {column.type === 'text' && /(_no|_id|^id|code)$/.test(column.name) ? (
                    <Code className="text-[13px]">{show(column, row[column.name])}</Code>
                  ) : (
                    show(column, row[column.name])
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
