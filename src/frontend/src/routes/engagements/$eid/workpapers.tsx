import { useMutation, useQuery } from '@tanstack/react-query'
import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import { workpapersGetWorkpaperOptions, workpapersListWorkpapersOptions } from '@/client/@tanstack/react-query.gen'
import {
  workpapersConcludeWorkpaper,
  workpapersDownloadArchive,
  workpapersDownloadWorkpaper,
  workpapersNoteWorkpaper,
  workpapersSignWorkpaper,
} from '@/client/sdk.gen'
import type { ReviewScope, WorkpaperDetail, WorkpaperIndexEntry } from '@/client/types.gen'
import { PageHeader, StatusPill } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { paperText, WorkpaperPreview } from '@/components/workpaper-preview'
import { errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useOverview } from '@/lib/use-engagement'
import { currentLanguage } from '@/lib/working-language'
import { saveDownload } from '@/lib/workpapers'

export const Route = createFileRoute('/engagements/$eid/workpapers')({
  validateSearch: (search: Record<string, unknown>): { ref?: string; version?: number } => ({
    ref: typeof search.ref === 'string' ? search.ref : undefined,
    version:
      Number.isInteger(Number(search.version)) && Number(search.version) > 0 ? Number(search.version) : undefined,
  }),
  component: Workpapers,
})

