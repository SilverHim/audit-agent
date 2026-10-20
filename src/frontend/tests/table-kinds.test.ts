// 账表种类与行指针来源的前端清单与后端接口 (生成的 openapi.json) 一致, bun test 运行: 数据管理页列出每种账表,
// 中英文都有账表名, 每种行指针都能点开看行 (应付账单为需求 R178 加的).
import { expect, test } from 'bun:test'
import openapi from '../openapi.json'
import { ROW_SOURCES } from '../src/components/evidence-link'
import { TABLE_KINDS } from '../src/lib/data-api'
import { en } from '../src/locales/en'
import { zh } from '../src/locales/zh'

const schemas = openapi.components.schemas
const NOT_ROWS = ['document', 'standard', 'trail', 'client_profile'] // 文件页, 准则片段, 留痕与客户画像, 不是账表的行

test('every table kind is listed and named in both languages', () => {
  const kinds = schemas.TableKind.enum
  expect(TABLE_KINDS).toEqual(kinds)
  for (const kind of kinds) {
    expect([kind, zh.data.tables[kind as keyof typeof zh.data.tables]]).toEqual([kind, expect.any(String)])
    expect([kind, en.data.tables[kind as keyof typeof en.data.tables]]).toEqual([kind, expect.any(String)])
  }
})

test('every row pointer source opens its row', () => {
  const sources = schemas.EvidencePointer.properties.source_type.enum.filter((s) => !NOT_ROWS.includes(s))
  expect([...ROW_SOURCES].sort()).toEqual([...sources].sort())
})
