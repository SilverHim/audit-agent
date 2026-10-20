// 程序名中英对照 (需求 R349) 的守卫, bun test 运行: 两份 procedureNames 的键都等于程序库的程序编号
// (后端 knowledge/procedures 下每个 YAML 的 id 行, 含未启用的). 缺项时界面退回后端的中文名, 英文界面就混进中文;
// 加了程序而漏写名字, 或程序删了而名字还在, 这里都失败
import { describe, expect, test } from 'bun:test'
import { readdirSync, readFileSync } from 'node:fs'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const LIBRARY = new URL('../../backend/knowledge/procedures/', import.meta.url)

// 各 YAML 顶层的 id (首个 "id:" 行); 读不到的记文件名, 让比对失败时看得出是哪个文件
const ids = readdirSync(LIBRARY)
  .filter((name) => name.endsWith('.yaml'))
  .map((name) => {
    const line = /^id:\s*(\S+)/m.exec(readFileSync(new URL(name, LIBRARY), 'utf8'))
    return line?.[1] ?? `${name} 没有 id 行`
  })
  .sort()

describe('程序名中英对照', () => {
  test.each([
    ['en', en.procedureNames],
    ['zh', zh.procedureNames],
  ])('%s 的键等于程序库的程序编号', (_, names) => {
    expect(Object.keys(names).sort()).toEqual(ids)
  })
})
