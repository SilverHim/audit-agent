import type { ReactNode } from 'react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

export function QueryState({
  isPending,
  error,
  children,
}: {
  isPending: boolean
  error: unknown
  children: ReactNode
}) {
  const { t } = useI18n()
  if (isPending) return <p className="text-muted-foreground text-sm">{t.app.loading}</p>
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{apiMessage(error)}</AlertDescription>
      </Alert>
    )
  }
  return children
}
