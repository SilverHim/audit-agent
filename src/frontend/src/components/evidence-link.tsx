import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { reportsGetTrailOptions, standardsGetStandardOptions } from '@/client/@tanstack/react-query.gen'
import { evidenceGetEvidence, evidenceGetRecord } from '@/client/sdk.gen'
import type { EvidencePointer } from '@/client/types.gen'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { apiMessage } from '@/lib/errors'

const ROW_SOURCES = [
  'journal_entry',
  'sales_invoice',
  'delivery_note',
  'bank_transaction',
  'bank_account',
  'trial_balance',
] as const

type RowSource = (typeof ROW_SOURCES)[number]

function isRowSource(value: string): value is RowSource {
  return (ROW_SOURCES as readonly string[]).includes(value)
}

export function PointerList({ eid, pointers }: { eid: string; pointers: EvidencePointer[] | undefined }) {
  if (!pointers?.length) return <p className="text-muted-foreground text-sm">No evidence pointers.</p>
  return (
    <ul className="space-y-1">
      {pointers.map((pointer) => (
        <li key={`${pointer.source_type}:${pointer.source_id}:${pointer.locator}:${pointer.page ?? ''}`}>
          <EvidenceLink eid={eid} pointer={pointer} />
        </li>
      ))}
    </ul>
  )
}

export function EvidenceLink({ eid, pointer, label }: { eid: string; pointer: EvidencePointer; label?: string }) {
  const [open, setOpen] = useState(false)
  const text =
    label ??
    `${pointer.locator || pointer.source_type} · ${pointer.source_id}${pointer.page ? ` p.${pointer.page}` : ''}`
  return (
    <>
      <button type="button" className="text-left text-sm underline underline-offset-2" onClick={() => setOpen(true)}>
        {text}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-4xl">
          <DialogHeader>
            <DialogTitle>{text}</DialogTitle>
          </DialogHeader>
          {open ? <EvidenceBody eid={eid} pointer={pointer} /> : null}
        </DialogContent>
      </Dialog>
    </>
  )
}

function EvidenceBody({ eid, pointer }: { eid: string; pointer: EvidencePointer }) {
  if (pointer.source_type === 'document') {
    return <PdfFrame eid={eid} documentId={pointer.source_id} page={pointer.page} />
  }
  if (pointer.source_type === 'standard') return <StandardBody fragmentId={pointer.source_id} />
  if (pointer.source_type === 'trail') return <TrailBody eid={eid} trailId={pointer.source_id} />
  if (isRowSource(pointer.source_type)) {
    return <RecordBody eid={eid} sourceType={pointer.source_type} sourceId={pointer.source_id} />
  }
  return <p className="text-muted-foreground text-sm">This pointer cannot be opened.</p>
}

function PdfFrame({ eid, documentId, page }: { eid: string; documentId: string; page?: number | null }) {
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
  if (!url) return <p className="text-muted-foreground text-sm">Loading PDF…</p>
  return <iframe title={documentId} src={`${url}#page=${page ?? 1}`} className="h-[70vh] w-full rounded-md border" />
}

function RecordBody({ eid, sourceType, sourceId }: { eid: string; sourceType: RowSource; sourceId: string }) {
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
  if (!row) return <p className="text-muted-foreground text-sm">Loading record…</p>
  return <RecordTable row={row} />
}

function StandardBody({ fragmentId }: { fragmentId: string }) {
  const query = useQuery(standardsGetStandardOptions({ path: { fragment_id: fragmentId } }))
  if (query.isPending) return <p className="text-muted-foreground text-sm">Loading standard…</p>
  if (query.error) return <p className="text-destructive text-sm">{apiMessage(query.error)}</p>
  const fragment = query.data
  if (!fragment) return null
  return (
    <div className="space-y-2 text-sm">
      <p className="font-medium">
        {fragment.standard} {fragment.paragraph}
      </p>
      <p className="whitespace-pre-wrap">{fragment.text}</p>
    </div>
  )
}

function TrailBody({ eid, trailId }: { eid: string; trailId: string }) {
  const query = useQuery(reportsGetTrailOptions({ path: { eid, trail_id: trailId } }))
  if (query.isPending) return <p className="text-muted-foreground text-sm">Loading trail…</p>
  if (query.error) return <p className="text-destructive text-sm">{apiMessage(query.error)}</p>
  const entry = query.data
  if (!entry) return null
  return (
    <div className="space-y-2 text-sm">
      <p>
        {entry.kind} · {entry.actor} · {entry.summary}
      </p>
      <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">{JSON.stringify(entry.payload, null, 2)}</pre>
    </div>
  )
}

export function RecordTable({ row }: { row: Record<string, unknown> }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {Object.entries(row).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{key}</dt>
          <dd className="break-all">{formatCell(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function formatCell(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
