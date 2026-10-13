import type { ReactNode } from 'react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { apiMessage } from '@/lib/errors'

export function QueryState({
  isPending,
  error,
  children,
}: {
  isPending: boolean
  error: unknown
  children: ReactNode
}) {
  if (isPending) return <p className="text-muted-foreground text-sm">Loading…</p>
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{apiMessage(error)}</AlertDescription>
      </Alert>
    )
  }
  return children
}
