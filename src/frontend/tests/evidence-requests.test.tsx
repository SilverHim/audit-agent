// 补充证据闭环的前端测试 (后端需求 R369, 页面 R370), bun test 运行: 请求的状态显示与操作入口, 修改与标无法取得的
// 请求体, 由补充证据请求重判的来龙去脉, 以及复核卡片上的 "要求补充证据" 选项. 数据是合成的: 只填用到的字段.
import { describe, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import {
  engagementsGetEngagementQueryKey,
  evidenceListDocumentsQueryKey,
  evidenceListEvidenceRequestsQueryKey,
} from '../src/client/@tanstack/react-query.gen'
import type { DocumentRef, EngagementOverview, EvidenceRequest, ReviewTaskView } from '../src/client/types.gen'
import { ReviewDecisionCard } from '../src/components/concierge'
import { EvidenceRequestRow, EvidenceRequests, RejudgeNotice } from '../src/components/evidence-requests'
import {
  editRequestBody,
  rejudgedRequest,
  requestDocuments,
  requestEditForm,
  requestFrom,
  requestRelated,
  requestStatus,
  requestStatusChanged,
  requestStatuses,
  unavailableBody,
} from '../src/lib/evidence-requests'
import { I18nProvider } from '../src/lib/i18n'
import type { RankedTask } from '../src/lib/review'
import { MoneyProvider } from '../src/lib/use-money'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const EID = 'ENG-TEST'

function request(patch: Partial<EvidenceRequest> = {}): EvidenceRequest {
  return {
    id: 'ER-D-1',
    version: 1,
    task_id: 'T-1',
    judgment_id: 'J-1',
    procedure_id: 'GEN-02',
    subject_id: 'XJ-499',
    description: 'The delivery note for journal entry XJ-499, dated 16 August 2026.',
    related_object_ids: ['XJ-499'],
    requested_from: 'client',
    status: 'open',
    document_ids: [],
    unavailable_reason: null,
    rejudged_judgment_id: null,
    requested_by: 'U-1',
    requested_at: '2026-10-22T01:00:00Z',
    resolved_at: null,
    resolved_by: null,
    ...patch,
  }
}

function document(patch: Partial<DocumentRef> = {}): DocumentRef {
  return {
    id: 'DOC-1',
    type: 'internal_record',
    file_name: 'delivery-note.pdf',
    sha256: 'a'.repeat(64),
    page_count: 2,
    reliability: 'internal_original',
    status: 'received',
    uploaded_at: '2026-10-22T02:00:00Z',
    ...patch,
  } as DocumentRef
}

// 复核卡片要的一条任务: AI 暂时判断不了, 后端允许要求补充证据
function ranked(): RankedTask {
  const view = {
    task: {
      id: 'T-1',
      kind: 'normal',
      procedure_id: 'GEN-02',
      target_version: 2,
      status: 'pending',
      run_id: 'RUN-GEN-02-1',
      target_type: 'Judgment',
      target_id: 'J-1',
    },
    procedure_name: 'Journal entry exception attribution',
    target: {
      id: 'J-1',
      version: 1,
      procedure_id: 'GEN-02',
      run_id: 'RUN-GEN-02-1',
      subject_id: 'XJ-499',
      conclusion: 'Cannot tell yet: the delivery note is missing.',
      confidence: 0.5,
      confidence_parts: {},
      reasoning_chain: [],
      evidence: [],
      counter_argument: '',
      proposed_action: 'more_evidence',
      verdict: 'undetermined',
      autonomy_level: 'L2',
      performed_by: 'U-1',
      created_at: '2026-10-22T00:00:00Z',
    },
    allowed_decisions: ['confirm', 'override', 'misstatement', 'more_evidence'],
    amount_cents: null,
    tier: 'read_reasoning',
  } as unknown as ReviewTaskView
  return { view, amount: null, tier: 'read_reasoning', subject: 'XJ-499' } as RankedTask
}

async function render(
  children: ReactNode,
  {
    language = 'en-AU',
    requests,
    documents = [],
    locked = false,
  }: { language?: string; requests?: EvidenceRequest[]; documents?: DocumentRef[]; locked?: boolean } = {},
): Promise<string> {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  client.setQueryData(engagementsGetEngagementQueryKey({ path: { eid: EID } }), {
    engagement: { id: EID, currency: 'AUD', locked },
    features: ['more_evidence'],
    run_languages: {},
  } as unknown as EngagementOverview)
  if (requests) client.setQueryData(evidenceListEvidenceRequestsQueryKey({ path: { eid: EID } }), requests)
  client.setQueryData(evidenceListDocumentsQueryKey({ path: { eid: EID } }), documents)
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
          <MoneyProvider currency="AUD">
            <RouterProvider router={router} />
          </MoneyProvider>
        </I18nProvider>
      </QueryClientProvider>,
    )
  } finally {
    if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
    else Reflect.deleteProperty(navigator, 'language')
    client.clear()
  }
}

