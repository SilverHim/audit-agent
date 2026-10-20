// 浏览器端到端走查的驱动 (需求 R345): Playwright 驱动 Chromium, 以审计师身份在页面上从注册登录走到签字归档.
// 只经页面操作推进 (点击, 填表); 读数 (停在哪, 有哪些待决任务) 经接口核对. 每步截图, 记浏览器控制台的报错与失败的
// 网络请求; 每步有超时, 超时记 "卡住". 页面上出现驱动不认得的状态时记为问题并截图, 不硬闯.
// 审计师的决定取 R311 的演示决定表 (由 tests/e2e/test_browser_walk.py 写进配置), 按状态走的做法同
// tests/test_demo_walkthrough_live.py 的 drive.
//
// 用法: node tests/e2e/walk.mjs <配置 JSON 的路径>; 结果写到配置的 out_dir/results.json. Playwright 取
// E2E_NODE_MODULES (缺省 /opt/node-tools/node_modules), 浏览器取 PLAYWRIGHT_BROWSERS_PATH.
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(path.join(process.env.E2E_NODE_MODULES ?? '/opt/node-tools/node_modules', 'x.js'))
const { chromium } = require('playwright')

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'))
const { base, timeouts } = config
const SHOTS = config.shots_dir
fs.mkdirSync(SHOTS, { recursive: true })

const result = {
  eid: null,
  owner: null,
  reached_archive: false,
  gates: [],
  fin: null,
  candidate: null,
  failures: [], // [程序, 对象, 次数]
  troubled: {}, // 任务 ID → [程序, 对象, 类型]
  decisions: [], // [任务 ID, 决定]
  steps: [], // {no, name, url, shot, console, failed, ms}
  issues: [], // {step, page, symptom, shot, expected}
}
const failures = new Map()
let token = null
let current = null // 当前步骤: 浏览器事件记到它上面

class Stuck extends Error {}

function slug(text) {
  return text.replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '').slice(0, 60)
}

function issue(symptom, expected, extra = {}) {
  result.issues.push({ step: current?.no ?? 0, page: current?.url ?? '', symptom, shot: current?.shot ?? '', expected, ...extra })
}

async function waitFor(check, ms, label) {
  const until = Date.now() + ms
  for (;;) {
    const value = await check()
    if (value) return value
    if (Date.now() > until) throw new Stuck(`${label}: ${Math.round(ms / 1000)} 秒内没有等到`)
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
}

async function api(pathname) {
  const response = await fetch(`${base}/api/v1${pathname}`, { headers: { Authorization: `Bearer ${token}` } })
  const body = await response.json().catch(() => null)
  return { status: response.status, body }
}

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: config.locale })
const page = await context.newPage()
page.setDefaultTimeout(timeouts.action_ms)
// 开发服务第一次打开页面要预构建依赖, 负载高时较慢; 导航等到 DOM 就绪即可, 之后各步等自己要的元素
page.setDefaultNavigationTimeout(timeouts.nav_ms)
const goto = (url) => page.goto(url, { waitUntil: 'domcontentloaded' })
page.on('console', (message) => {
  // 请求失败另由下面两个监听按地址与错误码记 (含预期的 404 NOT_AT_GATE 的筛除), 控制台的同一条不重复记
  if (message.type() !== 'error' || !current || message.text().startsWith('Failed to load resource')) return
  current.console.push(message.text().slice(0, 500))
})
page.on('pageerror', (error) => current?.console.push(`pageerror: ${String(error).slice(0, 500)}`))
page.on('requestfailed', (request) => {
  const reason = request.failure()?.errorText ?? ''
  if (current && !reason.includes('ERR_ABORTED')) current.failed.push(`${request.method()} ${request.url()} ${reason}`)
})
page.on('response', async (response) => {
  const target = current
  if (response.status() < 400 || !target) return
  const body = await response.text().catch(() => '')
  const code = body.match(/"code"\s*:\s*"([A-Z_]+)"/)?.[1] ?? ''
  target.failed.push(`${response.request().method()} ${new URL(response.url()).pathname} ${response.status()} ${code}`)
})

