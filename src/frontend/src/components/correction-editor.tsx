import { useId } from 'react'
import type { CorrectionEntry } from '@/client/types.gen'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/lib/i18n'
import { moneyToCents } from '@/lib/money'

export function CorrectionEditor({
  entries,
  accounts,
  onChange,
}: {
  entries: CorrectionEntry[]
  accounts: { code: string; name?: string | null }[]
  onChange: (entries: CorrectionEntry[]) => void
}) {
  const id = useId()
  const { t } = useI18n()
  const labels = t.correctionEntry
  const set = (index: number, patch: Partial<CorrectionEntry>) =>
    onChange(entries.map((e, i) => (i === index ? { ...e, ...patch } : e)))
  return (
    <fieldset className="flex flex-col gap-3 rounded-lg border p-3" data-testid="correction-editor">
      <legend className="px-1 font-semibold">{labels.edit}</legend>
      {entries.map((entry, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: All row values are controlled; rows have no independent state.
        <div key={`line-${index}`} className="grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-2">
          <label className="text-sm">
            {labels.account}
            <select
              aria-label={`${labels.account} ${index + 1}`}
              className="w-full rounded border bg-card p-2"
              value={entry.account_code}
              onChange={(event) => {
                const account = accounts.find((a) => a.code === event.target.value)
                if (account) set(index, { account_code: account.code, account_name: account.name ?? account.code })
              }}
            >
              {!accounts.some((a) => a.code === entry.account_code) ? (
                <option value={entry.account_code}>
                  {entry.account_name} ({entry.account_code})
                </option>
              ) : null}
              {accounts.map((a) => (
                <option key={a.code} value={a.code}>
                  {a.name} ({a.code})
                </option>
              ))}
            </select>
          </label>
          <label htmlFor={`${id}-debit-${index}`} className="text-sm">
            {labels.debit}
            <Input
              id={`${id}-debit-${index}`}
              aria-label={`${labels.debit} ${index + 1}`}
              type="number"
              min="0"
              step="0.01"
              value={(entry.debit_cents ?? 0) / 100}
              onChange={(event) => set(index, { debit_cents: moneyToCents(event.target.value) ?? 0 })}
            />
          </label>
          <label htmlFor={`${id}-credit-${index}`} className="text-sm">
            {labels.credit}
            <Input
              id={`${id}-credit-${index}`}
              aria-label={`${labels.credit} ${index + 1}`}
              type="number"
              min="0"
              step="0.01"
              value={(entry.credit_cents ?? 0) / 100}
              onChange={(event) => set(index, { credit_cents: moneyToCents(event.target.value) ?? 0 })}
            />
          </label>
          <label htmlFor={`${id}-description-${index}`} className="text-sm">
            {labels.description}
            <Input
              id={`${id}-description-${index}`}
              aria-label={`${labels.description} ${index + 1}`}
              value={entry.description}
              onChange={(event) => set(index, { description: event.target.value })}
            />
          </label>
          <Button
            type="button"
            variant="outline"
            className="self-end"
            onClick={() => onChange(entries.filter((_, i) => i !== index))}
          >
            {labels.remove}
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        className="self-start"
        disabled={!accounts.length}
        onClick={() => {
          const first = accounts[0]
          if (first)
            onChange([
              ...entries,
              {
                account_code: first.code,
                account_name: first.name ?? first.code,
                debit_cents: 0,
                credit_cents: 0,
                description: '',
              },
            ])
        }}
      >
        {labels.add}
      </Button>
    </fieldset>
  )
}
