import { useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { cn } from 'cn'
import { ChevronLeft, ChevronRight, Download, Minus, Plus } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import {
  evidenceListConfirmationsOptions,
  evidenceListContractsOptions,
  evidenceListDocumentsOptions,
  evidenceListRegistryExtractsOptions,
} from '@/client/@tanstack/react-query.gen'
import { evidenceGetEvidence } from '@/client/sdk.gen'
import type {
  Confirmation,
  Contract,
  DocumentRef,
  EvidencePointer,
  RegistryExtractView,
  Reliability,
} from '@/client/types.gen'
import { PdfViewer } from '@/components/pdf-viewer'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useActorName } from '@/lib/actor'
import { useDocumentDetails, useDocumentHistory, useEditExtraction } from '@/lib/data-api'
import { type DetailsForm, detailsBody, detailsErrorText, detailsForm, historyFields } from '@/lib/document-details'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { audToCents, formatAud } from '@/lib/money'
import { reasonText } from '@/lib/reason'
import type { Messages } from '@/locales/en'

export const Route = createFileRoute('/engagements/$eid/data/documents/$docId')({ component: DocumentView })

type EditKind = 'text' | 'long' | 'amount' | 'date' | 'choice'
type Choice = { value: string; label: string }

// 一项抽取结果可改的一个部分: 字段, 输入方式与当前值; choice 时从 choices 中选
type EditPart = { field: string; label: string; kind: EditKind; raw: string; choices?: Choice[] }

type Item = {
  id: string
  group: 'fact' | 'term' | 'officer' | 'holder'
  label: string
  value: string
  page: number
  candidates: string[]
  // 只读的项 (ASIC 摘录的股东) 没有 edit, 点选只跳页
  edit?: {
    target: 'contracts' | 'confirmations' | 'registry-extracts'
    id: string
    field: string
    kind: EditKind
    raw: string
    choices?: Choice[]
    label?: string // 有 more 时这一部分在对话框里的名称
    more?: EditPart[] // 同一项另可改的部分 (高管的职务与任职日)
  }
}

const AMOUNT = new Intl.NumberFormat('en-AU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function pageOf(evidence: EvidencePointer[] | undefined, field: string): number {
  return evidence?.find((pointer) => pointer.locator === field)?.page ?? 1
}

