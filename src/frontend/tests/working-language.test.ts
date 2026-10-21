// 工作语言在前端的判断 (lib/working-language, 需求 R339) 与对话流里的修改记录, bun test 运行. 数据是合成的: 只填用到的字段
import { describe, expect, test } from 'bun:test'
import type { EngagementOverview, TrailEntry } from '../src/client/types.gen'
import { buildTimeline } from '../src/lib/timeline'
import { contentLanguage, currentLanguage, generatedNote } from '../src/lib/working-language'
import { zh } from '../src/locales/zh'

// 项目当前为 zh; 总览的 run_languages 只列生成语言与之不同的执行 (改为中文之前的执行)
function overview(working: 'zh' | 'en' | undefined): EngagementOverview {
  return {
    engagement: { working_language: working },
    run_languages: { 'RUN-REV-02-1': 'en' },
  } as unknown as EngagementOverview
}

describe('生成语言的标注', () => {
  test('当前工作语言: 项目的值, 之前建的项目 (没有这一项) 为 en', () => {
    expect(currentLanguage(overview('zh'))).toBe('zh')
    expect(currentLanguage(overview(undefined))).toBe('en')
  })

  test.each([
    ['RUN-REV-02-1', 'en'], // 列出的执行: 以英文生成
    ['RUN-REV-02-2', null], // 没列出 (与当前工作语言相同): 不标
    [null, null], // 没有 run_id (迁移前的报告稿): 不标
  ])('执行 %p → %p', (runId, expected) => {
    expect(generatedNote(overview('zh'), runId)).toBe(expected as 'en' | null)
  })

  test('总览还没取到时不标; 文案同验收 4', () => {
    expect(generatedNote(undefined, 'RUN-REV-02-1')).toBeNull()
    expect([zh.workingLanguage.generatedIn.en, zh.workingLanguage.generatedIn.zh]).toEqual(['以英文生成', '以中文生成'])
  })
})

describe('生成内容的 lang 属性 (需求 R340)', () => {
  test.each([
    ['RUN-REV-02-1', 'en'], // 生成语言与当前不同: 取执行的
    ['RUN-REV-02-2', 'zh'], // 没列出: 与当前工作语言相同
    [null, 'en'], // 没有 run_id (迁移前的报告稿): 那时的输出都是英文
  ])('执行 %p → %p', (runId, expected) => {
    expect(contentLanguage(overview('zh'), runId)).toBe(expected as 'zh' | 'en')
  })
})

describe('对话流里改工作语言的记录', () => {
  test('decisions.language_trail 的留痕成为一行事件 (改前, 改后), 不按 "对象 决定" 拆摘要', () => {
    const entry = {
      id: 'TR-1',
      engagement_id: 'ENG-1',
      kind: 'decision',
      actor: 'U-1',
      summary: '工作语言 en → zh',
      created_at: '2026-10-21T01:00:00Z',
      payload: { action: 'working_language', from: 'en', to: 'zh' },
    } as TrailEntry
    const [item] = buildTimeline([entry], false)
    expect(item).toEqual({ kind: 'language', key: 'TR-1', at: '2026-10-21T01:00:00Z', from: 'en', to: 'zh' })
  })
})