function Workpapers() {
  const { eid } = Route.useParams()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const { t } = useI18n()
  const overview = useOverview(eid)
  const language = currentLanguage(overview)
  const list = useQuery({
    ...workpapersListWorkpapersOptions({ path: { eid } }),
    refetchInterval: overview?.running ? 2000 : false,
  })
  const selected =
    list.data?.find((row) => row.ref === search.ref) ?? list.data?.find((row) => row.availability === 'generated')
  const detail = useQuery({
    ...workpapersGetWorkpaperOptions({ path: { eid, ref: selected?.ref ?? '00' }, query: { version: search.version } }),
    enabled: selected?.availability === 'generated',
  })
  const download = useMutation({
    mutationFn: async (kind: 'file' | 'archive') => {
      const result =
        kind === 'archive'
          ? await workpapersDownloadArchive({ path: { eid }, throwOnError: true })
          : await workpapersDownloadWorkpaper({
              path: { eid, ref: selected?.ref ?? '00' },
              query: { version: detail.data?.workpaper.version },
              throwOnError: true,
            })
      saveDownload(result.data, result.response, kind === 'archive' ? 'workpapers.zip' : 'workpaper.xlsx')
    },
  })
  const changed = async () => {
    await list.refetch()
    await detail.refetch()
  }
  return (
    <>
      <PageHeader
        title={t.workpapers.title}
        actions={
          <Button
            variant="outline"
            disabled={!list.data?.some((p) => p.availability === 'generated') || download.isPending}
            onClick={() => download.mutate('archive')}
          >
            {t.workpapers.archive}
          </Button>
        }
      />
      <main className="mx-auto flex w-full max-w-[1500px] min-w-0 flex-col gap-6 px-4 py-6 sm:px-7">
        {download.error ? (
          <p role="alert" className="text-destructive">
            {errorText(download.error, t)}
          </p>
        ) : null}
        <QueryState isPending={list.isPending} error={list.error}>
          {list.data ? (
            <div className="overflow-x-auto rounded-lg border" data-testid="workpaper-catalog">
              <table className="w-full text-left text-sm">
                <thead className="bg-muted">
                  <tr>
                    {[
                      t.workpapers.index,
                      t.workpapers.paper,
                      t.workpapers.procedure,
                      t.workpapers.preparer,
                      t.workpapers.completed,
                      t.workpapers.status,
                      t.workpapers.reviewer,
                      t.workpapers.reviewed,
                      t.workpapers.scope,
                      t.workpapers.missing,
                    ].map((text) => (
                      <th key={text} className="whitespace-nowrap px-3 py-2">
                        {text}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {list.data.map((row) => (
                    <CatalogRow
                      key={row.ref}
                      eid={eid}
                      row={row}
                      selected={selected?.ref === row.ref}
                      language={language}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </QueryState>
        {selected?.availability === 'generated' ? (
          <QueryState isPending={detail.isPending} error={detail.error}>
            {detail.data ? (
              <div className="flex min-w-0 flex-col gap-5 rounded-xl border bg-card p-4 sm:p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <label className="flex items-center gap-2 text-sm">
                    {t.workpapers.version}
                    <select
                      aria-label={t.workpapers.version}
                      className="rounded border bg-card px-2 py-1"
                      value={detail.data.workpaper.version}
                      onChange={(event) =>
                        navigate({ search: { ref: selected.ref, version: Number(event.target.value) } })
                      }
                    >
                      {detail.data.versions.map((version) => (
                        <option key={version} value={version}>
                          {version === Math.max(...detail.data.versions)
                            ? t.workpapers.currentVersion(version)
                            : t.workpapers.historicalVersion(version)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button
                    variant="outline"
                    disabled={download.isPending || !detail.data.workpaper.content}
                    onClick={() => download.mutate('file')}
                  >
                    {t.workpapers.file}
                  </Button>
                </div>
                <p className="m-0 text-muted-foreground text-xs">{t.workpapers.versionHint}</p>
                {detail.data.workpaper.missing?.length ? (
                  <p className="text-sm text-muted-foreground">
                    {t.workpapers.missing}:{' '}
                    {detail.data.workpaper.missing
                      .map((id) =>
                        paperText(detail.data?.required.find((item) => item.id === id)?.name ?? id, language),
                      )
                      .join(', ')}
                  </p>
                ) : null}
                <WorkpaperPreview paper={detail.data.workpaper} language={language} />
                <PaperActions
                  key={`${selected.ref}-${detail.data.workpaper.version}`}
                  eid={eid}
                  detail={detail.data}
                  language={language}
                  disabled={Boolean(
                    !overview ||
                      overview.engagement.locked ||
                      overview?.running ||
                      detail.data.workpaper.version !== Math.max(...detail.data.versions),
                  )}
                  onChanged={changed}
                />
              </div>
            ) : null}
          </QueryState>
        ) : null}
      </main>
    </>
  )
}

function CatalogRow({
  eid,
  row,
  selected,
  language,
}: {
  eid: string
  row: WorkpaperIndexEntry
  selected: boolean
  language: 'zh' | 'en'
}) {
  const { t } = useI18n()
  const available = row.availability === 'generated'
  return (
    <tr className={`${selected ? 'bg-accent/30' : ''} ${available ? '' : 'text-muted-foreground'} border-t`}>
      <td className="px-3 py-2 font-mono">
        {available ? (
          <Link to="/engagements/$eid/workpapers" params={{ eid }} search={{ ref: row.ref }}>
            {row.ref}
          </Link>
        ) : (
          row.ref
        )}
      </td>
      <td className="px-3 py-2">{paperText(row.title, language)}</td>
      <td className="px-3 py-2">{row.procedure_id ?? '—'}</td>
      <td className="px-3 py-2">{paperText(row.header?.preparer, language)}</td>
      <td className="whitespace-nowrap px-3 py-2">{row.header?.completed_at?.slice(0, 10) ?? '—'}</td>
      <td className="px-3 py-2">
        <StatusPill tone={available ? 'neutral' : 'warn'}>
          {available && row.status ? t.workpapers.states[row.status] : t.workpapers.availability[row.availability]}
        </StatusPill>
        {available && row.unsigned ? <span className="block text-xs">{t.workpapers.unsigned}</span> : null}
        {row.pending_tasks ? <span className="block text-xs">{t.workpapers.pending(row.pending_tasks)}</span> : null}
      </td>
      <td className="px-3 py-2">{row.header?.reviewer ?? '—'}</td>
      <td className="whitespace-nowrap px-3 py-2">{row.header?.reviewed_at?.slice(0, 10) ?? '—'}</td>
      <td className="px-3 py-2">{row.header?.review_scope ? t.workpapers.scopes[row.header.review_scope] : '—'}</td>
      <td className="px-3 py-2">{available ? (row.missing?.length ?? 0) : '—'}</td>
    </tr>
  )
}

type Action = 'signoff' | 'conclusion' | 'note'
function PaperActions({
  eid,
  detail,
  language,
  disabled,
  onChanged,
}: {
  eid: string
  detail: WorkpaperDetail
  language: 'zh' | 'en'
  disabled: boolean
  onChanged: () => Promise<void>
}) {
  const { t } = useI18n()
  const [action, setAction] = useState<Action>('signoff')
  const [scope, setScope] = useState<ReviewScope>('full')
  const [text, setText] = useState('')
  const [item, setItem] = useState('')
  const path = { eid, ref: detail.workpaper.ref }
  const submit = useMutation({
    mutationFn: async () => {
      if (action === 'signoff')
        await workpapersSignWorkpaper({ path, body: { scope, opinion: text }, throwOnError: true })
      else if (action === 'conclusion') await workpapersConcludeWorkpaper({ path, body: { text }, throwOnError: true })
      else await workpapersNoteWorkpaper({ path, body: { text, item: item || null }, throwOnError: true })
      await onChanged()
    },
  })
  return (
    <form
      className="flex flex-col gap-3 border-t pt-4"
      onSubmit={(event) => {
        event.preventDefault()
        submit.mutate()
      }}
    >
      <fieldset disabled={disabled || submit.isPending} className="flex min-w-0 flex-col gap-3">
        <legend className="mb-3 font-medium">{t.workpapers.actions}</legend>
        <label className="flex flex-wrap items-center gap-3 text-sm">
          {t.workpapers.action}
          <select
            aria-label={t.workpapers.action}
            className="rounded border bg-card p-2"
            value={action}
            onChange={(event) => {
              setAction(event.target.value as Action)
              setText('')
            }}
          >
            {(['signoff', 'conclusion', 'note'] as const).map((key) => (
              <option key={key} value={key}>
                {t.workpapers.actionNames[key]}
              </option>
            ))}
          </select>
        </label>
        {action === 'signoff' ? (
          <label className="flex items-center gap-3 text-sm">
            {t.workpapers.scope}
            <select
              aria-label={t.workpapers.scope}
              className="rounded border bg-card p-2"
              value={scope}
              onChange={(event) => setScope(event.target.value as ReviewScope)}
            >
              {(['full', 'sample', 'conclusion_only'] as const).map((key) => (
                <option key={key} value={key}>
                  {t.workpapers.scopes[key]}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {action === 'note' ? (
          <label className="flex flex-wrap items-center gap-3 text-sm">
            {t.workpapers.supplement}
            <select
              aria-label={t.workpapers.supplement}
              className="max-w-full rounded border bg-card p-2"
              value={item}
              onChange={(event) => setItem(event.target.value)}
            >
              <option value="">{t.workpapers.generalNote}</option>
              {detail.required
                .filter((row) => row.supplementable)
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {paperText(row.name, language)}
                  </option>
                ))}
            </select>
          </label>
        ) : null}
        <label htmlFor="workpaper-edit-text" className="flex flex-col gap-2 text-sm">
          {action === 'signoff' ? t.workpapers.optionalOpinion : t.workpapers.text}
          <Textarea
            id="workpaper-edit-text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            required={action !== 'signoff'}
            rows={3}
          />
        </label>
        <Button className="self-start" type="submit">
          {t.workpapers.save}
        </Button>
      </fieldset>
      {disabled ? <p className="m-0 text-sm text-muted-foreground">{t.workpapers.readOnly}</p> : null}
      {submit.error ? (
        <p role="alert" className="m-0 text-destructive text-sm">
          {errorText(submit.error, t)}
        </p>
      ) : null}
    </form>
  )
}
