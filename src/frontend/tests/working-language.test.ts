// 工作语言在前端的判断 (lib/working-language, 需求 R339) 与对话流里的修改记录, bun test 运行. 数据是合成的: 只填用到的字段
import { describe, expect, test } from 'bun:test'
import type { EngagementOverview, TrailEntry } from '../src/client/types.gen'
import { buildTimeline } from '../src/lib/timeline'
import { currentLanguage, generatedNote, runLanguage } from '../src/lib/working-language'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

function overview(working: 'zh' | 'en' | undefined): EngagementOverview {
  const runs = { 'RUN-REV-02-1': 'en', 'RUN-REV-02-2': 'zh' }
  return { engagement: { working_language: working }, run_languages: runs } as unknown as EngagementOverview
}

describe('生成语言的标注', () => {
  test('当前工作语言: 项目的值, 之前建的项目 (没有这一项) 为 en', () => {
    expect(currentLanguage(overview('zh'))).toBe('zh')
    expect(currentLanguage(overview(undefined))).toBe('en')
  })

  test.each([
    ['zh', 'RUN-REV-02-1', 'en'], // 改为中文之前的执行: 以英文生成
    ['zh', 'RUN-REV-02-2', null], // 改之后的执行与当前相同: 不标
    ['en', 'RUN-REV-02-2', 'zh'],
    [undefined, 'RUN-REV-02-1', null], // 旧项目 (en) 的旧执行 (en)
    ['zh', 'RUN-OTHER-1', null], // 查不到执行: 不知道, 不标
    ['zh', null, null],
  ])('工作语言 %p, 执行 %p → %p', (working, runId, expected) => {
    const view = overview(working as 'zh' | 'en' | undefined)
    expect(generatedNote(runLanguage(view, runId), view)).toBe(expected as 'zh' | 'en' | null)
  })

  test('报告稿自带的语言照同一规则; 总览还没取到时不标', () => {
    expect(generatedNote('en', overview('zh'))).toBe('en')
    expect(generatedNote('en', undefined)).toBeNull()
    expect([zh.workingLanguage.generatedIn.en, en.workingLanguage.generatedIn.zh]).toEqual([
      '以英文生成',
      'Generated in Chinese',
    ])
  })
})

describe('对话流里改工作语言的记录', () => {
  const trail = (payload: object) =>
    ({
      id: 'TR-1',
      engagement_id: 'ENG-1',
      kind: 'decision',
      actor: 'U-1',
      summary: '工作语言 en → zh',
      created_at: '2026-10-21T01:00:00Z',
      payload,
    }) as TrailEntry

  test('decisions.language_trail 的留痕成为一行事件 (改前, 改后), 不按 "对象 决定" 拆摘要', () => {
    const [item] = buildTimeline([trail({ action: 'working_language', from: 'en', to: 'zh' })], false)
    expect(item).toEqual({ kind: 'language', key: 'TR-1', at: '2026-10-21T01:00:00Z', from: 'en', to: 'zh' })
    expect(zh.workingLanguage.changedEvent(zh.workingLanguage.names.en, zh.workingLanguage.names.zh)).toBe(
      '工作语言由英文改为中文',
    )
  })
})
