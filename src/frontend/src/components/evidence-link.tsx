import { useQuery } from '@tanstack/react-query'
import { cn } from 'cn'
import { FileText } from 'lucide-react'
import { useEffect, useState } from 'react'
import { reportsGetTrailOptions, standardsGetStandardOptions } from '@/client/@tanstack/react-query.gen'
import { evidenceGetEvidence, evidenceGetRecord } from '@/client/sdk.gen'
import type { EvidencePointer } from '@/client/types.gen'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import type { Messages } from '@/locales/en'

// 能点开行明细的行指针来源 (同后端 SourceType); tests/table-kinds.test.ts 核对与接口一致
export const ROW_SOURCES = [
  'journal_entry',
  'sales_invoice',
  'supplier_bill',
  'delivery_note',
  'bank_transaction',
  'bank_account',
  'trial_balance',
] as const

type RowSource = (typeof ROW_SOURCES)[number]

function isRowSource(value: string): value is RowSource {
  return (ROW_SOURCES as readonly string[]).includes(value)
}

// 准则片段编号 AASB15-P38 显示为 "AASB 15 para 38" / "AASB 15 第 38 段"
export function standardLabel(code: string, lang: 'zh' | 'en'): string {
  const match = code.match(/^([A-Z]+)(\d+)-P(\d+)$/)
  if (!match) return code
  const [, body, number, para] = match
  return lang === 'zh' ? `${body} ${number} 第 ${Number(para)} 段` : `${body} ${number} para ${Number(para)}`
}

function humanize(locator: string): string {
  return locator.replace(/_cents$/, '').replaceAll('_', ' ')
}

export function pointerLabel(pointer: EvidencePointer, t: Messages, lang: 'zh' | 'en'): string {
  if (pointer.source_type === 'standard') return standardLabel(pointer.source_id, lang)
  if (pointer.source_type === 'document') {
    const head = pointer.locator && /\d/.test(pointer.locator) ? pointer.locator : pointer.source_id
    return pointer.page ? `${head} · ${t.evidence.page(pointer.page)}` : head
  }
  if (pointer.source_type === 'trail') return t.card.sources
  return pointer.locator ? `${pointer.source_id} · ${humanize(pointer.locator)}` : pointer.source_id
}

export function PointerList({ eid, pointers }: { eid: string; pointers: EvidencePointer[] | undefined }) {
  if (!pointers?.length) return null
  return (
    <div className="flex flex-wrap gap-1.5">
      {pointers.map((pointer) => (
        <EvidenceLink
          key={`${pointer.source_type}:${pointer.source_id}:${pointer.locator}:${pointer.page ?? ''}`}
          eid={eid}
          pointer={pointer}
        />
      ))}
    </div>
  )
}

export function EvidenceLink({
  eid,
  pointer,
  label,
  className,
}: {
  eid: string
  pointer: EvidencePointer
  label?: string
  className?: string
}) {
  const { t, lang } = useI18n()
  const [open, setOpen] = useState(false)
  const text = label ?? pointerLabel(pointer, t, lang)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          'inline-flex min-h-7 items-center gap-1.5 rounded-md border border-[#dce0e6] bg-[#f8f9fb] px-2 py-0.5 font-mono text-[12px] text-accent-foreground hover:bg-accent',
          className,
        )}
      >
        <FileText className="size-3" aria-hidden="true" />
        {text}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-4xl">
          <DialogHeader>
            <DialogTitle className="font-mono text-base">{text}</DialogTitle>
          </DialogHeader>
          {open ? <EvidenceBody eid={eid} pointer={pointer} /> : null}
        </DialogContent>
      </Dialog>
    </>
  )
}

function EvidenceBody({ eid, pointer }: { eid: string; pointer: EvidencePointer }) {
  const { t } = useI18n()
  if (pointer.source_type === 'document') {
    return <PdfFrame eid={eid} documentId={pointer.source_id} page={pointer.page} />
  }
  if (pointer.source_type === 'standard') return <StandardBody fragmentId={pointer.source_id} />
  if (pointer.source_type === 'trail') return <TrailBody eid={eid} trailId={pointer.source_id} />
  if (isRowSource(pointer.source_type)) {
    return (
      <RecordBody eid={eid} sourceType={pointer.source_type} sourceId={pointer.source_id} highlight={pointer.locator} />
    )
  }
  return <p className="text-muted-foreground text-sm">{t.evidence.cannotOpen}</p>
}

