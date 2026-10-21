import { useQuery } from '@tanstack/react-query'
import { cn } from 'cn'
import { FileText } from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  engagementsGetClientProfileOptions,
  reportsGetTrailOptions,
  standardsGetStandardOptions,
} from '@/client/@tanstack/react-query.gen'
import { evidenceGetEvidence, evidenceGetRecord } from '@/client/sdk.gen'
import type { EvidencePointer } from '@/client/types.gen'
import { StatusPill } from '@/components/chrome'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { formatAbn } from '@/lib/abn'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { formatMoney } from '@/lib/money'
import { humanize, pointerKey } from '@/lib/presentation'
import { type ProfileField, type ProfilePair, type ProfileWords, profileEvidence } from '@/lib/profile-evidence'
import { useMoney } from '@/lib/use-money'
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
        <EvidenceLink key={pointerKey(pointer)} eid={eid} pointer={pointer} />
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
  if (pointer.source_type === 'client_profile') return <ProfileBody eid={eid} pointer={pointer} />
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

// 客户画像的一个字段 (需求 R302): 本项目采用版本中该字段的内容, 注明版本与管理层申报
function ProfileBody({ eid, pointer }: { eid: string; pointer: EvidencePointer }) {
  const { currency } = useMoney()
  const { t } = useI18n()
  const query = useQuery(engagementsGetClientProfileOptions({ path: { eid } }))
  if (query.isPending) return <p className="text-muted-foreground text-sm">{t.app.loading}</p>
  if (query.error) return <p className="text-destructive text-sm">{apiMessage(query.error)}</p>
  // 字段的显示沿用客户资料页 (主体类型, ABN) 与新建客户表单 (报告框架) 的标签与格式
  const frameworks: Record<string, string> = t.newClient.frameworks
  const words: ProfileWords = {
    currency,
    yes: t.clientPage.yes,
    no: t.clientPage.no,
    display: {
      entity_type: (value) => t.clientPage.entityTypes[value] ?? value,
      applicable_framework: (value) => frameworks[value] ?? value,
      abn: formatAbn,
    },
  }
  const shown = profileEvidence(query.data, pointer, words)
  if (shown.kind !== 'shown') return <p className="text-muted-foreground text-sm">{t.evidence.profile[shown.kind]}</p>
  return (
    <div className="space-y-3 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <StatusPill tone="warn">{t.evidence.profile.declared}</StatusPill>
        <span className="text-muted-foreground">{t.evidence.profile.version(shown.version)}</span>
      </p>
      <ProfileContent field={shown.field} />
      <p className="text-[12.5px] text-muted-foreground">{t.evidence.profile.note}</p>
    </div>
  )
}

function ProfileContent({ field }: { field: ProfileField }) {
  const { t } = useI18n()
  if (field.kind === 'undeclared') return <p className="text-muted-foreground">{t.newClient.notDeclared}</p>
  if (field.kind === 'text') return <p className="break-all font-mono">{field.text}</p>
  if (field.kind === 'pairs') return <PairList pairs={field.pairs} />
  return (
    <ol className="flex list-none flex-col gap-2 p-0">
      {field.rows.map((row, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: 申报的列表没有 ID, 只读不重排, 行号即位置
        <li key={index} className="rounded-md border px-3 py-2">
          {'text' in row ? <span className="break-all font-mono">{row.text}</span> : <PairList pairs={row.pairs} />}
        </li>
      ))}
    </ol>
  )
}

function PairList({ pairs }: { pairs: ProfilePair[] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
      {pairs.map(({ key, value }) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{key}</dt>
          <dd className="break-all font-mono">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function RecordTable({ row, highlight }: { row: Record<string, unknown>; highlight?: string }) {
  const { currency } = useMoney()
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-sm">
      {Object.entries(row).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{humanize(key)}</dt>
          <dd className="break-all font-mono">
            <span className={cn(highlight === key && 'rounded bg-mark px-1.5 py-0.5')}>
              {formatCell(key, value, currency)}
            </span>
          </dd>
        </div>
      ))}
    </dl>
  )
}

function formatCell(key: string, value: unknown, currency: string): string {
  if (value == null) return '—'
  if (key.endsWith('_cents') && typeof value === 'number') return formatMoney(value, currency)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