describe('补充证据请求的取值', () => {
  test('状态, 向谁要, 关联对象与文件: 旧数据没填时按等证据, 客户与空', () => {
    expect(requestStatus(request())).toBe('open')
    expect(requestStatus(request({ status: 'unavailable' }))).toBe('unavailable')
    expect(requestFrom(request())).toBe('client')
    expect(requestFrom(request({ requested_from: undefined }))).toBe('client')
    expect(requestRelated(request({ related_object_ids: undefined }))).toEqual([])
    expect(requestDocuments(request({ document_ids: undefined }))).toEqual([])
  })

  test('请求状态的变化: open 变成 received 或 unavailable 时要刷新复核队列', () => {
    const before = requestStatuses([request()])
    expect(requestStatusChanged(before, [request()])).toBe(false)
    expect(requestStatusChanged(null, [request({ status: 'received' })])).toBe(false) // 第一次读到不算变化
    expect(requestStatusChanged(before, [request({ status: 'received' })])).toBe(true)
    expect(requestStatusChanged(before, [request({ status: 'unavailable' })])).toBe(true)
    expect(requestStatusChanged(before, [request(), request({ id: 'ER-D-2' })])).toBe(false) // 新出现的请求不算变化
  })

  test('由补充证据请求重判: 只看 rejudged_judgment_id 等于该判断的请求, 没有为 null', () => {
    const rejudged = request({ id: 'ER-D-2', status: 'received', rejudged_judgment_id: 'J-NEW' })
    expect(rejudgedRequest([request(), rejudged], 'J-NEW')).toBe(rejudged)
    expect(rejudgedRequest([request(), rejudged], 'J-1')).toBeNull()
    expect(rejudgedRequest([request()], 'J-NEW')).toBeNull()
  })
})

describe('修改请求与标无法取得', () => {
  test('表单初值: 要什么, 向谁要与关联对象 (逗号分隔)', () => {
    expect(requestEditForm(request())).toEqual({
      description: 'The delivery note for journal entry XJ-499, dated 16 August 2026.',
      requestedFrom: 'client',
      relatedIds: 'XJ-499',
    })
  })

  test('只提交改了的项; 要什么不能为空; 一项都没改为错误文案', () => {
    const current = request({ related_object_ids: ['XJ-499', 'BA-091'] })
    const form = requestEditForm(current)
    expect(editRequestBody(current, { ...form, description: '  The delivery note and invoice  ' }, en)).toEqual({
      description: 'The delivery note and invoice',
    })
    expect(editRequestBody(current, { ...form, requestedFrom: 'bank' }, en)).toEqual({ requested_from: 'bank' })
    expect(editRequestBody(current, { ...form, relatedIds: ' XJ-499 ,, BA-091 ' }, en)).toBe(
      en.evidenceRequests.errors.noChange,
    )
    expect(editRequestBody(current, { ...form, relatedIds: 'XJ-499, BA-091, CON-001' }, en)).toEqual({
      related_object_ids: ['XJ-499', 'BA-091', 'CON-001'],
    })
    expect(editRequestBody(current, { ...form, description: '   ' }, en)).toBe(en.evidenceRequests.errors.description)
    expect(editRequestBody(current, { ...form, requestedFrom: 'management' }, zh)).toEqual({
      requested_from: 'management',
    })
  })

  test('标无法取得: 理由选填, 空或只有空白为 null', () => {
    expect(unavailableBody('  The bank refused to confirm.  ')).toEqual({ reason: 'The bank refused to confirm.' })
    expect(unavailableBody('   ')).toEqual({ reason: null })
  })
})