function PdfFrame({ eid, documentId, page }: { eid: string; documentId: string; page?: number | null }) {
  const { t } = useI18n()
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let objectUrl: string | null = null
    let cancelled = false
    evidenceGetEvidence({ path: { eid, document_id: documentId }, throwOnError: true })
      .then(({ data }) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(data)
        setUrl(objectUrl)
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught)
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [documentId, eid])

  if (error) return <p className="text-destructive text-sm">{apiMessage(error)}</p>
  if (!url) return <p className="text-muted-foreground text-sm">{t.evidence.loadingPdf}</p>
  return <iframe title={documentId} src={`${url}#page=${page ?? 1}`} className="h-[70vh] w-full rounded-md border" />
}

function RecordBody({
  eid,
  sourceType,
  sourceId,
  highlight,
}: {
  eid: string
  sourceType: RowSource
  sourceId: string
  highlight?: string
}) {
  const { t } = useI18n()
  const [row, setRow] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    let cancelled = false
    evidenceGetRecord({ path: { eid, source_type: sourceType, source_id: sourceId }, throwOnError: true })
      .then(({ data }) => {
        if (!cancelled) setRow(data)
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught)
      })
    return () => {
      cancelled = true
    }
  }, [eid, sourceId, sourceType])

  if (error) return <p className="text-destructive text-sm">{apiMessage(error)}</p>
  if (!row) return <p className="text-muted-foreground text-sm">{t.evidence.loadingRecord}</p>
  return <RecordTable row={row} highlight={highlight} />
}

function StandardBody({ fragmentId }: { fragmentId: string }) {
  const { t } = useI18n()
  const query = useQuery(standardsGetStandardOptions({ path: { fragment_id: fragmentId } }))
  if (query.isPending) return <p className="text-muted-foreground text-sm">{t.app.loading}</p>
  if (query.error) return <p className="text-destructive text-sm">{apiMessage(query.error)}</p>
  const fragment = query.data
  if (!fragment) return null
  return (
    <div className="space-y-2 text-sm">
      <p className="font-medium">
        {fragment.standard} {fragment.paragraph}
      </p>
      <p className="whitespace-pre-wrap font-serif leading-relaxed" lang="en">
        {fragment.text}
      </p>
    </div>
  )
}

function TrailBody({ eid, trailId }: { eid: string; trailId: string }) {
  const { t, formatDateTime } = useI18n()
  const query = useQuery(reportsGetTrailOptions({ path: { eid, trail_id: trailId } }))
  if (query.isPending) return <p className="text-muted-foreground text-sm">{t.app.loading}</p>
  if (query.error) return <p className="text-destructive text-sm">{apiMessage(query.error)}</p>
  const entry = query.data
  if (!entry) return null
  return (
    <div className="space-y-3 text-sm">
      <p>
        <span className="text-muted-foreground">{formatDateTime(entry.created_at)}</span> · {entry.summary}
      </p>
      <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">
        {JSON.stringify(entry.payload, null, 2)}
      </pre>
    </div>
  )
}

export function RecordTable({ row, highlight }: { row: Record<string, unknown>; highlight?: string }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-sm">
      {Object.entries(row).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{humanize(key)}</dt>
          <dd className="break-all font-mono">
            <span className={cn(highlight === key && 'rounded bg-mark px-1.5 py-0.5')}>{formatCell(key, value)}</span>
          </dd>
        </div>
      ))}
    </dl>
  )
}

function formatCell(key: string, value: unknown): string {
  if (value == null) return '—'
  if (key.endsWith('_cents') && typeof value === 'number') {
    return `AUD ${new Intl.NumberFormat('en-AU', { minimumFractionDigits: 2 }).format(value / 100)}`
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
