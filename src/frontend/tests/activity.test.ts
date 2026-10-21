// 系统忙时的说明 (lib/activity, 需求 R362) 的测试, bun test 运行: 当前活动的文案, 变灰按钮旁的说明, 已用时间与
// 进度停滞, 失败提示. 数据是合成的: 总览只填用到的字段, 活动按后端 R361 的形状 (外勤演示资料 19 份)
import { describe, expect, test } from 'bun:test'
import type { Activity, EngagementOverview } from '../src/client/types.gen'
import {
  activityCurrent,
  activityText,
  busyText,
  currentActivities,
  failureKey,
  failureText,
  timingText,
} from '../src/lib/activity'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const START = '2026-10-21T08:03:34Z'
const INTAKE: Activity = {
  kind: 'document_intake',
  actor: 'A3',
  description: '识别与抽取上传的文件',
  done: 7,
  total: 19,
  current: 'CON-X02.pdf',
  started_at: START,
  updated_at: START,
}

function overview(patch: Partial<EngagementOverview>): EngagementOverview {
  return { running: false, activities: [], last_failure: null, ...patch } as EngagementOverview
}

describe('当前活动与文案', () => {
  test('空闲时没有活动, 按钮旁不写说明', () => {
    expect(currentActivities(undefined)).toEqual([])
    expect(currentActivities(overview({}))).toEqual([])
    expect(busyText(overview({}), zh)).toBeNull()
  })

  test('逐份处理: 执行者在做什么与 x/y, 当前文件另列; 按钮旁写明完成后可继续', () => {
    const busy = overview({ running: true, activities: [INTAKE] })
    expect(currentActivities(busy)).toEqual([INTAKE])
    expect(activityText(INTAKE, zh)).toBe('数据管理智能体正在识别文件 7/19')
    expect(activityText(INTAKE, en)).toBe('The data agent is reading the files 7/19')
    expect(activityCurrent(INTAKE)).toBe('CON-X02.pdf')
    expect(busyText(busy, zh)).toBe('数据管理智能体正在识别文件 7/19，完成后可继续。')
  })

  test('载入演示资料说明在识别文件; 没有进度的不写 x/y', () => {
    const demo: Activity = { ...INTAKE, kind: 'demo_import', subject_id: 'fieldwork', done: 0 }
    expect(activityText(demo, zh)).toBe('数据管理智能体正在载入演示资料并识别文件 0/19')
    const sync: Activity = { ...INTAKE, kind: 'xero_sync', actor: 'system', done: null, total: null, current: null }
    expect(activityText(sync, zh)).toBe('系统正在同步 Xero 数据')
    expect(activityCurrent(sync)).toBeNull()
  })

  test('程序执行: 执行者, 程序与本阶段进度写进说明, 不另列当前对象', () => {
    const run: Activity = { ...INTAKE, kind: 'procedure', actor: 'A6', current: 'REV-02', done: 3, total: 9 }
    expect(activityText(run, zh)).toBe('外勤执行智能体正在执行 REV-02 收入截止性判断（本阶段已完成 3/9）')
    expect(activityText(run, en)).toBe(
      'The fieldwork agent is running REV-02 Revenue cut-off (3 of 9 done in this stage)',
    )
    expect(activityCurrent(run)).toBeNull()
  })

  test('认不出的种类: 中文界面照后端的描述兜底, 英文界面不显示中文描述', () => {
    const later = { ...INTAKE, kind: 'later_kind' as Activity['kind'] }
    expect(activityText(later, zh)).toBe('识别与抽取上传的文件')
    expect(activityText(later, en)).toBe('The system is working')
  })

  test('在执行而后端没列活动时只说系统在处理', () => {
    const [working] = currentActivities(overview({ running: true }))
    expect(working && activityText(working, zh)).toBe('系统正在处理')
    expect(busyText(overview({ running: true }), en)).toBe('The system is working. You can continue when it finishes.')
  })
})

describe('已用时间与进度停滞', () => {
  const at = (seconds: number) => Date.parse(START) + seconds * 1000
  const updatedAt = (seconds: number) => new Date(at(seconds)).toISOString()

  test('超过 30 秒才显示已用时间', () => {
    expect(timingText(INTAKE, at(30), zh)).toBeNull()
    expect(timingText({ ...INTAKE, updated_at: updatedAt(60) }, at(65), zh)).toBe('已用时 1 分 05 秒')
    expect(timingText({ ...INTAKE, updated_at: updatedAt(40) }, at(45), en)).toBe('45 s elapsed')
  })

  test('进度 (updated_at) 2 分钟没变提示仍在进行, 变了照常显示已用时间', () => {
    expect(timingText({ ...INTAKE, updated_at: updatedAt(60) }, at(185), zh)).toBe('仍在进行，已用时 3 分钟')
    expect(timingText({ ...INTAKE, updated_at: updatedAt(100) }, at(200), zh)).toBe('已用时 3 分 20 秒')
  })

  test('开始时间不明不显示', () => {
    expect(timingText({ ...INTAKE, started_at: '', updated_at: '' }, at(500), zh)).toBeNull()
  })
})

describe('失败提示', () => {
  test('哪类作业失败与原因原文; 关掉按种类, 对象与时刻认', () => {
    const failure = {
      kind: 'import_batch' as const,
      actor: 'A3' as const,
      reason: 'ValueError: 读不了表头',
      subject_id: 'BAT-1',
      retry: 'import_batch' as const,
      failed_at: '2026-10-21T08:05:00Z',
    }
    expect(failureText(failure, zh)).toBe('识别上传的文件失败：ValueError: 读不了表头')
    expect(failureText({ ...failure, kind: 'xero_sync', reason: 'xero_error (HTTP 503)' }, en)).toBe(
      'Xero sync failed: xero_error (HTTP 503)',
    )
    expect(failureKey(failure)).not.toBe(failureKey({ ...failure, failed_at: '2026-10-21T09:00:00Z' }))
  })
})
