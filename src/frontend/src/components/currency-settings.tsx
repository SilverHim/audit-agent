import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { toast } from 'sonner'
import { engagementsSetCurrencyMutation } from '@/client/@tanstack/react-query.gen'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { invalidateEngagement } from '@/lib/engagement'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import { useEngagement } from '@/lib/use-engagement'

export function validCurrency(value: string): boolean {
  return /^[A-Z]{3}$/.test(value)
}

export function CurrencyForm({
  currency,
  source,
  disabled,
  onConfirm,
}: {
  currency: string
  source: 'default' | 'xero' | 'auditor'
  disabled: boolean
  onConfirm: (currency: string) => void
}) {
  const { t } = useI18n()
  const id = useId()
  const [value, setValue] = useState(currency)
  return (
    <form
      className="flex flex-wrap items-end gap-3 rounded-xl border bg-card p-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (validCurrency(value) && !disabled) onConfirm(value)
      }}
    >
      <label htmlFor={id} className="flex flex-col gap-1.5 font-medium text-sm">
        {t.currency.title}
        <Input
          id={id}
          value={value}
          maxLength={3}
          pattern="[A-Z]{3}"
          required
          disabled={disabled}
          aria-describedby={`${id}-hint`}
          className="w-28 uppercase"
          onChange={(event) => setValue(event.target.value.toUpperCase())}
        />
      </label>
      <Button type="submit" variant="outline" disabled={disabled || !validCurrency(value)}>
        {t.currency.confirm}
      </Button>
      <div id={`${id}-hint`} className="flex basis-full flex-col gap-1 text-muted-foreground text-xs">
        <span>{t.currency.source[source]}</span>
        <span>{t.currency.hint}</span>
      </div>
    </form>
  )
}

export function CurrencySettings({ eid }: { eid: string }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const engagement = useEngagement(eid).data?.engagement
  const confirm = useMutation({
    ...engagementsSetCurrencyMutation(),
    onSuccess: async () => {
      await invalidateEngagement(queryClient, eid)
      toast.success(t.currency.saved)
    },
    onError: (error) => toast.error(apiMessage(error)),
  })
  if (!engagement) return null
  return (
    <CurrencyForm
      key={`${engagement.currency}:${engagement.currency_source}`}
      currency={engagement.currency ?? 'AUD'}
      source={engagement.currency_source ?? 'default'}
      disabled={engagement.locked || confirm.isPending}
      onConfirm={(currency) => confirm.mutate({ path: { eid }, body: { currency } })}
    />
  )
}
