import { createFileRoute, redirect } from '@tanstack/react-router'
import { cn } from 'cn'
import { useState } from 'react'
import { toast } from 'sonner'
import { TopBar } from '@/components/chrome'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { getToken } from '@/lib/auth'
import { apiMessage } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import {
  type LlmSlot,
  type LlmSlotView,
  type LlmTestResult,
  useLlmSettings,
  useSaveLlmSlot,
  useTestLlmSlot,
} from '@/lib/new-api'

export const Route = createFileRoute('/settings')({
  beforeLoad: () => {
    if (!getToken()) throw redirect({ to: '/login' })
  },
  component: Settings,
})

const PROVIDERS: Array<{ id: string; url: string; models: Partial<Record<LlmSlot, string>> }> = [
  {
    id: 'anthropic',
    url: 'https://api.anthropic.com/v1',
    models: { primary: 'claude-opus-5-5', review: 'claude-fable-5-1' },
  },
  { id: 'openai', url: 'https://api.openai.com/v1', models: {} },
  { id: 'deepseek', url: 'https://api.deepseek.com/v1', models: { primary: 'deepseek-chat', review: 'deepseek-chat' } },
  { id: 'qwen', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', models: {} },
  { id: 'ollama', url: 'http://localhost:11434/v1', models: {} },
  { id: 'custom', url: '', models: {} },
]

function defaults(slot: LlmSlot): LlmSlotView {
  return {
    provider: 'anthropic',
    base_url: PROVIDERS[0].url,
    model: PROVIDERS[0].models[slot] ?? '',
    has_key: false,
    key_last4: null,
    updated_at: null,
    updated_by: null,
  }
}

function Settings() {
  const { t } = useI18n()
  const settings = useLlmSettings()
  return (
    <div className="flex min-h-screen flex-col">
      <TopBar />
      <div className="mx-auto flex w-full max-w-[1180px] flex-1 flex-wrap items-start gap-7 px-4 py-8 sm:px-8">
        <nav aria-label={t.settings.title} className="flex flex-[1_1_200px] flex-col gap-0.5">
          <h1 className="m-0 mb-3 font-semibold text-[22px] tracking-tight">{t.settings.title}</h1>
          <span aria-current="page" className="flex min-h-10 items-center rounded-lg bg-[#e6e9f0] px-3 font-semibold">
            {t.settings.aiModels}
          </span>
        </nav>
        <main className="flex min-w-0 flex-[999_1_520px] flex-col gap-5">
          {settings.isPending ? (
            <p className="text-muted-foreground text-sm">{t.app.loading}</p>
          ) : (
            (['primary', 'review'] as const).map((slot) => (
              <SlotForm
                key={`${slot}:${settings.data?.[slot].updated_at ?? 'new'}`}
                slot={slot}
                initial={settings.data?.[slot] ?? defaults(slot)}
              />
            ))
          )}
          <p className="m-0 text-[13px] text-[#4a5262]">{t.settings.effect}</p>
        </main>
        <aside className="flex flex-[1_1_260px] flex-col gap-3 rounded-[14px] border bg-card p-5">
          <h2 className="m-0 font-semibold text-[14px]">{t.settings.dataTitle}</h2>
          <ul className="m-0 flex flex-col gap-2 pl-[18px] text-[13px] text-secondary-foreground">
            {t.settings.dataItems.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <p className="m-0 border-line-soft border-t pt-2.5 text-[12.5px] text-muted-foreground">
            {t.settings.dataFoot}
          </p>
        </aside>
      </div>
    </div>
  )
}

function SlotForm({ slot, initial }: { slot: LlmSlot; initial: LlmSlotView }) {
  const { t } = useI18n()
  const save = useSaveLlmSlot()
  const test = useTestLlmSlot()
  const [provider, setProvider] = useState(initial.provider)
  const [baseUrl, setBaseUrl] = useState(initial.base_url)
  const [model, setModel] = useState(initial.model)
  const [key, setKey] = useState('')
  const [result, setResult] = useState<LlmTestResult | null>(null)
  const local = provider === 'ollama'
  const body = { provider, base_url: baseUrl.trim(), model: model.trim(), api_key: key.trim() || null }
  const keyStatus = local
    ? t.settings.keyNotNeeded
    : initial.has_key && initial.key_last4
      ? t.settings.keySaved(initial.key_last4)
      : t.settings.keyNone

  const pick = (id: string) => {
    const preset = PROVIDERS.find((item) => item.id === id)
    setProvider(id)
    setBaseUrl(preset?.url ?? '')
    setModel(preset?.models[slot] ?? '')
    setResult(null)
  }

  return (
    <section
      aria-label={slot === 'primary' ? t.settings.primary : t.settings.review}
      className="flex flex-col gap-4.5 rounded-[14px] border bg-card px-6 py-5.5"
    >
      <div className="flex flex-col gap-0.5">
        <h2 className="m-0 font-semibold text-base">{slot === 'primary' ? t.settings.primary : t.settings.review}</h2>
        <p className="m-0 text-[13.5px] text-[#4a5262]">
          {slot === 'primary' ? t.settings.primaryDesc : t.settings.reviewDesc}
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <span className="font-semibold text-[13px]">{t.settings.provider}</span>
        <fieldset aria-label={t.settings.provider} className="flex flex-wrap gap-2">
          {PROVIDERS.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-pressed={provider === item.id}
              onClick={() => pick(item.id)}
              className={cn(
                'min-h-9 rounded-full border-[1.5px] px-3.5 text-[13.5px]',
                provider === item.id
                  ? 'border-primary bg-accent font-semibold text-accent-foreground'
                  : 'border-[#dce0e6] bg-card text-secondary-foreground',
              )}
            >
              {t.settings.providers[item.id]}
            </button>
          ))}
        </fieldset>
      </div>
      <div className="flex flex-wrap gap-4">
        <div className="flex flex-[2_1_320px] flex-col gap-1.5">
          <label htmlFor={`${slot}-url`} className="font-semibold text-[13px]">
            {t.settings.endpoint}
          </label>
          <Input
            id={`${slot}-url`}
            type="url"
            className="h-11 font-mono text-[13.5px]"
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </div>
        <div className="flex flex-[1_1_220px] flex-col gap-1.5">
          <label htmlFor={`${slot}-model`} className="font-semibold text-[13px]">
            {t.settings.model}
          </label>
          <Input
            id={`${slot}-model`}
            className="h-11 font-mono text-[13.5px]"
            placeholder={t.settings.modelPlaceholder}
            value={model}
            onChange={(event) => setModel(event.target.value)}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${slot}-key`} className="font-semibold text-[13px]">
          {t.settings.apiKey}
        </label>
        <div className="flex flex-wrap items-center gap-2.5">
          <Input
            id={`${slot}-key`}
            type="password"
            autoComplete="off"
            className="h-11 flex-[1_1_320px] font-mono text-[13.5px]"
            disabled={local}
            placeholder={local ? t.settings.keyLocal : initial.has_key ? t.settings.keyKeep : t.settings.keyPlaceholder}
            value={key}
            onChange={(event) => setKey(event.target.value)}
          />
          <span className="text-[12.5px] text-[#4a5262]">{keyStatus}</span>
        </div>
        <span className="text-[12.5px] text-muted-foreground">{t.settings.keyNote}</span>
      </div>
      <div className="flex flex-wrap items-center gap-3 border-line-soft border-t pt-3.5">
        <Button
          type="button"
          variant="outline"
          size="lg"
          disabled={test.isPending || !body.base_url || !body.model}
          onClick={() =>
            test.mutate(
              { slot, body },
              {
                onSuccess: setResult,
                onError: (error) => setResult({ ok: false, model: null, latency_ms: 0, message: apiMessage(error) }),
              },
            )
          }
        >
          {test.isPending ? t.settings.testing : t.settings.test}
        </Button>
        {result ? (
          <span
            className={cn(
              'rounded-lg px-3 py-2 text-[13px]',
              result.ok ? 'bg-ok-soft text-ok' : 'bg-risk-soft text-risk',
            )}
          >
            {result.ok
              ? t.settings.testOk(result.model ?? model, result.latency_ms)
              : t.settings.testFail(result.message)}
          </span>
        ) : null}
        <span className="flex-1" />
        <Button
          type="button"
          size="lg"
          disabled={save.isPending || !body.base_url || !body.model}
          onClick={() =>
            save.mutate(
              { slot, body },
              {
                onSuccess: () => {
                  setKey('')
                  toast.success(t.settings.saved)
                },
                onError: (error) => toast.error(apiMessage(error)),
              },
            )
          }
        >
          {t.settings.save}
        </Button>
      </div>
    </section>
  )
}
