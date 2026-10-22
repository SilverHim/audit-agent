import type { LocalizedText, Workpaper, WorkpaperSection, WorkpaperTable } from '@/client/types.gen'
import { useI18n } from '@/lib/i18n'
import { formatMoney, formatPct } from '@/lib/money'

type WorkingLanguage = 'zh' | 'en'
type WorkpaperValue = NonNullable<WorkpaperTable['rows']>[number][string]

export function paperText(value: WorkpaperValue | LocalizedText | undefined, language: WorkingLanguage): string {
  if (value == null) return '—'
  if (typeof value === 'object') return value[language]
  if (typeof value === 'boolean') return language === 'zh' ? (value ? '是' : '否') : value ? 'Yes' : 'No'
  return String(value)
}

function PaperTable({
  table,
  language,
  currency,
}: {
  table: WorkpaperTable
  language: WorkingLanguage
  currency: string
}) {
  return (
    <div className="max-w-full overflow-x-auto rounded-md border">
      <table className="w-full border-collapse text-left text-sm">
        <thead className="bg-muted">
          <tr>
            {table.columns.map((col) => (
              <th
                key={col.key}
                className="border-b px-3 py-2 font-medium"
                style={{ minWidth: Math.min((col.width ?? 20) * 6, 300) }}
              >
                {paperText(col.label, language)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows?.map((row) => (
            <tr key={JSON.stringify(row)} className="border-b last:border-0">
              {table.columns.map((col) => {
                const value = row[col.key]
                const displayed =
                  typeof value === 'number' && col.format === 'money'
                    ? formatMoney(value, currency)
                    : typeof value === 'number' && col.format === 'percent'
                      ? formatPct(value)
                      : paperText(value, language)
                return (
                  <td key={col.key} className="max-w-[420px] whitespace-pre-wrap break-words px-3 py-2 align-top">
                    {displayed}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PaperSection({
  part,
  language,
  currency,
}: {
  part: WorkpaperSection
  language: WorkingLanguage
  currency: string
}) {
  const { t } = useI18n()
  return (
    <section className="flex min-w-0 flex-col gap-3" data-section={part.id}>
      <h3 className="m-0 rounded-md bg-muted px-3 py-2 font-semibold">{paperText(part.title, language)}</h3>
      {part.generated_language && part.generated_language !== language ? (
        <p className="m-0 text-sm text-muted-foreground">{t.workingLanguage.generatedIn[part.generated_language]}</p>
      ) : null}
      {part.paragraphs?.map((text) => (
        <p key={JSON.stringify(text)} className="m-0 whitespace-pre-wrap text-sm leading-relaxed">
          {paperText(text, language)}
        </p>
      ))}
      {part.tables?.map((table) => (
        <PaperTable key={table.id} table={table} language={language} currency={currency} />
      ))}
      {part.children?.map((child) => (
        <PaperSection key={child.id} part={child} language={language} currency={currency} />
      ))}
    </section>
  )
}

export function WorkpaperPreview({ paper, language }: { paper: Workpaper; language: WorkingLanguage }) {
  const { t } = useI18n()
  const content = paper.content
  if (!content) return <p>{t.workpapers.noSnapshot}</p>
  const header = content.header
  return (
    <article className="flex min-w-0 flex-col gap-5" data-testid="workpaper-preview" lang={language}>
      <header className="grid gap-2 rounded-lg border p-4 text-sm sm:grid-cols-2">
        <h2 className="col-span-full m-0 font-semibold text-lg">
          {paper.ref} · {paperText(header.title, language)}
        </h2>
        <p className="m-0">{header.client_name}</p>
        <p className="m-0">
          {header.period_start} — {header.period_end} · {header.currency}
        </p>
        <p className="m-0">
          {t.workpapers.preparer}: {paperText(header.preparer, language)} · {header.completed_at ?? '—'}
        </p>
        <p className="m-0">
          {t.workpapers.reviewer}: {header.reviewer ?? '—'} · {header.reviewed_at ?? '—'} ·{' '}
          {header.review_scope ? t.workpapers.scopes[header.review_scope] : '—'}
        </p>
      </header>
      {content.sections.map((part) => (
        <PaperSection key={part.id} part={part} language={language} currency={header.currency ?? 'AUD'} />
      ))}
    </article>
  )
}
