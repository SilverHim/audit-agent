import { cn } from 'cn'
import { Check, FileSpreadsheet, Upload } from 'lucide-react'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { autoMap, type Mapping, RULE } from '@/lib/batch-plan'
import {
  changeImportSheet,
  type DateFormat,
  type ImportPreview,
  startImport,
  TABLE_KINDS,
  type TableField,
  type TableKind,
  useCommitImport,
  useTableSchemas,
} from '@/lib/data-api'
import { errorText, type ParseFailure, parseFailures } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

export const SELECT = 'h-10 w-full rounded-[9px] border border-input bg-card px-2.5 text-[13.5px]'

export function ImportDialog({
  eid,
  open,
  onOpenChange,
  kind: presetKind,
}: {
  eid: string
  open: boolean
  onOpenChange: (open: boolean) => void
  kind?: TableKind
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
      const result = await startImport(eid, file)
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

              <MappingTable
                fields={schema?.fields ?? []}
                columns={preview.columns}
                sample={preview.sample}
                mapping={mapping}
                onChange={(field, column) => setMapping((current) => ({ ...current, [field]: column }))}
              />

              {failures?.length ? (
                <div role="alert" className="flex flex-col gap-2 rounded-[10px] bg-risk-soft px-3.5 py-3">
                  <span className="font-semibold text-[13px] text-risk">{t.importDialog.parseErrors}</span>
                  <FailureTable failures={failures} />
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

// 列对应表: 每个系统字段选文件中的一列 (或不导入), 旁边是该列的示例. 账表导入与智能导入的调整单项共用.
// rules 为可沿用规整方案取法的字段 (字段 → 说明), 取值为 RULE 时选中它; ruleColumns 是这些取法用到的列, 不算未对应;
// missing 为对应的列不在当前表头里的字段 (照原列名显示, 标红并写 missingText)
export function MappingTable({
  fields,
  columns,
  sample,
  mapping,
  onChange,
  rules = {},
  ruleColumns = [],
  sampleLabel,
  missing,
  missingText,
}: {
  fields: TableField[]
  columns: string[]
  sample: Record<string, string>
  mapping: Mapping
  onChange: (field: string, column: string | null) => void
  rules?: Record<string, string>
  ruleColumns?: string[]
  sampleLabel?: string
  missing?: Set<string>
  missingText?: string
}) {
  const { t } = useI18n()
  const used = new Set([...Object.values(mapping).filter((value) => value && value !== RULE), ...ruleColumns])
  const unmatched = columns.filter((column) => !used.has(column))
  return (
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
                {sampleLabel ?? t.importDialog.sample}
              </th>
            </tr>
          </thead>
          <tbody>
            {fields.map((field) => {
              const column = mapping[field.name] ?? null
              const rule = rules[field.name]
              const absent = missing?.has(field.name) ?? false
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
                      className={cn(SELECT, 'h-9', absent && 'border-risk text-risk')}
                      value={column ?? ''}
                      onChange={(event) => onChange(field.name, event.target.value || null)}
                    >
                      <option value="">{t.importDialog.notImported}</option>
                      {rule ? <option value={RULE}>{t.batch.sources.rule(rule)}</option> : null}
                      {column && column !== RULE && !columns.includes(column) ? (
                        <option value={column}>{column}</option>
                      ) : null}
                      {columns.map((item) => (
                        <option key={item} value={item}>
                          {item}
                        </option>
                      ))}
                    </select>
                    {absent && missingText ? (
                      <span className="mt-1 block text-[12.5px] text-risk">{missingText}</span>
                    ) : null}
                  </td>
                  <td className="break-all border-line-soft border-b px-3.5 py-2 font-mono text-[13px]">
                    {column === RULE ? (
                      <span className="font-sans text-[#4a5262]">{rule}</span>
                    ) : column ? (
                      (sample[column] ?? '')
                    ) : (
                      ''
                    )}
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
  )
}

// 解析失败的前 20 处: 行号, 列, 原值
export function FailureTable({ failures }: { failures: ParseFailure[] }) {
  const { t } = useI18n()
  return (
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
            <tr key={`${failure.row}-${failure.column}-${failure.value}`} className="border-line-soft border-t">
              <td className="py-1 pr-3 font-mono">{failure.row}</td>
              <td className="py-1 pr-3">{failure.column}</td>
              <td className="break-all py-1 font-mono">{failure.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
