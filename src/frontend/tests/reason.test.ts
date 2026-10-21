// 人填的理由的显示 (lib/reason, 需求 R360) 的测试, bun test 运行: 空理由显示 "未填理由", 填了的照原文.
import { describe, expect, test } from 'bun:test'
import { reasonText } from '../src/lib/reason'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

describe('reasonText', () => {
  test('没填 (null, 缺省, 空串或只有空白) 显示 "未填理由", 不加引号', () => {
    for (const reason of [null, undefined, '', '  \n']) {
      expect(reasonText(reason, zh)).toBe('未填理由')
      expect(reasonText(reason, en, true)).toBe('No reason given')
    }
  })

  test('填了的照原文, quoted 时加引号', () => {
    expect(reasonText('Cut-off confirmed with the bank', en)).toBe('Cut-off confirmed with the bank')
    expect(reasonText('合同已核对', zh, true)).toBe('“合同已核对”')
  })
})
