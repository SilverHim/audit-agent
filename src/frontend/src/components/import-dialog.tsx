import { cn } from 'cn'
import { Check, FileSpreadsheet, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  changeImportSheet,
  type DateFormat,
  type ImportPreview,
  startImport,
  TABLE_KINDS,
  type TableKind,
  type TableSchema,
  useCommitImport,
  useTableSchemas,
} from '@/lib/data-api'
import { errorText, type ParseFailure, parseFailures } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

type Mapping = Record<string, string | null>

const SELECT = 'h-10 w-full rounded-[9px] border border-input bg-card px-2.5 text-[13.5px]'

// 换数据类型时按列名重新对应: 先取后端建议, 再按字段名与列名 (忽略大小写与分隔符) 相同的配对
function autoMap(schema: TableSchema | undefined, columns: string[], suggested: Mapping): Mapping {
  const plain = (value: string) => value.toLowerCase().replace(/[^a-z0-9]/g, '')
  const result: Mapping = {}
  for (const field of schema?.fields ?? []) {
    const hint = suggested[field.name]
    result[field.name] =
      hint && columns.includes(hint) ? hint : (columns.find((column) => plain(column) === plain(field.name)) ?? null)
  }
  return result
}

export function ImportDialog({
  eid,
  open,
  onOpenChange,
  kind: presetKind,
  requestId,
}: {
  eid: string
  open: boolean
  onOpenChange: (open: boolean) => void
  kind?: TableKind
  requestId?: string
}) {
  const { t } = useI18n()
  const schemas = useTableSchemas(open).data ?? []
  const commit = useCommitImport(eid)
  const input = useRef<HTMLInputElement>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [reading, setReading] = useState(false)
  const [kind, setKind] = useState<TableKind>(presetKind ?? 'accounts')
  const [dateFormat, setDateFormat] = useState<DateFormat>('dmy')
  const [mapping, setMapping] = useState<Mapping>({})
  const [failures, setFailures] = useState<ParseFailure[] | null>(null)

  const schema = schemas.find((item) => item.kind === kind)
  const used = new Set(Object.values(mapping).filter(Boolean))
  const unmatched = preview?.columns.filter((column) => !used.has(column)) ?? []
  const missingRequired = (schema?.fields ?? []).some((field) => field.required && !mapping[field.name])

  const reset = () => {
    setPreview(null)
    setMapping({})
    setFailures(null)
    if (input.current) input.current.value = ''
  }

  const read = async (file: File) => {
    setReading(true)
    try {
      const result = await startImport(eid, file, requestId)
      const nextKind = presetKind ?? result.suggested_kind ?? kind
      setPreview(result)
      setFailures(null)
      setKind(nextKind)
      setDateFormat(result.suggested_date_format)
      setMapping(
        autoMap(
          schemas.find((item) => item.kind === nextKind),
          result.columns,
          result.suggested_mapping,
        ),
      )
    } catch (error) {
      toast.error(errorText(error, t))
    } finally {
      setReading(false)
    }
  }

  const pickSheet = async (sheet: string) => {
    if (!preview) return
    try {
      const result = await changeImportSheet(eid, preview.import_id, sheet)
      setPreview(result)
      setMapping(autoMap(schema, result.columns, result.suggested_mapping))
      setFailures(null)
    } catch (error) {
      toast.error(errorText(error, t))
    }
  }

  const close = (next: boolean) => {
    if (!next) reset()
    onOpenChange(next)
  }

  const submit = () => {
    if (!preview) return
    setFailures(null)
    commit.mutate(
      { importId: preview.import_id, body: { kind, sheet: preview.sheet, date_format: dateFormat, mapping } },
      {
        onSuccess: (result) => {
          toast.success(t.importDialog.done(t.data.tables[result.kind] ?? result.kind, result.records))
          close(false)
        },
        // 解析失败在窗口里逐处列出, 改了对应或日期格式后可直接再导入
        onError: (error) => {
          const found = parseFailures(error)
          if (found) setFailures(found)
          else toast.error(errorText(error, t))
        },
      },
    )
  }

  const steps = [
    { key: 'choose', label: t.importDialog.steps.choose, done: Boolean(preview), current: !preview },
    { key: 'match', label: t.importDialog.steps.match, done: false, current: Boolean(preview) },
    { key: 'import', label: t.importDialog.steps.import, done: false, current: false },
  ]

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[92vh] gap-0 overflow-y-auto p-0 sm:max-w-[820px]">
        <DialogHeader className="px-6 pt-5">
          <DialogTitle className="font-semibold text-lg">{t.importDialog.title}</DialogTitle>
        </DialogHeader>
        <ol className="m-0 flex list-none flex-wrap gap-x-5 gap-y-2 border-line-soft border-b px-6 py-4 text-[13.5px]">
          {steps.map((step, index) => (
            <li
              key={step.key}
              aria-current={step.current ? 'step' : undefined}
              className={cn(
                'flex items-center gap-2',
                step.done ? 'text-ok' : step.current ? 'font-semibold' : 'text-muted-foreground',
              )}
            >
              <span
                className={cn(
                  'flex size-[22px] items-center justify-center rounded-full text-xs',
                  step.done
                    ? 'bg-ok-soft'
                    : step.current
                      ? 'bg-primary text-primary-foreground'
                      : 'border-[1.5px] border-input',
                )}
              >
                {step.done ? <Check className="size-3.5" aria-hidden="true" /> : index + 1}
              </span>
              {step.label}
            </li>
          ))}
        </ol>

        <div className="flex flex-col gap-[18px] px-6 py-[18px]">
          <input
            ref={input}
            type="file"
            accept=".csv,.xlsx,.xls"
            className="sr-only"
            tabIndex={-1}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void read(file)
            }}
          />
          {!preview ? (
            <button
              type="button"
              disabled={reading}
              onClick={() => input.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault()
                const file = event.dataTransfer.files[0]
                if (file) void read(file)
              }}
              className="flex min-h-40 flex-col items-center justify-center gap-2 rounded-xl border-[1.5px] border-input border-dashed bg-[#fafbfc] px-4 text-[#4a5262] text-[14px]"
            >
              <Upload className="size-5" aria-hidden="true" />
              <span>{reading ? t.importDialog.reading : t.data.chooseFiles}</span>
              <span className="text-[12.5px] text-muted-foreground">{t.importDialog.choose}</span>
            </button>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-3 rounded-[10px] border bg-[#fafbfc] px-3.5 py-3">
                <span className="flex size-[34px] shrink-0 items-center justify-center rounded-lg bg-ok-soft text-ok">
                  <FileSpreadsheet className="size-[18px]" aria-hidden="true" />
                </span>
                <span className="flex min-w-[200px] flex-1 flex-col">
                  <span className="font-medium font-mono text-[13.5px]">{preview.file_name}</span>
                  <span className="text-[12.5px] text-muted-foreground">
                    {t.importDialog.sheetInfo(preview.sheet, preview.row_count, preview.columns.length)}
                  </span>
                </span>
                <Button type="button" variant="outline" onClick={() => input.current?.click()}>
                  {t.importDialog.changeFile}
                </Button>
              </div>

              <div className="flex flex-wrap gap-4">
                {preview.sheets.length > 1 ? (
                  <label className="flex flex-[1_1_200px] flex-col gap-1.5 font-semibold text-[13px]">
                    {t.importDialog.sheet}
                    <select
                      className={SELECT}
                      value={preview.sheet}
                      onChange={(event) => void pickSheet(event.target.value)}
                    >
                      {preview.sheets.map((sheet) => (
                        <option key={sheet} value={sheet}>
                          {sheet}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : null}
                <label className="flex flex-[1_1_200px] flex-col gap-1.5 font-semibold text-[13px]">
                  {t.importDialog.kind}
                  <select
                    className={SELECT}
                    value={kind}
                    disabled={Boolean(presetKind)}
                    onChange={(event) => {
                      const next = event.target.value as TableKind
                      setKind(next)
                      setMapping(
                        autoMap(
                          schemas.find((item) => item.kind === next),
                          preview.columns,
                          preview.suggested_mapping,
                        ),
                      )
                    }}
                  >
                    {TABLE_KINDS.map((item) => (
                      <option key={item} value={item}>
                        {t.data.tables[item]}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-[1_1_200px] flex-col gap-1.5 font-semibold text-[13px]">
                  {t.importDialog.dateFormat}
                  <select
                    className={SELECT}
                    value={dateFormat}
                    onChange={(event) => setDateFormat(event.target.value as DateFormat)}
                  >
                    {(['dmy', 'ymd', 'mdy'] as const).map((item) => (
                      <option key={item} value={item}>
                        {t.importDialog.dateFormats[item]}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="flex flex-col gap-2">
                <span className="font-semibold text-[13px]">{t.importDialog.match}</span>
                <div className="overflow-x-auto rounded-[10px] border">
                  <table className="w-full min-w-[600px] border-collapse text-[13.5px]">
                    <thead>
                      <tr className="bg-[#fafbfc] text-left text-[12px] text-muted-foreground">
                        <th scope="col" className="w-[30%] border-b px-3.5 py-2 font-semibold">
                          {t.importDialog.field}
                        </th>
                        <th scope="col" className="w-[36%] border-b px-3.5 py-2 font-semibold">
                          {t.importDialog.column}
                        </th>
                        <th scope="col" className="border-b px-3.5 py-2 font-semibold">
                          {t.importDialog.sample}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {(schema?.fields ?? []).map((field) => {
                        const column = mapping[field.name] ?? null
                        const id = `map-${field.name}`
                        return (
                          <tr key={field.name}>
                            <td className="border-line-soft border-b px-3.5 py-2">
                              <label htmlFor={id}>
                                {t.importDialog.fields[field.name] ?? field.name}
                                {field.required ? <span className="text-risk"> *</span> : null}
                              </label>
                            </td>
                            <td className="border-line-soft border-b px-3.5 py-1.5">
                              <select
                                id={id}
                                className={cn(SELECT, 'h-9')}
                                value={column ?? ''}
                                onChange={(event) =>
                                  setMapping((current) => ({ ...current, [field.name]: event.target.value || null }))
                                }
                              >
                                <option value="">{t.importDialog.notImported}</option>
                                {preview.columns.map((item) => (
                                  <option key={item} value={item}>
                                    {item}
                                  </option>
                                ))}
                              </select>
                            </td>
                            <td className="border-line-soft border-b px-3.5 py-2 font-mono text-[13px]">
                              {column ? (preview.sample[column] ?? '') : ''}
                            </td>
                          </tr>
                        )
                      })}
                      {unmatched.length ? (
                        <tr>
                          <td className="px-3.5 py-2 text-muted-foreground">{t.importDialog.unmatched}</td>
                          <td colSpan={2} className="px-3.5 py-2 text-[#4a5262] text-[13px]">
                            {t.importDialog.unmatchedValue(unmatched.join(', '))}
                          </td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
              </div>

              {failures?.length ? (
                <div role="alert" className="flex flex-col gap-2 rounded-[10px] bg-risk-soft px-3.5 py-3">
                  <span className="font-semibold text-[13px] text-risk">{t.importDialog.parseErrors}</span>
                  <div className="max-h-56 overflow-y-auto">
                    <table className="w-full border-collapse text-[13px]">
                      <thead>
                        <tr className="text-left text-[12px] text-muted-foreground">
                          <th scope="col" className="w-16 py-1 pr-3 font-semibold">
                            {t.importDialog.parseCols.row}
                          </th>
                          <th scope="col" className="w-[40%] py-1 pr-3 font-semibold">
                            {t.importDialog.parseCols.column}
                          </th>
                          <th scope="col" className="py-1 font-semibold">
                            {t.importDialog.parseCols.value}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {failures.map((failure) => (
                          <tr key={`${failure.row}-${failure.column}`} className="border-line-soft border-t">
                            <td className="py-1 pr-3 font-mono">{failure.row}</td>
                            <td className="py-1 pr-3">{failure.column}</td>
                            <td className="break-all py-1 font-mono">{failure.value}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : null}
            </>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2.5 border-line-soft border-t bg-[#fafbfc] px-6 py-3.5">
          {preview ? (
            <Button type="button" variant="outline" size="lg" onClick={reset}>
              {t.importDialog.back}
            </Button>
          ) : null}
          <Button
            type="button"
            size="lg"
            disabled={!preview || !schema || missingRequired || commit.isPending}
            onClick={submit}
          >
            {commit.isPending ? t.importDialog.importing : t.importDialog.import}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