describe('请求的状态显示与操作入口', () => {
  test('等证据: 等待证据, 要什么, 向谁要与关联对象, 有修改/上传/标无法取得三个入口', async () => {
    const html = await render(
      <EvidenceRequestRow
        eid={EID}
        request={request()}
        documents={[]}
        disabled={false}
        onEdit={() => {}}
        onUnavailable={() => {}}
        onUpload={() => {}}
      />,
    )
    for (const text of [
      'ER-D-1',
      'Waiting for evidence',
      'Client',
      'GEN-02',
      'XJ-499',
      'The delivery note for journal entry XJ-499',
      'Edit request',
      'Upload evidence',
      'Mark unavailable',
    ]) {
      expect(html).toContain(text)
    }
    expect(html).toContain('data-status="open"')
    expect(html).toContain(`evidence-upload-ER-D-1`)
  })

  test('已收到: 给出登记的文件链接, 不再有修改与标无法取得入口', async () => {
    const html = await render(
      <EvidenceRequestRow
        eid={EID}
        request={request({ status: 'received', document_ids: ['DOC-1'] })}
        documents={[document()]}
        disabled={false}
        onEdit={() => {}}
        onUnavailable={() => {}}
        onUpload={() => {}}
      />,
    )
    expect(html).toContain('Received')
    expect(html).toContain('delivery-note.pdf')
    expect(html).toContain(`/engagements/${EID}/data/documents/DOC-1`)
    expect(html).not.toContain('Edit request')
    expect(html).not.toContain('Mark unavailable')
  })

  test('无法取得: 显示状态与理由, 不再有入口', async () => {
    const html = await render(
      <EvidenceRequestRow
        eid={EID}
        request={request({ status: 'unavailable', unavailable_reason: 'The bank refused to confirm.' })}
        documents={[]}
        disabled={false}
        onEdit={() => {}}
        onUnavailable={() => {}}
        onUpload={() => {}}
      />,
    )
    expect(html).toContain('Unavailable')
    expect(html).toContain('The bank refused to confirm.')
    expect(html).not.toContain('Edit request')
  })

  test('中文界面: 等待证据 / 已收到 / 无法取得', async () => {
    for (const [status, text] of [
      ['open', '等待证据'],
      ['received', '已收到'],
      ['unavailable', '无法取得'],
    ] as const) {
      const html = await render(
        <EvidenceRequestRow
          eid={EID}
          request={request({ status })}
          documents={[]}
          disabled={false}
          onEdit={() => {}}
          onUnavailable={() => {}}
          onUpload={() => {}}
        />,
        { language: 'zh-CN' },
      )
      expect(html).toContain(text)
    }
  })
})

describe('补充证据请求区块与重判链接', () => {
  test('数据管理页的区块列出请求; 没有请求时整块不渲染', async () => {
    const html = await render(<EvidenceRequests eid={EID} />, {
      requests: [request(), request({ id: 'ER-D-2', status: 'received', document_ids: [] })],
      documents: [],
    })
    expect(html).toContain('Supplementary evidence requests')
    expect(html).toContain('ER-D-1')
    expect(html).toContain('ER-D-2')
    expect(await render(<EvidenceRequests eid={EID} />, { requests: [] })).toBe('')
  })

  test('重判后的判断给出 "由补充证据请求 ER-… 重判" 的链接; 没有重判记录时不显示', async () => {
    const html = await render(<RejudgeNotice eid={EID} judgmentId="J-NEW" />, {
      requests: [request({ id: 'ER-D-9', status: 'received', rejudged_judgment_id: 'J-NEW' })],
    })
    expect(html).toContain('data-testid="rejudge-notice"')
    expect(html).toContain('Rejudged from supplementary evidence request ER-D-9')
    expect(html).toContain(`/engagements/${EID}/data`)
    expect(
      await render(<RejudgeNotice eid={EID} judgmentId="J-OTHER" />, {
        requests: [request({ id: 'ER-D-9', rejudged_judgment_id: 'J-NEW' })],
      }),
    ).toBe('')
  })
})

test('复核任务等待证据的状态与决定文案 (需求 R370 第 2 条)', () => {
  expect(zh.status.awaitingEvidence).toContain('证据')
  expect(en.status.awaitingEvidence).toContain('Waiting for evidence')
  expect(zh.decisions.more_evidence).toBe('要求补充证据')
  expect(en.decisions.more_evidence).toContain('more evidence')
  expect(zh.decisionsPage.waitingEvidence).toBe('等待证据的任务')
})

describe('复核卡片的要求补充证据选项', () => {
  test('普通复核任务的复核卡片列出该选项, 提交体只带理由 (需求 R370 第 1 条)', async () => {
    const html = await render(
      <ReviewDecisionCard
        eid={EID}
        ranked={ranked()}
        disabled={false}
        deferred={false}
        onDefer={() => {}}
        deferring={false}
      />,
    )
    expect(html).toContain('Ask for more evidence, raising a request')
    expect(html).toContain(en.card.choices.more_evidence.hint)
    expect(html).toContain('decision-T-1-more_evidence')
  })

  test('没有错报更新载荷时也不需要复制的更正分录; 中文文案同时给', async () => {
    const html = await render(
      <ReviewDecisionCard
        eid={EID}
        ranked={ranked()}
        disabled={false}
        deferred={false}
        onDefer={() => {}}
        deferring={false}
      />,
      { language: 'zh-CN' },
    )
    expect(html).toContain('要求补充证据，生成补交请求')
    expect(html).toContain(zh.card.choices.more_evidence.hint)
  })
})
