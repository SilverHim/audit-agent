import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from '@tanstack/react-router'
import { cn } from 'cn'
import { LogOut } from 'lucide-react'
import type { ReactNode } from 'react'
import { usersMeOptions } from '@/client/@tanstack/react-query.gen'
import { clearToken, getToken } from '@/lib/auth'
import { useI18n } from '@/lib/i18n'

// 外壳通用件: 标识, 语言切换, 当前用户, 页头, 状态标签

export function BrandMark({ className }: { className?: string }) {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true" className={className}>
      <rect width="26" height="26" rx="7" fill="#2B46D1" />
      <path d="M7 9h12M7 13h7M7 17h5" stroke="#FFFFFF" strokeWidth="1.8" strokeLinecap="round" />
      <path
        d="M15.5 17.2l1.8 1.8 3.4-4"
        stroke="#FFFFFF"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  )
}

export function LanguageToggle({ stretch = false }: { stretch?: boolean }) {
  const { lang, setLang, t } = useI18n()
  return (
    <fieldset
      aria-label={t.lang.label}
      className="flex gap-1 rounded-[9px] border border-sidebar-border bg-sidebar-raised p-[3px]"
    >
      {(['en', 'zh'] as const).map((code) => (
        <button
          key={code}
          type="button"
          lang={code === 'zh' ? 'zh-CN' : 'en'}
          aria-pressed={lang === code}
          onClick={() => setLang(code)}
          className={cn(
            'rounded-[7px] px-3 py-1.5 text-[12.5px] transition-colors',
            stretch && 'flex-1',
            lang === code ? 'bg-sidebar-primary font-semibold text-white' : 'text-[#c9d1dc] hover:text-white',
          )}
        >
          {code === 'en' ? t.lang.en : t.lang.zh}
        </button>
      ))}
    </fieldset>
  )
}

export function useSignOut() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  return () => {
    clearToken()
    queryClient.clear()
    void navigate({ to: '/login' })
  }
}

function initials(name: string | undefined): string {
  if (!name) return '·'
  const parts = name.trim().split(/\s+/)
  return (parts[0]?.[0] ?? '').concat(parts.length > 1 ? (parts.at(-1)?.[0] ?? '') : '').toUpperCase()
}

export function CurrentUser({ withRole = false }: { withRole?: boolean }) {
  const { t } = useI18n()
  const me = useQuery({ ...usersMeOptions(), enabled: Boolean(getToken()) })
  const signOut = useSignOut()
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <span className="flex size-[30px] shrink-0 items-center justify-center rounded-full bg-sidebar-border font-semibold text-white text-xs">
        {initials(me.data?.full_name)}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-medium text-[13px] text-white">{me.data?.full_name ?? '…'}</span>
        {withRole ? <span className="text-sidebar-muted text-xs">{t.app.role}</span> : null}
      </span>
      <button
        type="button"
        onClick={signOut}
        aria-label={t.app.signOut}
        title={t.app.signOut}
        className="flex size-9 shrink-0 items-center justify-center rounded-lg text-sidebar-foreground hover:bg-sidebar-accent hover:text-white"
      >
        <LogOut className="size-[17px]" aria-hidden="true" />
      </button>
    </div>
  )
}

// 不在项目内的页面 (项目首页, 设置) 用的深色顶栏
export function TopBar() {
  const { t } = useI18n()
  return (
    <header className="flex flex-wrap items-center gap-4 bg-sidebar px-4 py-3 text-sidebar-foreground sm:px-8">
      <Link to="/engagements" className="flex items-center gap-2.5">
        <BrandMark />
        <span className="font-semibold text-[15px] text-white">{t.app.name}</span>
      </Link>
      <span className="flex-1" />
      <LanguageToggle />
      <Link
        to="/settings"
        className="rounded-lg px-2.5 py-2 text-[#d4d9e2] text-[13.5px] hover:bg-sidebar-accent hover:text-white"
        activeProps={{ className: 'bg-sidebar-accent text-white' }}
      >
        {t.settings.title}
      </Link>
      <div className="w-56">
        <CurrentUser />
      </div>
    </header>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b bg-card px-4 py-4 sm:px-7">
      <div className="flex min-w-0 flex-col">
        <h1 className="font-semibold text-xl tracking-tight">{title}</h1>
        {subtitle ? <div className="text-[13px] text-muted-foreground">{subtitle}</div> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2.5">{actions}</div> : null}
    </header>
  )
}

export type Tone = 'ok' | 'warn' | 'risk' | 'neutral' | 'accent'

const TONES: Record<Tone, string> = {
  ok: 'bg-ok-soft text-ok',
  warn: 'bg-warn-soft text-warn',
  risk: 'bg-risk-soft text-risk',
  neutral: 'bg-muted text-secondary-foreground',
  accent: 'bg-accent text-accent-foreground',
}

export function StatusPill({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 font-medium text-[12.5px]',
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('font-mono', className)}>{children}</span>
}

export function SectionLabel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn('font-semibold text-[12px] text-muted-foreground tracking-wide', className)}>{children}</span>
  )
}
