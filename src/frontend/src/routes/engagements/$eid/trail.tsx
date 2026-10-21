import { useQuery } from '@tanstack/react-query'
import { createFileRoute } from '@tanstack/react-router'
import { cn } from 'cn'
import { useState } from 'react'
import { reportsListTrailOptions } from '@/client/@tanstack/react-query.gen'
import type { TrailKind } from '@/client/types.gen'
import { PageHeader, StatusPill, type Tone } from '@/components/chrome'
import { QueryState } from '@/components/query-state'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/lib/i18n'

// 后端加上 deferral, conversation 两类留痕后, 这里按字符串筛选即可
const KINDS = [
  'all',
  'data',
  'decision',
  'llm_call',
  'transition',
  'external_call',
  'deferral',
  'conversation',
] as const
const TONES: Record<string, Tone> = {
  decision: 'warn',
  data: 'neutral',
  llm_call: 'accent',
  transition: 'neutral',
  external_call: 'neutral',
  deferral: 'neutral',
  conversation: 'accent',
}

export const Route = createFileRoute('/engagements/$eid/trail')({ component: Trail })

function Trail() {
  const { eid } = Route.useParams()
  const { t, formatDateTime } = useI18n()
  const [kind, setKind] = useState<(typeof KINDS)[number]>('all')
  const [offset, setOffset] = useState(0)
  const limit = 100
  const trail = useQuery(
    reportsListTrailOptions({
      path: { eid },
      query: { kind: kind === 'all' ? undefined : [kind as TrailKind], limit, offset },
    }),
  )
  const rows = trail.data ?? []
  const label = (item: string) => (item === 'conversation' ? t.nav.concierge : (t.trailKinds[item] ?? item))
  // 操作者: 审计师显示角色, 智能体显示编号与名称, 模型调用显示模型名
  const actor = (entry: { actor: string; model?: string | null }) =>
    entry.model ??
    (entry.actor.startsWith('USR-') ? t.trailPage.auditor : `${entry.actor} ${t.agents[entry.actor] ?? ''}`.trim())
  return (
    <>
      <PageHeader title={t.trailPage.title} />
      <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-4 px-4 py-5 sm:px-7">
        <fieldset
          aria-label={t.trailPage.title}
          className="inline-flex flex-wrap self-start rounded-[10px] bg-[#e9ecf1] p-[3px]"
        >
          {KINDS.map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={kind === item}
              onClick={() => {
                setKind(item)
                setOffset(0)
              }}
              className={cn(
                'min-h-8 rounded-lg px-3 text-[13px]',
                kind === item ? 'bg-card font-semibold shadow-xs' : 'font-medium',
              )}
            >
              {label(item)}
            </button>
          ))}
        </fieldset>
        <QueryState isPending={trail.isPending} error={trail.error}>
          <div className="flex flex-col overflow-hidden rounded-xl border bg-card">
            {rows.length ? (
              rows.map((entry) => (
                <details
                  key={entry.id ?? `${entry.created_at}:${entry.summary}`}
                  className="group border-line-soft border-b last:border-b-0"
                >
                  <summary className="flex cursor-pointer flex-wrap items-center gap-3 px-4 py-2.5 text-[14px] hover:bg-[#fafbfc]">
                    <span className="w-40 shrink-0 text-[12.5px] text-muted-foreground">
                      {formatDateTime(entry.created_at)}
                    </span>
                    <StatusPill tone={TONES[entry.kind] ?? 'neutral'}>{label(entry.kind)}</StatusPill>
                    <span className="min-w-0 flex-1 truncate" lang="en">
                      {entry.payload?.action === 'currency'
                        ? t.currency.change(String(entry.payload.from), String(entry.payload.to))
                        : entry.summary}
                    </span>
                    <span className="text-[12.5px] text-muted-foreground">{actor(entry)}</span>
                  </summary>
                  <pre className="m-0 overflow-x-auto bg-muted px-4 py-3 font-mono text-xs">
                    {JSON.stringify(entry.payload, null, 2)}
                  </pre>
                </details>
              ))
            ) : (
              <span className="px-4 py-4 text-[13px] text-muted-foreground">{t.trailPage.none}</span>
            )}
          </div>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              {t.trailPage.previous}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={rows.length < limit}
              onClick={() => setOffset(offset + limit)}
            >
              {t.trailPage.next}
            </Button>
          </div>
        </QueryState>
      </div>
    </>
  )
}