// 一步: 执行 fn, 截图, 记下本步的控制台报错与失败请求; 超时 (Stuck) 与异常都记为问题, 由调用方决定是否停下
async function step(name, fn) {
  const no = result.steps.length + 1
  current = { no, name, url: '', shot: '', console: [], failed: [], ms: 0 }
  result.steps.push(current)
  const started = Date.now()
  let ok = true
  try {
    await fn()
  } catch (error) {
    ok = false
    const stuck = error instanceof Stuck || error?.name === 'TimeoutError'
    current.error = String(error?.message ?? error).slice(0, 800)
    issue(stuck ? `卡住: ${current.error}` : `出错: ${current.error}`, `${name} 能完成`)
  }
  current.ms = Date.now() - started
  current.url = new URL(page.url()).pathname
  current.shot = `${String(no).padStart(3, '0')}-${slug(name)}.png`
  await page.screenshot({ path: path.join(SHOTS, current.shot), fullPage: true }).catch(() => {})
  for (const item of result.issues.filter((i) => i.step === no)) Object.assign(item, { page: current.url, shot: current.shot })
  for (const text of current.console) issue(`控制台报错: ${text}`, '没有控制台报错')
  for (const text of current.failed) issue(`请求失败: ${text}`, '页面发出的请求都成功')
  return ok
}

// 本步中途的截图 (如点 "记录决定" 之前填好的表单), 文件名为本步的编号加 suffix
async function snap(suffix) {
  const file = `${String(current.no).padStart(3, '0')}-${slug(current.name)}-${suffix}.png`
  await page.screenshot({ path: path.join(SHOTS, file), fullPage: true }).catch(() => {})
}

async function click(locator) {
  await locator.scrollIntoViewIfNeeded()
  await locator.click()
}

// 单选卡片: input 视觉上隐藏, 点它的 label
async function choose(id) {
  await click(page.locator(`label[for="${id}"]`))
}

async function selectById(id, value) {
  await page.locator(`[id="${id}"]`).selectOption(String(value))
}

async function fillById(id, value) {
  await page.locator(`[id="${id}"]`).fill(String(value))
}

// 比例 (0.05) 按百分比填 (5), 去掉浮点误差
function percent(fraction) {
  return String(Number((fraction * 100).toFixed(6)))
}

const recordButton = () => page.getByRole('button', { name: config.labels.record, exact: true }).last()

// ── 读状态 ──

async function overview() {
  const { status, body } = await api(`/engagements/${result.eid}`)
  if (status !== 200) throw new Error(`GET 总览 ${status}`)
  return body
}

async function summary() {
  return (await api(`/engagements/${result.eid}/data/summary`)).body
}

// 后台作业 (图的执行与数据作业) 都停下; 返回停下时的总览
async function idle(label) {
  return waitFor(
    async () => {
      const view = await overview()
      const data = await summary()
      return !view.running && (data?.processing ?? 0) === 0 ? view : null
    },
    timeouts.idle_ms,
    label,
  )
}

// ── 注册与建项目 ──

async function register() {
  await goto(`${base}/register`)
  await fillById('register-name', config.user.full_name)
  await fillById('register-username', config.user.username)
  await fillById('register-password', config.user.password)
  await fillById('register-confirm', config.user.password)
  await click(page.getByRole('button', { name: config.labels.register }))
  await page.waitForURL(/\/engagements\/?$/)
  token = await page.evaluate(() => localStorage.getItem('access_token'))
  if (!token) throw new Error('注册后没有登录令牌')
  result.owner = (await api('/users/me')).body?.id ?? null
}

