import type { QueryClient } from '@tanstack/react-query'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createRootRouteWithContext, Link, Outlet, useNavigate } from '@tanstack/react-router'
import { usersMeOptions } from '@/client/@tanstack/react-query.gen'
import { BackendStatus } from '@/components/backend-status'
import { Button } from '@/components/ui/button'
import { Toaster } from '@/components/ui/sonner'
import { clearToken, getToken } from '@/lib/auth'

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: RootLayout,
})

function RootLayout() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const authed = Boolean(getToken())
  const me = useQuery({ ...usersMeOptions(), enabled: authed })

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <Link to="/engagements" className="font-heading font-semibold">
          Audit Support
        </Link>
        <div className="flex items-center gap-3">
          {authed ? (
            <>
              <span className="text-sm">{me.data?.full_name}</span>
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  clearToken()
                  queryClient.clear()
                  void navigate({ to: '/login' })
                }}
              >
                Sign out
              </Button>
            </>
          ) : null}
          <BackendStatus />
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
      <Toaster />
    </div>
  )
}
