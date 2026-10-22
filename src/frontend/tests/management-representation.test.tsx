// 登记管理层说明的前端测试 (后端需求 R369, 页面 R370), bun test 运行: 表单的字段与只读, 请求体的必填与去空白,
// 可靠性文案. 数据是合成的.
import { describe, expect, test } from 'bun:test'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { ManagementRepresentationForm } from '../src/components/management-representation'
import { emptyRepresentation, representationBody } from '../src/lib/evidence-requests'
import { I18nProvider } from '../src/lib/i18n'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const EID = 'ENG-TEST'

function render(children: ReactNode, language = 'en-AU'): string {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const originalLanguage = Object.getOwnPropertyDescriptor(navigator, 'language')
  Object.defineProperty(navigator, 'language', { value: language, configurable: true })
  try {
    return renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <I18nProvider>{children}</I18nProvider>
      </QueryClientProvider>,
    )
  } finally {
    if (originalLanguage) Object.defineProperty(navigator, 'language', originalLanguage)
    else Reflect.deleteProperty(navigator, 'language')
    client.clear()
  }
}

describe('登记管理层说明的请求体', () => {
  test('三项都必填; 去掉首尾空白后照填', () => {
    const draft = { person: '  Alex Morgan ', statedOn: ' 2026-10-21 ', content: ' No fraud noticed. ' }
    expect(representationBody(draft, en)).toEqual({
      person: 'Alex Morgan',
      stated_on: '2026-10-21',
      content: 'No fraud noticed.',
    })
    expect(representationBody(draft, zh)).toEqual({
      person: 'Alex Morgan',
      stated_on: '2026-10-21',
      content: 'No fraud noticed.',
    })
  })

  test('空表单逐项提示缺的字段; 只缺一项时只列它', () => {
    const empty = representationBody(emptyRepresentation(), en)
    expect(typeof empty).toBe('string')
    for (const label of [
      en.managementRepresentation.person,
      en.managementRepresentation.statedOn,
      en.managementRepresentation.content,
    ]) {
      expect(empty as string).toContain(label)
    }
    const partial = representationBody({ person: 'Alex', statedOn: '', content: 'No fraud.' }, zh)
    expect(typeof partial).toBe('string')
    expect(partial as string).toContain(zh.managementRepresentation.statedOn)
    expect(partial as string).not.toContain(zh.managementRepresentation.content)
  })
})

describe('登记管理层说明的表单', () => {
  test('三个字段与提交按钮; 初始为空时仍可填写', () => {
    const html = render(<ManagementRepresentationForm eid={EID} disabled={false} />)
    for (const text of [
      en.managementRepresentation.title,
      en.managementRepresentation.person,
      en.managementRepresentation.statedOn,
      en.managementRepresentation.content,
      en.managementRepresentation.register,
    ]) {
      expect(html).toContain(text)
    }
    expect(html).toContain('id="mr-person"')
    expect(html).toContain('id="mr-stated-on"')
    expect(html).toContain('id="mr-content"')
  })

  test('项目已签字只读时说明并禁用提交', () => {
    const html = render(<ManagementRepresentationForm eid={EID} disabled />)
    expect(html).toContain(en.managementRepresentation.locked)
    expect(html).toContain('disabled')
  })

  test('中文界面用中文文案', () => {
    const html = render(<ManagementRepresentationForm eid={EID} disabled={false} />, 'zh-CN')
    for (const text of [
      zh.managementRepresentation.title,
      zh.managementRepresentation.person,
      zh.managementRepresentation.statedOn,
      zh.managementRepresentation.content,
      zh.managementRepresentation.register,
    ]) {
      expect(html).toContain(text)
    }
  })
})

test('可靠性显示为管理层说明 (中英文)', () => {
  expect(en.data.reliabilities.management_representation).toBe('Management representation')
  expect(zh.data.reliabilities.management_representation).toBe('管理层说明')
})