async function createEngagement() {
  await click(page.getByRole('button', { name: config.labels.newEngagement }).first())
  await page.locator('#new-client').waitFor()
  const client = await page.locator('#new-client option:checked').innerText()
  if (!client.includes(config.client_name)) issue(`新建项目默认的客户是 ${client}`, `默认选 ${config.client_name}`)
  await click(page.getByRole('button', { name: config.labels.create }))
  await page.waitForURL(/\/engagements\/[^/]+$/)
  result.eid = decodeURIComponent(new URL(page.url()).pathname.split('/').pop())
  await idle('建项目后的承接')
}

// ── 数据管理页 ──

async function importDemo(stage) {
  await goto(`${base}/engagements/${result.eid}/data`)
  const card = page.getByRole('region', { name: config.labels.demoTitle })
  await card.waitFor()
  const button = card.getByRole('button').first()
  const label = await button.innerText()
  if (!label.includes(config.labels.demoStages[stage] ?? stage)) issue(`导入按钮写的是 "${label}"`, `按钮的阶段为 ${stage}`)
  await click(button)
  await waitFor(async () => (await summary())?.demo?.stages_done?.includes(stage), timeouts.idle_ms, `导入 ${stage}`)
}

async function confirmReady() {
  await goto(`${base}/engagements/${result.eid}/data`)
  await click(page.getByRole('button', { name: config.labels.ready }).first())
  const dialog = page.getByRole('dialog')
  if (await dialog.isVisible().catch(() => false)) {
    await page.locator('#data-ready-reason').fill(config.ready_reason)
    await click(dialog.getByRole('button', { name: config.labels.readyConfirm }))
  }
  await waitFor(async () => !(await overview()).awaiting_data, timeouts.idle_ms, '确认数据已齐')
}

// ── 闸门 (表单视图) ──

async function decideGate(gate) {
  const view = (await api(`/engagements/${result.eid}/gate`)).body
  await goto(`${base}/engagements/${result.eid}/gate`)
  const verdictId = (verdict) => `gate-${gate}-${verdict}`
  await page.locator(`[id="${verdictId('approve')}"]`).waitFor({ state: 'attached' })
  const seen = result.gates.filter((g) => g === gate).length
  let reason = ''
  if (gate === 'PLN-01/03') {
    await choose(verdictId('approve'))
    const m = config.materiality
    await selectById('pln-basis', m.basis)
    await fillById('pln-pct', percent(m.pct))
    await fillById('pln-perf', percent(m.performance_pct))
    await fillById('pln-trivial', percent(m.clearly_trivial_pct))
  } else if (gate === 'PLN-15' && seen === 0) {
    await choose(verdictId('reject'))
    reason = config.plan_reject_reason
  } else if (gate === 'PLN-15') {
    await choose(verdictId('approve'))
    const expert = page.getByLabel(config.labels.noExpert)
    if (await expert.count()) await expert.check()
    else issue('计划批准卡片没有 "不需要审计师的专家" 的确认', '批准时可确认三项 (需求 R315)')
  } else if (gate === 'FIN-01') {
    await choose(verdictId('approve'))
    result.candidate = view?.material?.schedule?.candidate_opinion ?? null
    result.fin = config.fin_policy === 'candidate' ? result.candidate : config.fin_policy
    await selectById('fin-opinion', result.fin)
    reason = config.fin_reason.replace('{candidate}', result.candidate ?? '')
  } else if (gate === 'RPT-01' && seen === 0) {
    await choose(verdictId('reject'))
    await page.getByLabel(config.labels.section).first().selectOption(config.rpt_reject.section)
    await page.getByPlaceholder(config.labels.comment).first().fill(config.rpt_reject.comment)
    reason = config.rpt_reject.reason
  } else {
    await choose(verdictId('approve'))
  }
  if (reason) await fillById(`gate-reason-${gate}`, reason)
  await snap('form')
  const button = recordButton()
  if (await button.isDisabled()) throw new Error(`${gate} 的记录按钮不可点`)
  await click(button)
  result.gates.push(gate)
  await waitFor(
    async () => {
      const decided = (await api(`/engagements/${result.eid}/gate`)).body
      return !decided?.target_id || decided.target_id !== view?.target_id || decided.target_version !== view?.target_version
        ? true
        : (await overview()).running
    },
    timeouts.action_ms,
    `${gate} 的决定`,
  )
}

