import { useMutation } from '@tanstack/react-query'
import { createFileRoute, Link, redirect, useNavigate } from '@tanstack/react-router'
import { type ReactNode, useState } from 'react'
import { toast } from 'sonner'
import { authRegisterMutation } from '@/client/@tanstack/react-query.gen'
import { BrandMark, LanguageToggle } from '@/components/chrome'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getToken, setToken } from '@/lib/auth'
import { apiMessage, isApiCode } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'

export const Route = createFileRoute('/register')({
  beforeLoad: () => {
    if (getToken()) throw redirect({ to: '/engagements' })
  },
  component: Register,
})

type Field = 'fullName' | 'username' | 'password' | 'confirm'

// 与后端注册接口的规则一致: 账号去首尾空白后不能为空, 不含空白, 最长 254; 姓名最长 100; 密码 8 到 128 位
function problems(
  values: Record<Field, string>,
): Partial<Record<Field, 'username' | 'fullName' | 'password' | 'mismatch'>> {
  const found: Partial<Record<Field, 'username' | 'fullName' | 'password' | 'mismatch'>> = {}
  const username = values.username.trim()
  const fullName = values.fullName.trim()
  if (!fullName || fullName.length > 100) found.fullName = 'fullName'
  if (!username || username.length > 254 || /\s/.test(username)) found.username = 'username'
  if (values.password.length < 8 || values.password.length > 128) found.password = 'password'
  if (values.confirm !== values.password) found.confirm = 'mismatch'
  return found
}

function Register() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const [values, setValues] = useState<Record<Field, string>>({ fullName: '', username: '', password: '', confirm: '' })
  const [submitted, setSubmitted] = useState(false)
  const [taken, setTaken] = useState(false)
  const errors = submitted ? problems(values) : {}

  const register = useMutation({
    ...authRegisterMutation(),
    onSuccess: (token) => {
      setToken(token.access_token)
      void navigate({ to: '/engagements' })
    },
    onError: (error) => {
      if (isApiCode(error, 'USERNAME_TAKEN')) setTaken(true)
      else toast.error(apiMessage(error))
    },
  })

  const set = (field: Field) => (event: React.ChangeEvent<HTMLInputElement>) => {
    if (field === 'username') setTaken(false)
    setValues((current) => ({ ...current, [field]: event.target.value }))
  }

  const usernameError = taken ? t.register.errors.taken : errors.username ? t.register.errors.username : null

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center gap-2.5 bg-sidebar px-4 py-3 sm:px-8">
        <BrandMark />
        <span className="font-semibold text-[15px] text-white">{t.app.name}</span>
        <span className="flex-1" />
        <LanguageToggle />
      </header>
      <main className="flex flex-1 items-start justify-center px-4 pt-[10vh] pb-10">
        <form
          noValidate
          className="flex w-full max-w-[400px] flex-col gap-4 rounded-[14px] border bg-card px-7 py-7"
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(true)
            if (Object.keys(problems(values)).length === 0) {
              register.mutate({
                body: {
                  username: values.username.trim(),
                  full_name: values.fullName.trim(),
                  password: values.password,
                },
              })
            }
          }}
        >
          <h1 className="m-0 font-semibold text-[22px] tracking-tight">{t.register.title}</h1>
          <FormField
            id="register-name"
            label={t.register.fullName}
            error={errors.fullName ? t.register.errors.fullName : null}
          >
            <Input
              id="register-name"
              autoComplete="name"
              className="h-11"
              value={values.fullName}
              onChange={set('fullName')}
              aria-invalid={Boolean(errors.fullName)}
              aria-describedby={errors.fullName ? 'register-name-error' : undefined}
            />
          </FormField>
          <FormField
            id="register-username"
            label={t.register.username}
            hint={t.register.usernameHint}
            error={usernameError}
          >
            <Input
              id="register-username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              className="h-11"
              value={values.username}
              onChange={set('username')}
              aria-invalid={Boolean(usernameError)}
              aria-describedby={usernameError ? 'register-username-error' : 'register-username-hint'}
            />
          </FormField>
          <FormField
            id="register-password"
            label={t.register.password}
            hint={t.register.passwordHint}
            error={errors.password ? t.register.errors.password : null}
          >
            <Input
              id="register-password"
              type="password"
              autoComplete="new-password"
              className="h-11"
              value={values.password}
              onChange={set('password')}
              aria-invalid={Boolean(errors.password)}
              aria-describedby={errors.password ? 'register-password-error' : 'register-password-hint'}
            />
          </FormField>
          <FormField
            id="register-confirm"
            label={t.register.confirm}
            error={errors.confirm ? t.register.errors.mismatch : null}
          >
            <Input
              id="register-confirm"
              type="password"
              autoComplete="new-password"
              className="h-11"
              value={values.confirm}
              onChange={set('confirm')}
              aria-invalid={Boolean(errors.confirm)}
              aria-describedby={errors.confirm ? 'register-confirm-error' : undefined}
            />
          </FormField>
          <Button type="submit" size="lg" disabled={register.isPending}>
            {t.register.submit}
          </Button>
          <p className="m-0 text-center text-[13.5px] text-muted-foreground">
            {t.register.haveAccount}{' '}
            <Link to="/login" className="font-medium text-accent-foreground underline-offset-2 hover:underline">
              {t.register.signIn}
            </Link>
          </p>
        </form>
      </main>
    </div>
  )
}

function FormField({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string
  label: string
  hint?: string
  error: string | null
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="font-semibold text-[13px]">
        {label}
      </label>
      {children}
      {error ? (
        <span id={`${id}-error`} className="text-[12.5px] text-risk">
          {error}
        </span>
      ) : hint ? (
        <span id={`${id}-hint`} className="text-[12.5px] text-muted-foreground">
          {hint}
        </span>
      ) : null}
    </div>
  )
}
