// 未认定建议与撤回入口的实际渲染: 金额档位, 可选的旧记录字段, FIN-01 通过后隐藏撤回, 项目忙时禁用.
import { describe, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { engagementsGetEngagementQueryKey } from '../src/client/@tanstack/react-query.gen'
import type { EngagementOverview, UnrecognisedSuggestion } from '../src/client/types.gen'
import { UnrecognisedSuggestions } from '../src/components/unrecognised-suggestions'
import { WithdrawDecision } from '../src/components/withdraw-decision'
import { I18nProvider } from '../src/lib/i18n'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const EID = 'ENG-TEST'
const ITEM: UnrecognisedSuggestion = {
  judgment_id: 'J-GEN-02-1-XJ-499',
  procedure_id: 'GEN-02',
  subject_id: 'XJ-499',
  task_id: 'T-XJ499',
  record_id: 'D-XJ499',
  decision: 'override',
  reviewed_by: 'U-TEST',
  decided_at: '2026-10-21T12:00:00Z',
  account: 'Office equipment',
  account_code: '710',
  assertion: 'Existence',
  amount_cents: 75000,
  direction: 'overstated',
  level: 'material',
  would_qualify: true,
}

async function render(children: ReactNode, canWithdraw = true, running = false, language = 'en-AU') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(engagementsGetEngagementQueryKey({ path: { eid: EID } }), {
    engagement: { locked: false },
    can_withdraw: canWithdraw,
    running,
    run_error: null,
  } as EngagementOverview)
  const router = createRouter({
    routeTree: createRootRoute({ component: () => children }),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  await router.load()
  const originalLanguage = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: language, configurable: true })
  try {
    return renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <I18nProvider>
          <RouterProvider router={router} />
        </I18nProvider>
      </QueryClientProvider>,
    )
  } finally {
    if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
    else Reflect.deleteProperty(navigator, 'language')
    client.clear()
  }
}

describe('AI 建议为错报但未认定', () => {
  test('重大金额标红, 列出金额与复核记录, 可以查看判断与撤回改判', async () => {
    const html = await render(<UnrecognisedSuggestions eid={EID} items={[ITEM]} />)
    for (const text of ['XJ-499', '750.00', 'U-TEST', en.completion.unrecognised.wouldQualify]) {
      expect(html).toContain(text)
    }
    expect(html).toContain('border-risk bg-risk-soft')
    expect(html).toContain(`/engagements/${EID}/reviews/T-XJ499`)
    expect(html).toContain(en.completion.unrecognised.redecide)
  })

  test('旧记录可缺少任务和决定; 仍提示金额, 不生成空链接或撤回入口', async () => {
    const item = { ...ITEM, task_id: null, record_id: null, decision: null, reviewed_by: null, decided_at: null }
    const html = await render(<UnrecognisedSuggestions eid={EID} items={[item]} />)
    expect(html).toContain('750.00')
    expect(html).toContain(en.clientPage.notRecorded)
    expect(html).not.toContain('<a ')
    expect(html).not.toContain(en.completion.unrecognised.redecide)
  })

  test('较小金额仍醒目提示, 意见通过后保留查看入口并隐藏撤回; 没有建议时整节隐藏', async () => {
    const item = { ...ITEM, level: 'above_trivial' as const, amount_cents: 4853, would_qualify: false }
    const html = await render(<UnrecognisedSuggestions eid={EID} items={[item]} />, false)
    expect(html).toContain(en.completion.unrecognised.levels.above_trivial)
    expect(html).not.toContain('border-risk bg-risk-soft')
    expect(html).toContain(en.completion.unrecognised.open)
    expect(html).not.toContain(en.completion.unrecognised.redecide)
    expect(await render(<UnrecognisedSuggestions eid={EID} items={[]} />)).not.toContain('unrecognised-suggestions')
  })

  test('中文界面显示中文金额档位, 意见影响与撤回入口', async () => {
    const html = await render(<UnrecognisedSuggestions eid={EID} items={[ITEM]} />, true, false, 'zh-CN')
    for (const text of [
      zh.completion.unrecognised.levels.material,
      zh.completion.unrecognised.wouldQualify,
      zh.completion.unrecognised.redecide,
    ]) {
      expect(html).toContain(text)
    }
  })
})

test('撤回按钮使用对应入口的文案, 项目忙时禁用', async () => {
  const html = await render(<WithdrawDecision eid={EID} recordId="D-XJ499" disabled />)
  expect(html).toContain(en.withdraw.action)
  expect(html).toContain('disabled')
  const busy = await render(<UnrecognisedSuggestions eid={EID} items={[ITEM]} />, true, true)
  expect(busy).toContain(en.completion.unrecognised.redecide)
  expect(busy).toContain('disabled')
})