// ── 复核任务 (表单视图), 决定同 R311 的 review_body 与 dispute_fields ──

function subjectOf(view) {
  const { task, target } = view
  if (task.kind === 'failed') {
    // 整条失败的计划与报告按修订计 (修订号为其闸门已决定的次数加一, 同 R311 驱动的 REVISED_BY)
    const gate = config.revised_by[task.procedure_id]
    if (!task.subject_id && gate) return `r${result.gates.filter((g) => g === gate).length + 1}`
    return task.subject_id ?? ''
  }
  if (target.subject_id) return target.subject_id
  return target.detail?.entry_no ?? target.source_ids?.[0] ?? ''
}

function expectedFor(subject) {
  return config.table[subject] ?? { decision: 'confirm', reason: null, draft: null, conclusion: null }
}

function reviewPlan(view) {
  const { task } = view
  const subject = subjectOf(view)
  if (task.kind !== 'normal') result.troubled[task.id] = [task.procedure_id, subject, task.kind]
  const expected = expectedFor(subject)
  if (task.kind === 'failed') {
    const key = `${task.procedure_id} ${subject}`
    failures.set(key, (failures.get(key) ?? 0) + 1)
    if (failures.get(key) > 1) return { error: `${task.procedure_id} ${subject || '(整条)'} 重跑后又失败` }
    return { decision: 'rerun', reason: config.rerun_reason }
  }
  if (task.kind === 'normal' || task.kind === 'unattributed') {
    if (expected.decision === 'misstatement') return { decision: 'misstatement', reason: expected.reason, draft: expected.draft }
    if (expected.decision === 'override' || task.kind === 'unattributed') {
      const conclusion = expected.conclusion ?? config.confirm_conclusion
      return { decision: 'override', reason: expected.reason ?? conclusion, conclusion }
    }
    return { decision: 'confirm' }
  }
  if (task.kind === 'dispute') {
    const a6 = view.draft_rules?.a6_upheld
    const want = expected.decision === 'misstatement'
    if ((view.allowed_resolutions ?? []).includes('a6_upheld') && expected.decision !== 'override') {
      if (want && a6 === 'required') return { decision: 'dispute_resolution', choice: 'a6_upheld', reason: expected.reason, draft: expected.draft }
      if (!want && a6 !== 'required') return { decision: 'dispute_resolution', choice: 'a6_upheld', reason: config.confirm_conclusion }
    }
    const conclusion = expected.conclusion ?? (want ? expected.reason : config.confirm_conclusion)
    return {
      decision: 'dispute_resolution',
      choice: 'neither',
      conclusion,
      reason: expected.reason ?? conclusion,
      draft: want ? expected.draft : null,
      attachRule: view.draft_rules?.neither ?? 'optional',
    }
  }
  return { error: `驱动不认得的任务类型 ${task.kind}` }
}

async function fillDraft(id, draft) {
  await selectById(`ms-type-${id}`, draft.type)
  await fillById(`ms-account-${id}`, draft.account)
  await selectById(`ms-assertion-${id}`, draft.assertion)
  await fillById(`ms-amount-${id}`, (draft.amount_cents / 100).toFixed(2))
  await selectById(`ms-direction-${id}`, draft.direction)
}

