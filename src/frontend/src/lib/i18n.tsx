import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from 'react'
import { en, type Messages } from '@/locales/en'
import { zh } from '@/locales/zh'

// 界面语言: 选择存在 localStorage, 未选择时按浏览器语言; 隐私模式下读写失败按默认处理
export type Lang = 'zh' | 'en'

const KEY = 'ui_lang'
const MESSAGES: Record<Lang, Messages> = { zh, en }

function initialLang(): Lang {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'zh' || saved === 'en') return saved
  } catch {
    // 忽略
  }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

type I18n = {
  lang: Lang
  setLang: (lang: Lang) => void
  t: Messages
  formatDate: (value: string | Date | null | undefined) => string
  formatDateTime: (value: string | Date | null | undefined) => string
}

const I18nContext = createContext<I18n | null>(null)

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>(initialLang)

  useEffect(() => {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
    try {
      localStorage.setItem(KEY, lang)
    } catch {
      // 忽略
    }
  }, [lang])

  const value = useMemo<I18n>(() => {
    const locale = lang === 'zh' ? 'zh-CN' : 'en-AU'
    const dateFormat = new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', day: 'numeric' })
    const dateTimeFormat = new Intl.DateTimeFormat(locale, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
    const toDate = (input: string | Date) => (typeof input === 'string' ? new Date(input) : input)
    return {
      lang,
      setLang,
      t: MESSAGES[lang],
      formatDate: (input) => (input ? dateFormat.format(toDate(input)) : '—'),
      formatDateTime: (input) => (input ? dateTimeFormat.format(toDate(input)) : '—'),
    }
  }, [lang])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18n {
  const value = useContext(I18nContext)
  if (!value) throw new Error('useI18n must be used inside I18nProvider')
  return value
}
