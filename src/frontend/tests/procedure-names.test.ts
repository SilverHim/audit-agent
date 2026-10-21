// 程序名中英对照 (需求 R349) 与人工程序说明 (需求 R357) 的守卫, bun test 运行: 两份 procedureNames 的键都等于程序库的
// 程序编号 (后端 knowledge/procedures 下每个 YAML 的 id 行, 含未启用的), 两份 manualInstructions 的键都等于其中带
// manual.instructions 的程序. 缺项时界面退回后端的中文, 英文界面就混进中文; 加了程序而漏写, 或程序删了而文案还在,
// 这里都失败
import { describe, expect, test } from 'bun:test'
import { readdirSync, readFileSync } from 'node:fs'
import { manualInstructions } from '../src/lib/review'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const LIBRARY = new URL('../../backend/knowledge/procedures/', import.meta.url)
const CJK = /[㐀-䶿一-鿿]/

// 各 YAML 顶层的 id (首个 "id:" 行; 读不到的记文件名, 让比对失败时看得出是哪个文件) 与 manual 段的 instructions
const library = readdirSync(LIBRARY)
  .filter((name) => name.endsWith('.yaml'))
  .map((name) => {
    const text = readFileSync(new URL(name, LIBRARY), 'utf8')
    const id = /^id:\s*(\S+)/m.exec(text)?.[1] ?? `${name} 没有 id 行`
    return { id, instructions: /^\s+instructions:\s*(.+)$/m.exec(text)?.[1] }
  })
const ids = library.map((p) => p.id).sort()
const manual = library.filter((p) => p.instructions !== undefined)

describe('程序名中英对照', () => {
  test.each([
    ['en', en.procedureNames],
    ['zh', zh.procedureNames],
  ])('%s 的键等于程序库的程序编号', (_, names) => {
    expect(Object.keys(names).sort()).toEqual(ids)
  })
})

describe('人工程序说明随界面语言', () => {
  test.each([
    ['en', en.manualInstructions],
    ['zh', zh.manualInstructions],
  ])('%s 的键等于程序库中带 manual.instructions 的程序', (_, texts) => {
    expect(Object.keys(texts).sort()).toEqual(manual.map((p) => p.id).sort())
  })

  test('英文界面不用后端的中文说明, 中文界面为中文; 程序库外的程序退回后端的说明', () => {
    expect(manual.length).toBeGreaterThan(0)
    for (const { id, instructions } of manual) {
      expect(manualInstructions(en, id, instructions)).not.toMatch(CJK)
      expect(manualInstructions(zh, id, instructions)).toMatch(CJK)
    }
    expect(manualInstructions(en, 'TST-M01', 'Obtain the signed letter')).toBe('Obtain the signed letter')
  })
})