async function decideTask(view) {
  const id = view.task.id
  const plan = reviewPlan(view)
  if (plan.error) {
    issue(plan.error, '每个对象至多重跑一次, 任务类型在决定表的范围内')
    throw new Stuck(plan.error)
  }
  result.decisions.push([id, plan.decision])
  await goto(`${base}/engagements/${result.eid}/reviews/${encodeURIComponent(id)}`)
  await recordButton().waitFor()
  if (view.allowed_decisions.length > 1 || plan.decision !== 'dispute_resolution') {
    if (!view.allowed_decisions.includes(plan.decision)) throw new Error(`${id} 不能选 ${plan.decision} (可选 ${view.allowed_decisions})`)
    await choose(`decision-${id}-${plan.decision}`)
  }
  if (plan.choice) await choose(`dispute-${id}-${plan.choice}`)
  if (plan.draft && plan.decision === 'dispute_resolution') {
    const attach = page.locator(`[id="attach-ms-${id}"]`)
    if ((await attach.count()) && !(await attach.isChecked())) await attach.check()
  }
  if (plan.conclusion && plan.decision !== 'confirm') await fillById(`conclusion-${id}`, plan.conclusion)
  if (plan.draft) await fillDraft(id, plan.draft)
  if (plan.reason) await fillById(`reason-${id}`, plan.reason)
  await snap('form')
  const button = recordButton()
  if (await button.isDisabled()) throw new Error(`${id} 的记录按钮不可点`)
  await click(button)
  await waitFor(
    async () => {
      const pending = (await api(`/engagements/${result.eid}/review-tasks?status=pending`)).body ?? []
      return !pending.some((item) => item.task.id === id)
    },
    timeouts.action_ms,
    `${id} 的决定`,
  )
}

// ── 主循环: 每次停下按当时的状态办 (同 R311 的 drive) ──

async function main() {
  if (!(await step('注册并登录', register))) return
  if (!(await step('新建演示项目', createEngagement))) return
  let readyPosted = false
  let last = ''
  let repeats = 0
  for (let n = 0; n < config.max_steps; n += 1) {
    const view = await overview()
    if (view.run_error) {
      issue(`后台出错: ${view.run_error}`, '后台没有错误')
      return
    }
    if (view.engagement.phase === 'Archived') {
      result.reached_archive = true
      // 报告页上签字的那一稿带 "已签字归档" 标记; 等它出来再截图 (打开页面时内容还没渲染)
      await step('归档后的报告页', async () => {
        await goto(`${base}/engagements/${result.eid}/report`)
        await page.getByText(config.labels.signed).first().waitFor()
      })
      return
    }
    const pending = (await api(`/engagements/${result.eid}/review-tasks?status=pending`)).body ?? []
    const suggested = (await summary())?.demo?.suggested ?? null
    let name
    let action
    if (pending.length) {
      const task = pending[0]
      name = `复核 ${task.task.procedure_id} ${subjectOf(task)}`
      action = () => decideTask(task)
    } else if (suggested) {
      name = `导入演示资料 ${suggested}`
      action = () => importDemo(suggested)
    } else if (view.awaiting_data) {
      if (readyPosted) {
        issue(`确认数据已齐之后仍在等数据: ${view.missing_tables}`, '确认后进入计划')
        return
      }
      readyPosted = true
      name = '确认数据已齐'
      action = confirmReady
    } else if (view.current_gate) {
      name = `闸门 ${view.current_gate}`
      action = () => decideGate(view.current_gate)
    } else {
      await step('流程停住', async () => goto(`${base}/engagements/${result.eid}`))
      issue('没有待决任务, 不等数据也不在闸门, 流程停住', '页面给出下一步')
      return
    }
    repeats = name === last ? repeats + 1 : 0
    last = name
    if (repeats >= 3) {
      issue(`同一步连做 4 次仍没有进展: ${name}`, '做完一步流程往前走')
      return
    }
    if (
      !(await step(name, async () => {
        await action()
        await idle(`${name} 之后的后台执行`)
      }))
    )
      return
  }
  issue(`${config.max_steps} 步内未到归档`, '走到归档')
}

try {
  await main()
} finally {
  result.failures = [...failures.entries()].map(([key, n]) => [...key.split(' ', 2), n])
  fs.writeFileSync(path.join(config.out_dir, 'results.json'), JSON.stringify(result, null, 2))
  await browser.close()
}
