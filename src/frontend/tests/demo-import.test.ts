// 数据管理页 "导入演示资料" 的按钮与上次结果 (lib/demo-import.ts) 的测试, bun test 运行. 文件名取自合成的 Xero 演示资料.
import { describe, expect, test } from 'bun:test'
import type { DemoImportResult, DemoSummary } from '../src/client/types.gen'
import { demoButton, demoResultText } from '../src/lib/demo-import'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const SUMMARY: DemoSummary = { available: true, stages_done: [], suggested: null, last: null }

describe('demoButton', () => {
  test('没有演示资料 (Numbat, 新客户) 不显示按钮', () => {
    expect(demoButton({ ...SUMMARY, available: false }, zh)).toBeNull()
    expect(demoButton(undefined, zh)).toBeNull()
  })

  test.each([
    ['acceptance', '导入演示资料：承接文件'],
    ['fieldwork', '导入演示资料：外勤账表与文件'],
    ['statements', '导入演示资料：待审财报'],
  ] as const)('按后端给的阶段 %s 一键导入', (stage, label) => {
    expect(demoButton({ ...SUMMARY, suggested: stage }, zh)).toEqual({ stage, label, hint: null })
  })

  test('不在要导入的位置 (或这一阶段已导入过): 按钮在而不可点, 附说明', () => {
    expect(demoButton({ ...SUMMARY, stages_done: ['acceptance'] }, en)).toEqual({
      stage: null,
      label: 'Load demo data',
      hint: 'Nothing to load at the current point of the audit.',
    })
  })
})

describe('demoResultText', () => {
  const fieldwork: DemoImportResult = {
    stage: 'fieldwork',
    status: 'imported',
    tables: Array(10).fill('accounts'),
    uploaded: Array(19).fill('CON-X01.pdf'),
    retyped: ['ridgeway_order_CRM08-12.pdf'],
    needs_manual: [],
    data_version: 10,
  }

  test('写了几版账表, 上传几份; 按清单改了类型的列出文件名', () => {
    expect(demoResultText(fieldwork, zh)).toBe(
      '外勤账表与文件：写入账表 10 版，上传文件 19 份；按清单指定类型：ridgeway_order_CRM08-12.pdf',
    )
    expect(demoResultText({ ...fieldwork, retyped: [], needs_manual: ['CON-X02.pdf'] }, en)).toBe(
      'fieldwork ledgers and files: 10 ledger versions written, 19 files uploaded; needs attention: CON-X02.pdf',
    )
  })

  test('没写账表的阶段 (承接) 只说上传了几份', () => {
    const acceptance: DemoImportResult = {
      stage: 'acceptance',
      status: 'imported',
      tables: [],
      uploaded: ['a.pdf', 'b.pdf'],
    }
    expect(demoResultText(acceptance, zh)).toBe('承接文件：上传文件 2 份')
    expect(demoResultText(acceptance, en)).toBe('acceptance files: 2 files uploaded')
  })

  test('作业中途出错: 写错误类别, 提示再导入一次接着做', () => {
    const failed: DemoImportResult = { stage: 'fieldwork', status: 'failed', error: 'OSError', data_version: 5 }
    expect(demoResultText(failed, zh)).toBe('外勤账表与文件：没做完（OSError），再导入一次接着做')
    expect(demoResultText(failed, en)).toBe(
      'fieldwork ledgers and files: stopped before finishing (OSError); load again to continue',
    )
  })

  test('什么都没写的一次说明已导入过', () => {
    expect(demoResultText({ stage: 'acceptance', status: 'already_imported' }, zh)).toBe('承接文件：已导入过，没有写入')
  })

  test('出错后再导入, 没有新写的表与文件: 说上次没做完的已做完, 不说上传了 0 份', () => {
    const resumed: DemoImportResult = { stage: 'acceptance', status: 'imported', tables: [], uploaded: [], retyped: [] }
    expect(demoResultText(resumed, zh)).toBe('承接文件：上次没做完的已做完')
    expect(demoResultText(resumed, en)).toBe('acceptance files: finished what the last load left undone')
  })
})