// 同一个值在原文里可能的写法, 依次尝试
function dateForms(iso: string): string[] {
  const [y, m, d] = iso.split('-').map(Number)
  const date = new Date(Date.UTC(y, m - 1, d))
  const long = date.toLocaleDateString('en-AU', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' })
  return [long, iso, `${String(d).padStart(2, '0')}/${String(m).padStart(2, '0')}/${y}`]
}

function abnForm(abn: string): string {
  const digits = abn.replace(/\D/g, '')
  return digits.length === 11
    ? `${digits.slice(0, 2)} ${digits.slice(2, 5)} ${digits.slice(5, 8)} ${digits.slice(8)}`
    : abn
}

function contractItems(contract: Contract, t: Messages, formatDate: (iso: string) => string): Item[] {
  const base = { target: 'contracts' as const, id: contract.id }
  const facts: Item[] = [
    {
      id: 'counterparty',
      group: 'fact',
      label: t.docView.fields.counterparty,
      value: contract.counterparty,
      page: pageOf(contract.evidence, 'counterparty'),
      candidates: [contract.counterparty],
      edit: { ...base, field: 'counterparty', kind: 'text', raw: contract.counterparty },
    },
  ]
  if (contract.counterparty_abn) {
    facts.push({
      id: 'counterparty_abn',
      group: 'fact',
      label: t.docView.fields.counterparty_abn,
      value: abnForm(contract.counterparty_abn),
      page: pageOf(contract.evidence, 'counterparty_abn'),
      candidates: [abnForm(contract.counterparty_abn), contract.counterparty_abn],
      edit: { ...base, field: 'counterparty_abn', kind: 'text', raw: contract.counterparty_abn },
    })
  }
  if (contract.amount_cents != null) {
    const plain = AMOUNT.format(contract.amount_cents / 100)
    facts.push({
      id: 'amount_cents',
      group: 'fact',
      label: t.docView.fields.amount_cents,
      value: formatAud(contract.amount_cents),
      page: pageOf(contract.evidence, 'amount_cents'),
      candidates: [`AUD ${plain}`, plain],
      edit: { ...base, field: 'amount_cents', kind: 'amount', raw: plain },
    })
  }
  if (contract.sign_date) {
    facts.push({
      id: 'sign_date',
      group: 'fact',
      label: t.docView.fields.sign_date,
      value: formatDate(contract.sign_date),
      page: pageOf(contract.evidence, 'sign_date'),
      candidates: dateForms(contract.sign_date),
      edit: { ...base, field: 'sign_date', kind: 'date', raw: contract.sign_date },
    })
  }
  const terms: Item[] = contract.terms.map((term, index) => {
    const clause = term.text.match(/^(\d+(?:\.\d+)*)/)?.[1]
    const type = t.evidencePage.termTypes[term.term_type] ?? term.term_type
    return {
      id: term.id,
      group: 'term',
      label: clause ? `${type} · ${clause}` : type,
      value: term.text,
      page: term.page,
      candidates: [term.text, term.text.slice(0, 80), term.text.slice(0, 40)],
      edit: { ...base, field: `terms[${index}].text`, kind: 'long', raw: term.text },
    }
  })
  return [...facts, ...terms]
}

function confirmationItems(confirmation: Confirmation, t: Messages, formatDate: (iso: string) => string): Item[] {
  const base = { target: 'confirmations' as const, id: confirmation.id }
  const items: Item[] = [
    {
      id: 'confirming_party',
      group: 'fact',
      label: t.docView.fields.confirming_party,
      value: confirmation.confirming_party,
      page: pageOf(confirmation.evidence, 'confirming_party'),
      candidates: [confirmation.confirming_party],
      edit: { ...base, field: 'confirming_party', kind: 'text', raw: confirmation.confirming_party },
    },
  ]
  if (confirmation.balance_date) {
    items.push({
      id: 'balance_date',
      group: 'fact',
      label: t.docView.fields.balance_date,
      value: formatDate(confirmation.balance_date),
      page: pageOf(confirmation.evidence, 'balance_date'),
      candidates: dateForms(confirmation.balance_date),
      edit: { ...base, field: 'balance_date', kind: 'date', raw: confirmation.balance_date },
    })
  }
  if (confirmation.confirmed_balance_cents != null) {
    const plain = AMOUNT.format(confirmation.confirmed_balance_cents / 100)
    items.push({
      id: 'confirmed_balance_cents',
      group: 'fact',
      label: t.docView.fields.confirmed_balance_cents,
      value: formatAud(confirmation.confirmed_balance_cents),
      page: pageOf(confirmation.evidence, 'confirmed_balance_cents'),
      candidates: [`AUD ${plain}`, plain],
      edit: { ...base, field: 'confirmed_balance_cents', kind: 'amount', raw: plain },
    })
  }
  return items
}

function acnForm(acn: string): string {
  return acn.length === 9 ? `${acn.slice(0, 3)} ${acn.slice(3, 6)} ${acn.slice(6)}` : acn
}

// 当前值不在可选值里时 (正常不会有) 仍列出, 保留显示
function choicesOf(values: string[], current: string, label: (value: string) => string): Choice[] {
  const all = values.includes(current) ? values : [current, ...values]
  return all.map((value) => ({ value, label: label(value) }))
}

// ASIC 摘录 (需求 R290): 登记信息, 董事与秘书 (姓名, 职务, 任职日都可改), 股东只读; abn 与注册地址没写时也列出, 可补填
function registryItems(view: RegistryExtractView, t: Messages, formatDate: (iso: string) => string): Item[] {
  const x = view.extraction
  const base = { target: 'registry-extracts' as const, id: view.document_id }
  const fact = (
    field: string,
    value: string,
    candidates: string[],
    raw: string,
    kind: EditKind = 'text',
    choices?: Choice[],
  ): Item => ({
    id: field,
    group: 'fact',
    label: t.docView.fields[field] ?? field,
    value,
    page: pageOf(x.evidence, field),
    candidates,
    edit: { ...base, field, kind, raw, choices },
  })
  // 只读的登记项 (类别, 子类): 点选跳页, 不能改
  const readOnly = (field: string, value: string): Item => ({
    id: field,
    group: 'fact',
    label: t.docView.fields[field] ?? field,
    value,
    page: pageOf(x.evidence, field),
    candidates: [value],
  })
  const missing = t.docView.notStated
  const statusLabel = (value: string) => t.docView.statuses[value] ?? value
  const roleLabel = (value: string) => t.docView.roles[value] ?? value
  const items: Item[] = [
    fact('name', x.name, [x.name], x.name),
    fact('acn', acnForm(x.acn), [acnForm(x.acn), x.acn], x.acn),
    x.abn ? fact('abn', abnForm(x.abn), [abnForm(x.abn), x.abn], x.abn) : fact('abn', missing, [], ''),
    fact(
      'status',
      statusLabel(x.status),
      [x.status],
      x.status,
      'choice',
      choicesOf(['Registered', 'Deregistered'], x.status, statusLabel),
    ),
    { ...fact('type', x.type, [x.type], x.type), label: t.docView.fields.company_type },
    readOnly('company_class', x.company_class),
    readOnly('subclass', x.subclass),
    fact('registered_date', formatDate(x.registered_date), dateForms(x.registered_date), x.registered_date, 'date'),
    x.registered_address
      ? fact('registered_address', x.registered_address, [x.registered_address], x.registered_address)
      : fact('registered_address', missing, [], ''),
  ]
  const officers: Item[] = x.officeholders.map((person, index) => ({
    id: `officeholders-${index}`,
    group: 'officer',
    label: `${roleLabel(person.role)} · ${t.docView.appointed(formatDate(person.appointed))}`,
    value: person.name,
    page: pageOf(x.evidence, 'officeholders'),
    candidates: [person.name],
    edit: {
      ...base,
      field: `officeholders[${index}].name`,
      kind: 'text',
      raw: person.name,
      label: t.docView.officerParts.name,
      more: [
        {
          field: `officeholders[${index}].role`,
          label: t.docView.officerParts.role,
          kind: 'choice',
          raw: person.role,
          choices: choicesOf(['director', 'secretary'], person.role, roleLabel),
        },
        {
          field: `officeholders[${index}].appointed`,
          label: t.docView.officerParts.appointed,
          kind: 'date',
          raw: person.appointed,
        },
      ],
    },
  }))
  const holders: Item[] = x.shareholders.map((holder, index) => ({
    id: `shareholders-${index}`,
    group: 'holder',
    label: t.docView.shareholderKinds[holder.kind] ?? holder.kind,
    value: `${holder.name} — ${t.docView.holding(
      holder.shares.toLocaleString('en-AU'),
      t.docView.shareClasses[holder.share_class] ?? holder.share_class,
      `${Math.round(holder.pct * 10000) / 100}%`,
    )}`,
    page: pageOf(x.evidence, 'shareholders'),
    candidates: [holder.name],
  }))
  return [...items, ...officers, ...holders]
}

function DocumentView() {
  const { eid, docId } = Route.useParams()
  const { t, formatDate } = useI18n()
  const documents = useQuery(evidenceListDocumentsOptions({ path: { eid } }))
  const contracts = useQuery(evidenceListContractsOptions({ path: { eid } }))
  const confirmations = useQuery(evidenceListConfirmationsOptions({ path: { eid } }))
  const extracts = useQuery(evidenceListRegistryExtractsOptions({ path: { eid } }))
  const doc = documents.data?.find((item) => item.id === docId)
  const mediaType = doc?.media_type ?? 'application/pdf'
  const isPdf = mediaType === 'application/pdf'
  const isImage = mediaType.startsWith('image/')

  const [file, setFile] = useState<Blob | null>(null)
  const [bytes, setBytes] = useState<ArrayBuffer | null>(null)
  const [fileUrl, setFileUrl] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [pages, setPages] = useState(doc?.page_count ?? 1)
  const [page, setPage] = useState<number | null>(null)
  const [zoom, setZoom] = useState(1)
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState<Item | null>(null)

  useEffect(() => {
    let cancelled = false
    let objectUrl: string | null = null
    evidenceGetEvidence({ path: { eid, document_id: docId }, throwOnError: true })
      .then(async ({ data }) => {
        if (cancelled) return
        const blob = data as Blob
        objectUrl = URL.createObjectURL(blob)
        setFile(blob)
        setFileUrl(objectUrl)
        setBytes(await blob.arrayBuffer())
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught)
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [eid, docId])

  const items = useMemo(() => {
    const contract = contracts.data?.find((item) => item.document_id === docId)
    if (contract) return contractItems(contract, t, formatDate)
    const confirmation = confirmations.data?.find((item) => item.reply_document_id === docId)
    if (confirmation) return confirmationItems(confirmation, t, formatDate)
    const extract = extracts.data?.find((item) => item.document_id === docId)
    return extract ? registryItems(extract, t, formatDate) : []
  }, [contracts.data, confirmations.data, extracts.data, docId, t, formatDate])

  const current = items.find((item) => item.id === selected) ?? null
  const shownPage = page ?? current?.page ?? 1
  const highlight = useMemo(
    () => (current && current.page === shownPage ? current.candidates : null),
    [current, shownPage],
  )
  const facts = items.filter((item) => item.group === 'fact')
  const terms = items.filter((item) => item.group === 'term')
  const officers = items.filter((item) => item.group === 'officer')
  const holders = items.filter((item) => item.group === 'holder')
  const target = items.find((item) => item.edit)?.edit?.target
  const factsTitle =
    target === 'contracts'
      ? t.docView.facts
      : target === 'registry-extracts'
        ? t.docView.registryFacts
        : t.docView.replyFacts

  const pick = (item: Item) => {
    setSelected(item.id)
    setPage(null)
  }

  return (
    <>
      <header className="flex flex-wrap items-end justify-between gap-3 border-b bg-card px-4 py-4 sm:px-7">
        <div className="flex min-w-0 flex-col">
          <span className="text-[12.5px]">
            <Link to="/engagements/$eid/data" params={{ eid }} search={{}}>
              {t.data.title}
            </Link>{' '}
            <span className="text-muted-foreground">/ {t.docView.files}</span>
          </span>
          <h1 className="m-0 truncate font-mono font-semibold text-xl">{doc?.file_name ?? docId}</h1>
          {doc ? (
            <span className="text-[13px] text-muted-foreground">
              {t.docView.meta(
                t.data.docTypes[doc.type] ?? doc.type,
                doc.page_count,
                t.data.reliabilities[doc.reliability] ?? doc.reliability,
                formatDate(doc.uploaded_at),
              )}
            </span>
          ) : null}
        </div>
        {fileUrl && file ? (
          <a
            href={fileUrl}
            download={doc?.file_name ?? docId}
            className="inline-flex min-h-10 items-center gap-2 rounded-[9px] border border-input bg-card px-3.5 text-[13.5px] text-foreground no-underline"
          >
            <Download className="size-4" aria-hidden="true" />
            {t.docView.download}
          </a>
        ) : null}
      </header>

      <div className="flex flex-wrap items-start gap-5 px-4 py-5 sm:px-7">
        <section aria-label={doc?.file_name ?? docId} className="flex min-w-0 flex-[999_1_560px] flex-col gap-3">
          {isPdf ? (
            <div className="flex flex-wrap items-center justify-between gap-2.5 rounded-[10px] border bg-card px-2 py-1.5">
              <div className="flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t.docView.previous}
                  disabled={shownPage <= 1}
                  onClick={() => setPage(shownPage - 1)}
                >
                  <ChevronLeft className="size-4" aria-hidden="true" />
                </Button>
                <span className="min-w-[88px] text-center text-[13.5px]">{t.docView.pageOf(shownPage, pages)}</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t.docView.next}
                  disabled={shownPage >= pages}
                  onClick={() => setPage(shownPage + 1)}
                >
                  <ChevronRight className="size-4" aria-hidden="true" />
                </Button>
              </div>
              <div className="flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t.docView.zoomOut}
                  disabled={zoom <= 0.75}
                  onClick={() => setZoom((value) => Math.max(0.75, value - 0.25))}
                >
                  <Minus className="size-4" aria-hidden="true" />
                </Button>
                <span className="min-w-12 text-center font-mono text-[13px]">{Math.round(zoom * 100)}%</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t.docView.zoomIn}
                  disabled={zoom >= 2}
                  onClick={() => setZoom((value) => Math.min(2, value + 0.25))}
                >
                  <Plus className="size-4" aria-hidden="true" />
                </Button>
              </div>
            </div>
          ) : null}
          <div className="rounded-xl bg-[#e9ecf1] p-6">
            {error ? (
              <p className="text-destructive text-sm">{errorText(error, t)}</p>
            ) : !bytes ? (
              <p className="text-muted-foreground text-sm">{t.evidence.loadingPdf}</p>
            ) : isPdf ? (
              <div className="mx-auto max-w-[680px]">
                <PdfViewer data={bytes} page={shownPage} zoom={zoom} highlight={highlight} onLoaded={setPages} />
              </div>
            ) : isImage && fileUrl ? (
              <img src={fileUrl} alt={doc?.file_name ?? docId} className="mx-auto block max-w-full bg-white" />
            ) : (
              <p className="m-0 text-center text-[#4a5262] text-[14px]">{t.docView.download_only}</p>
            )}
          </div>
        </section>

        <div className="flex min-w-0 flex-[1_1_340px] flex-col gap-5">
          {doc ? <Registration eid={eid} doc={doc} /> : null}
          <aside
            aria-label={t.docView.extracted}
            className="flex min-w-0 flex-col gap-3.5 rounded-[14px] border bg-card px-[18px] pt-[18px] pb-3.5"
          >
            <h2 className="m-0 font-semibold text-base">{t.docView.extracted}</h2>
            {items.length ? (
              <>
                <ItemGroup title={factsTitle} items={facts} selected={selected} onPick={pick} />
                {terms.length ? (
                  <ItemGroup title={t.docView.terms} items={terms} selected={selected} onPick={pick} long />
                ) : null}
                {officers.length ? (
                  <ItemGroup title={t.docView.officeholders} items={officers} selected={selected} onPick={pick} />
                ) : null}
                {holders.length ? (
                  <ItemGroup title={t.docView.shareholders} items={holders} selected={selected} onPick={pick} />
                ) : null}
                <div className="flex justify-end border-line-soft border-t pt-2.5">
                  <Button type="button" variant="outline" disabled={!current?.edit} onClick={() => setEditing(current)}>
                    {t.docView.edit}
                  </Button>
                </div>
              </>
            ) : (
              <p className="m-0 text-[13.5px] text-muted-foreground">
                {contracts.isPending || confirmations.isPending || extracts.isPending ? t.app.loading : t.docView.none}
              </p>
            )}
          </aside>
        </div>
      </div>

      <EditDialog eid={eid} item={editing} onClose={() => setEditing(null)} />
    </>
  )
}

function ItemGroup({
  title,
  items,
  selected,
  onPick,
  long,
}: {
  title: string
  items: Item[]
  selected: string | null
  onPick: (item: Item) => void
  long?: boolean
}) {
  const { t } = useI18n()
  return (
    <div className="flex flex-col gap-1">
      <h3 className="m-0 mb-1 font-semibold text-[12px] text-muted-foreground tracking-wide">{title}</h3>
      {items.map((item) => {
        const on = item.id === selected
        return (
          <button
            key={item.id}
            type="button"
            aria-pressed={on}
            onClick={() => onPick(item)}
            className={cn(
              'flex w-full flex-col gap-0.5 rounded-[9px] border-[1.5px] px-[11px] py-[9px] text-left',
              on ? 'border-primary bg-accent' : 'border-transparent bg-card hover:bg-[#fafbfc]',
            )}
          >
            <span className="flex justify-between gap-2 text-[12.5px] text-muted-foreground">
              <span>{item.label}</span>
              <span>{t.docView.page(item.page)}</span>
            </span>
            <span
              lang={long ? 'en' : undefined}
              className={cn(long ? 'line-clamp-3 text-[#2d3443] text-[13px]' : 'font-medium text-[14px]')}
            >
              {item.value}
            </span>
          </button>
        )
      })}
    </div>
  )
}

const SELECT = 'h-10 rounded-lg border border-input bg-card px-2.5 font-normal text-[14px]'

function EditDialog({ eid, item, onClose }: { eid: string; item: Item | null; onClose: () => void }) {
  const { t } = useI18n()
  const edit = useEditExtraction(eid)
  const [partIndex, setPartIndex] = useState(0)
  const [value, setValue] = useState('')
  const [reason, setReason] = useState('')
  const parts: EditPart[] = item?.edit
    ? [
        {
          field: item.edit.field,
          label: item.edit.label ?? item.label,
          kind: item.edit.kind,
          raw: item.edit.raw,
          choices: item.edit.choices,
        },
        ...(item.edit.more ?? []),
      ]
    : []
  const part = parts[partIndex] ?? parts[0]

  useEffect(() => {
    setPartIndex(0)
    setValue(item?.edit?.raw ?? '')
    setReason('')
  }, [item])

  if (!item?.edit || !part) return null
  const target = item.edit
  const choose = (index: number) => {
    setPartIndex(index)
    setValue(parts[index]?.raw ?? '')
  }
  const parsed = part.kind === 'amount' ? audToCents(value) : value.trim()
  const canSave = parsed !== null && parsed !== '' && value.trim() !== part.raw
  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="text-base">{item.label}</DialogTitle>
        </DialogHeader>
        {parts.length > 1 ? (
          <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
            <label htmlFor="extraction-part">{t.docView.part}</label>
            <select
              id="extraction-part"
              className={SELECT}
              value={partIndex}
              onChange={(event) => choose(Number(event.target.value))}
            >
              {parts.map((option, index) => (
                <option key={option.field} value={index}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="extraction-value">{t.docView.value}</label>
          {part.kind === 'long' ? (
            <Textarea
              id="extraction-value"
              rows={5}
              lang="en"
              className="font-normal"
              value={value}
              onChange={(event) => setValue(event.target.value)}
            />
          ) : part.kind === 'choice' ? (
            <select
              id="extraction-value"
              className={SELECT}
              value={value}
              onChange={(event) => setValue(event.target.value)}
            >
              {(part.choices ?? []).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          ) : (
            <Input
              id="extraction-value"
              type={part.kind === 'date' ? 'date' : 'text'}
              inputMode={part.kind === 'amount' ? 'decimal' : undefined}
              className={cn('h-10 font-normal', part.kind === 'amount' && 'font-mono')}
              value={value}
              onChange={(event) => setValue(event.target.value)}
            />
          )}
        </div>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="extraction-reason">{t.reason.label}</label>
          <Textarea
            id="extraction-reason"
            rows={2}
            className="font-normal"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.docView.cancel}
          </Button>
          <Button
            type="button"
            disabled={!canSave || edit.isPending}
            onClick={() =>
              edit.mutate(
                {
                  target: target.target,
                  id: target.id,
                  body: { field: part.field, value: parsed, reason: reason.trim() },
                },
                {
                  onSuccess: () => {
                    toast.success(t.docView.saved)
                    onClose()
                  },
                  onError: (error) => toast.error(errorText(error, t)),
                },
              )
            }
          >
            {t.docView.save}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

const HELD = new Set(['staged', 'not_imported']) // 还在导入确认单里或未导入的文件: 后端不收补登记

// 登记信息 (后端需求 R90, 页面 R270): 当前的可靠性, 文件日期与关联对象, 补登记对话框, 历史 (后端的文件登记各版)
function Registration({ eid, doc }: { eid: string; doc: DocumentRef }) {
  const { t, formatDate, formatDateTime } = useI18n()
  const actorName = useActorName()
  const history = useDocumentHistory(eid, doc.id).data ?? []
  const [editing, setEditing] = useState(false)
  const held = HELD.has(doc.status)
  const hintId = `details-hint-${doc.id}`
  const related = doc.related_ids ?? []
  return (
    <section
      aria-label={t.docView.details.title}
      className="flex flex-col gap-3 rounded-[14px] border bg-card px-[18px] pt-[18px] pb-3.5"
    >
      <h2 className="m-0 font-semibold text-base">{t.docView.details.title}</h2>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[13.5px]">
        <dt className="text-muted-foreground">{t.docView.details.reliability}</dt>
        <dd className="m-0">{t.data.reliabilities[doc.reliability] ?? doc.reliability}</dd>
        <dt className="text-muted-foreground">{t.docView.details.documentDate}</dt>
        <dd className="m-0">{doc.document_date ? formatDate(doc.document_date) : t.docView.details.noDate}</dd>
        <dt className="text-muted-foreground">{t.docView.details.relatedIds}</dt>
        <dd className="m-0 font-mono text-[13px]">
          {related.length ? related.join(', ') : t.docView.details.noRelated}
        </dd>
      </dl>
      <div className="flex flex-col items-end gap-1 border-line-soft border-t pt-2.5">
        <Button
          type="button"
          variant="outline"
          disabled={held}
          aria-describedby={held ? hintId : undefined}
          onClick={() => setEditing(true)}
        >
          {t.docView.details.edit}
        </Button>
        {held ? (
          <span id={hintId} className="text-[12.5px] text-muted-foreground">
            {t.docView.details.unavailable}
          </span>
        ) : null}
      </div>
      {history.length ? (
        <div className="flex flex-col gap-1.5">
          <h3 className="m-0 font-semibold text-[12px] text-muted-foreground tracking-wide">
            {t.docView.details.history}
          </h3>
          <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
            {[...history].reverse().map((record) => (
              <li key={record.version} className="flex flex-col gap-0.5 text-[13px]">
                <span className="flex flex-wrap justify-between gap-x-2 text-muted-foreground text-[12.5px]">
                  <span className="whitespace-nowrap">
                    {t.docView.details.version(record.version)} · {historyFields(record, t)}
                  </span>
                  <span className="whitespace-nowrap">
                    {[actorName(record.changed_by), formatDateTime(record.changed_at)].filter(Boolean).join(' · ')}
                  </span>
                </span>
                {record.field ? <span className="text-[#2d3443]">{reasonText(record.reason, t)}</span> : null}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {editing ? <DetailsDialog eid={eid} doc={doc} onClose={() => setEditing(false)} /> : null}
    </section>
  )
}

const RELIABILITIES: Reliability[] = ['external_original', 'external_copy', 'internal_original', 'internal_copy']

function DetailsDialog({ eid, doc, onClose }: { eid: string; doc: DocumentRef; onClose: () => void }) {
  const { t } = useI18n()
  const save = useDocumentDetails(eid)
  const [form, setForm] = useState<DetailsForm>(() => detailsForm(doc))
  const [problem, setProblem] = useState<string | null>(null)
  const set = (change: Partial<DetailsForm>) => {
    setForm((current) => ({ ...current, ...change }))
    setProblem(null)
  }
  const submit = () => {
    const body = detailsBody(doc, form)
    if (!body) {
      setProblem(t.docView.details.noChange)
      return
    }
    save.mutate(
      { documentId: doc.id, body },
      {
        onSuccess: () => {
          toast.success(t.docView.details.saved)
          onClose()
        },
        onError: (error) => setProblem(detailsErrorText(error, t)),
      },
    )
  }
  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="text-base">{t.docView.details.dialogTitle(doc.file_name)}</DialogTitle>
        </DialogHeader>
        <label className="flex flex-col gap-1.5 font-semibold text-[13px]">
          {t.docView.details.reliability}
          <select
            className="h-10 rounded-[9px] border border-input bg-card px-2.5 font-normal text-[14px]"
            value={form.reliability}
            onChange={(event) => set({ reliability: event.target.value as Reliability })}
          >
            {RELIABILITIES.map((value) => (
              <option key={value} value={value}>
                {t.data.reliabilities[value] ?? value}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="details-date">{t.docView.details.documentDate}</label>
          <Input
            id="details-date"
            type="date"
            className="h-10 font-normal"
            value={form.documentDate}
            onChange={(event) => set({ documentDate: event.target.value })}
          />
        </div>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="details-related">{t.docView.details.relatedIds}</label>
          <Textarea
            id="details-related"
            rows={2}
            className="font-mono font-normal"
            aria-describedby="details-related-hint"
            value={form.relatedIds}
            onChange={(event) => set({ relatedIds: event.target.value })}
          />
          <span id="details-related-hint" className="font-normal text-[12.5px] text-muted-foreground">
            {t.docView.details.relatedHint}
          </span>
        </div>
        <div className="flex flex-col gap-1.5 font-semibold text-[13px]">
          <label htmlFor="details-reason">{t.reason.label}</label>
          <Textarea
            id="details-reason"
            rows={2}
            className="font-normal"
            value={form.reason}
            onChange={(event) => set({ reason: event.target.value })}
          />
        </div>
        {problem ? (
          <p role="alert" className="m-0 text-[13px] text-destructive">
            {problem}
          </p>
        ) : null}
        <div className="flex justify-end gap-2.5">
          <Button type="button" variant="outline" onClick={onClose}>
            {t.docView.details.cancel}
          </Button>
          <Button type="button" disabled={save.isPending} onClick={submit}>
            {t.docView.details.save}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
