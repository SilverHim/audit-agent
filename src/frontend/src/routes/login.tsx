import { useMutation } from '@tanstack/react-query'
import { createFileRoute, Link, redirect, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { toast } from 'sonner'
import { authLoginMutation } from '@/client/@tanstack/react-query.gen'
import { BrandMark, LanguageToggle } from '@/components/chrome'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getToken, setToken } from '@/lib/auth'
import { apiMessage, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

export const Route = createFileRoute('/login')({
  beforeLoad: () => {
    if (getToken()) throw redirect({ to: '/engagements' })
  },
  component: Login,
})

function Login() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const login = useMutation({
    ...authLoginMutation(),
    onSuccess: (token) => {
      setToken(token.access_token)
      void navigate({ to: '/engagements' })
    },
    onError: (error) => toast.error(isApiCode(error, 'BAD_CREDENTIALS') ? t.login.bad : apiMessage(error)),
  })

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center gap-2.5 bg-sidebar px-4 py-3 sm:px-8">
        <BrandMark />
        <span className="font-semibold text-[15px] text-white">{t.app.name}</span>
        <span className="flex-1" />
        <LanguageToggle />
      </header>
      <main className="flex flex-1 items-start justify-center px-4 pt-[12vh]">
        <form
          className="flex w-full max-w-[400px] flex-col gap-4 rounded-[14px] border bg-card px-7 py-7"
          onSubmit={(event) => {
            event.preventDefault()
            login.mutate({ body: { username: email, password } })
          }}
        >
          <h1 className="m-0 font-semibold text-[22px] tracking-tight">{t.login.title}</h1>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-email" className="font-semibold text-[13px]">
              {t.login.email}
            </label>
            <Input
              id="login-email"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              className="h-11"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-password" className="font-semibold text-[13px]">
              {t.login.password}
            </label>
            <Input
              id="login-password"
              type="password"
              autoComplete="current-password"
              className="h-11"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
          <Button type="submit" size="lg" disabled={login.isPending}>
            {t.login.submit}
          </Button>
          <p className="m-0 text-center text-[13.5px] text-muted-foreground">
            {t.login.noAccount}{' '}
            <Link to="/register" className="font-medium text-accent-foreground underline-offset-2 hover:underline">
              {t.login.register}
            </Link>
          </p>
        </form>
      </main>
    </div>
  )
}
