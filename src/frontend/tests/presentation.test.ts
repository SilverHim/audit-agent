// 判断呈递在前端的判断 (lib/presentation) 的测试, bun test 运行. 数据是合成的: 只填用到的字段.
import { describe, expect, test } from 'bun:test'
import type { ReviewTaskView, TaskPresentation } from '../src/client/types.gen'
import {
  cardLayout,
  needsPresentation,
  presentationFor,
  presentationKey,
  showSideProposals,
} from '../src/lib/presentation'

function view(taskId: string, status: 'pending' | 'decided', target: 'judgment' | 'run' | 'exception') {
  const targets = {
    judgment: { id: 'J-1', conclusion: 'synthetic', subject_id: 'BA-01' },
    run: { id: 'RUN-REV-02-1', run_no: 1 },
    exception: { id: 'X-1', rule_id: 'R01' },
  }
  return { task: { id: taskId, status }, target: targets[target] } as unknown as ReviewTaskView
}

function presented(taskId: string, reviewer: boolean) {
  const side = { conclusion: 'synthetic', reasoning_chain: [], proposed_action: null }
  return {
    task: { id: taskId },
    sides: reviewer ? { generator: side, reviewer: side } : { generator: side },
  } as unknown as TaskPresentation
}

describe('needsPresentation', () => {
  test('挂载后第一次要呈递, 同一条不再呈递 (StrictMode 的第二次 effect, 重新渲染)', () => {
    expect(needsPresentation(null, 'ENG-1', 'T-1')).toBe(true)
    expect(needsPresentation(presentationKey('ENG-1', 'T-1'), 'ENG-1', 'T-1')).toBe(false)
  })

  test('换一条任务 (工作台决定或稍后处理后显示下一条) 再呈递', () => {
    expect(needsPresentation(presentationKey('ENG-1', 'T-1'), 'ENG-1', 'T-2')).toBe(true)
    expect(needsPresentation(presentationKey('ENG-1', 'T-1'), 'ENG-2', 'T-1')).toBe(true)
  })
})

describe('presentationFor', () => {
  test('只用这条任务自己的呈递视图; 换任务时上一条的不拿来显示', () => {
    const data = presented('T-1', true)
    expect(presentationFor(data, 'T-1')).toBe(data)
    expect(presentationFor(data, 'T-2')).toBeUndefined()
    expect(presentationFor(undefined, 'T-1')).toBeUndefined()
  })
})

describe('cardLayout', () => {
  test('目标是执行记录或例外: 失败与规则发现卡', () => {
    expect(cardLayout(view('T-RUN', 'pending', 'run'), undefined)).toBe('target')
    expect(cardLayout(view('T-X', 'pending', 'exception'), presented('T-X', false))).toBe('target')
  })

  test('争议任务的呈递回来后 (有复核方) 两方并排; 还没回来时为原来的判断卡', () => {
    expect(cardLayout(view('T-1', 'pending', 'judgment'), presented('T-1', true))).toBe('dispute')
    expect(cardLayout(view('T-1', 'pending', 'judgment'), undefined)).toBe('judgment')
  })

  test('只有生成方 (normal 任务) 为判断卡', () => {
    expect(cardLayout(view('T-1', 'pending', 'judgment'), presented('T-1', false))).toBe('judgment')
  })
})

describe('showSideProposals', () => {
  test('collab 打开且任务待决时省掉 (决定卡已并排显示两方的建议)', () => {
    expect(showSideProposals(true, view('T-1', 'pending', 'judgment'))).toBe(false)
  })

  test('已决定的任务没有决定卡, 两栏照常显示', () => {
    expect(showSideProposals(true, view('T-1', 'decided', 'judgment'))).toBe(true)
  })

  test('collab 关闭时决定卡没有两方的建议, 两栏照常显示', () => {
    expect(showSideProposals(false, view('T-1', 'pending', 'judgment'))).toBe(true)
  })
})
