// 浏览器端到端走查的驱动 (需求 R345): Playwright 驱动 Chromium, 以审计师身份在页面上从注册登录走到签字归档.
// 只经页面操作推进 (点击, 填表); 读数 (停在哪, 有哪些待决任务) 经接口核对. 每步截图, 记浏览器控制台的报错与失败的
// 网络请求; 每步有超时, 超时记 "卡住". 页面上出现驱动不认得的状态时记为问题并截图, 不硬闯.
// 审计师的决定取 R311 的演示决定表 (由 tests/e2e/test_browser_walk.py 写进配置), 按状态走的做法同
// tests/test_demo_walkthrough_live.py 的 drive. 错报闭环另有两步 (需求 R356): FIN-08 人工任务在人工任务页填管理层回复
// 登记表, 等更正时在总览的更正卡片上提交更正通知 (勾选待更正的错报与配置的更正分录). 配了 activity 时 (需求 R362)
// 在载入那一阶段的演示资料后核对系统忙时的说明: 状态条的逐份进度, 补交卡片按钮旁的原因, 处理完不刷新即可用.
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
  corrections: [], // [种类, 错报 ID, 更正分录] (功能 misstatement 打开时, 等更正处提交的更正通知)
  evidence_followup: { requested: false, received: false, unavailable: false },
  workpapers: null, // 档案页预览、编辑、历史与下载, 复用 mock 走通.
  currency: null, // 币种确认与锁定后的显示核对 (R366), 共用原有走通.
  withdrawal_totals: [], // Issue #67: 初次同意, 撤回后改判不是错报, 再撤回并同意的未更正合计
  activity: null, // 配了 activity 时的核对结果 (需求 R362), 见 checkActivity
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
  // 请求失败另由下面两个监听按地址与错误码记, 控制台的同一条不重复记
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
  if (config.activity?.stage === stage) await checkActivity()
  await waitFor(async () => (await summary())?.demo?.stages_done?.includes(stage), timeouts.idle_ms, `导入 ${stage}`)
}

// 系统忙时的说明 (需求 R362, Issue #63 验收 6; 走查测试给逐份处理加了延时): 数据管理页顶部的状态条写明 "数据管理
// 智能体正在识别文件 n/总数" 且 n 往前走; 转到工作区, 补交卡片的 "数据已齐" 变灰, 卡片里写明正在进行的活动; 不刷新
// 页面等处理完, 状态条消失, "数据已齐" 自己变为可点
async function checkActivity() {
  const want = config.activity
  const labels = config.labels.activity
  const progress = new RegExp(`(\\d+)/${want.total}(?!\\d)`)
  const bar = page.getByRole('region', { name: labels.bar })
  const seen = new Set()
  const sample = async () => {
    const text = await bar.innerText({ timeout: 1000 }).catch(() => '')
    const match = text.includes(labels.reading) ? text.match(progress) : null
    if (match) seen.add(Number(match[1]))
    return seen.size
  }
  await waitFor(async () => (await sample()) >= 2, timeouts.idle_ms, `状态条的识别进度 x/${want.total} 往前走`)
  await snap('data-page-bar')
  await goto(`${base}/engagements/${result.eid}`)
  const card = page.getByRole('region', { name: labels.requests })
  await card.waitFor({ timeout: timeouts.nav_ms })
  const ready = card.getByRole('button', { name: config.labels.ready })
  const note = card.getByText(labels.busy)
  await note.waitFor({ timeout: timeouts.action_ms })
  const noted = { note: await note.innerText(), disabled: await ready.isDisabled() }
  await sample()
  await snap('workspace-busy')
  await bar.waitFor({ state: 'detached', timeout: timeouts.idle_ms })
  await ready.waitFor({ timeout: timeouts.action_ms })
  await waitFor(async () => ready.isEnabled(), timeouts.action_ms, '处理完后 "数据已齐" 自己变为可点')
  await snap('workspace-done')
  result.activity = { seen: [...seen].sort((a, b) => a - b), ...noted, note_after: await note.count(), enabled_after: true }
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

// 重跑计数的键 (程序 对象), 同 tests/live_walk.py 的 rerun_key: 对象见 subjectOf (整条失败的计划与报告按修订)
function rerunKey(view) {
  return `${view.task.procedure_id} ${subjectOf(view)}`
}

// 可重跑的任务类型: 配置给的 (tests/live_walk.RERUNNABLE, 由后端 review_queue.RERUNNABLE 推出), 不在这里另写一份
const RERUNNABLE = config.rerunnable

function expectedFor(subject) {
  return config.table[subject] ?? { decision: 'confirm', reason: null, draft: null, conclusion: null }
}

// pending 为当时全部待决任务 (复核队列各项). 规则同 tests/live_walk.py 的 review_body
function reviewPlan(view, pending) {
  const { task } = view
  const subject = subjectOf(view)
  if (task.kind !== 'normal') result.troubled[task.id] = [task.procedure_id, subject, task.kind]
  const expected = expectedFor(subject)
  const key = rerunKey(view)
  // 判断或执行失败先重跑一次 (每个对象至多一次): failed 重跑后又失败即判失败; unattributed (带规则例外的对象, 需求
  // R347, R355) 重跑后仍失败时按下面的决定表处理. 一轮失败算一组 (同 decisions.rerun_group): 同程序同一次执行其余待决
  // 的可重跑任务随之重跑 (DecisionSubmit 一并关闭), 也算重跑过一次
  if (task.kind === 'failed' || (task.kind === 'unattributed' && !failures.get(key))) {
    failures.set(key, (failures.get(key) ?? 0) + 1)
    if (failures.get(key) > 1) return { error: `${task.procedure_id} ${subject || '(整条)'} 重跑后又失败` }
    const group = pending.filter(
      (other) =>
        other.task.procedure_id === task.procedure_id &&
        other.task.run_id === task.run_id &&
        RERUNNABLE.includes(other.task.kind) &&
        other.task.id !== task.id,
    )
    for (const other of group) failures.set(rerunKey(other), (failures.get(rerunKey(other)) ?? 0) + 1)
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

// 判断的复核卡片按 AI 结论给选项 (需求 R364): 决定表的决定 → 卡片上的选项. 判为错报时认定错报用默认的 "同意" (草稿
// 由 AI 带出, 不另填), 其余 "不是错报" 的为推翻 (须写结论); 卡片照 allowed_decisions 给选项时 (争议, 例外, 带错报更新
// 载荷的判断) 选项即决定本身
const CHOICES = {
  misstatement: { misstatement: 'agree_misstatement', override: 'disagree_not_misstatement' },
  accept: { confirm: 'agree_clear', misstatement: 'disagree_misstatement' },
  close: { confirm: 'agree_clear', misstatement: 'disagree_misstatement' },
  more_evidence: { misstatement: 'misstatement', override: 'not_misstatement' },
}

async function optionFor(view, plan) {
  const id = view.task.id
  if (await page.locator(`[id="decision-${id}-${plan.decision}"]`).count()) return { option: plan.decision, plan }
  const byAction = CHOICES[view.target.proposed_action] ?? {}
  // AI 判为错报或暂时判断不了时没有 "确认": 决定表的确认改为推翻, 结论同其余推翻
  const conclusion = config.confirm_conclusion
  const want =
    plan.decision === 'confirm' && !byAction.confirm
      ? { ...plan, decision: 'override', conclusion, reason: plan.reason ?? conclusion }
      : plan
  if (await page.locator(`[id="decision-${id}-${want.decision}"]`).count()) return { option: want.decision, plan: want }
  const option = byAction[want.decision]
  if (!option) throw new Error(`${id} 的卡片没有对应 ${plan.decision} 的选项 (AI 结论 ${view.target.proposed_action})`)
  return { option, plan: want }
}

async function fillDraft(id, draft) {
  await selectById(`ms-type-${id}`, draft.type)
  await fillById(`ms-account-${id}`, draft.account)
  await selectById(`ms-assertion-${id}`, draft.assertion)
  await fillById(`ms-amount-${id}`, (draft.amount_cents / 100).toFixed(2))
  await selectById(`ms-direction-${id}`, draft.direction)
}

async function decideTask(view, pending, forcedPlan = null) {
  const id = view.task.id
  let plan = forcedPlan ?? reviewPlan(view, pending)
  if (plan.error) {
    issue(plan.error, '每个对象至多重跑一次, 任务类型在决定表的范围内')
    throw new Stuck(plan.error)
  }
  await goto(`${base}/engagements/${result.eid}/reviews/${encodeURIComponent(id)}`)
  await recordButton().waitFor()
  let byDefault = false
  if (view.allowed_decisions.length > 1 || plan.decision !== 'dispute_resolution') {
    const chosen = await optionFor(view, plan)
    plan = chosen.plan
    if (!view.allowed_decisions.includes(plan.decision)) throw new Error(`${id} 不能选 ${plan.decision} (可选 ${view.allowed_decisions})`)
    byDefault = chosen.option === 'agree_misstatement'
    const radio = page.locator(`[id="decision-${id}-${chosen.option}"]`)
    if (byDefault && !(await radio.isChecked())) issue(`${id} 的 "同意, 认定为错报" 没有默认选中`, 'AI 判为错报时默认选中同意 (需求 R364)')
    if (!byDefault) await choose(`decision-${id}-${chosen.option}`)
  }
  result.decisions.push([id, plan.decision])
  if (plan.choice) await choose(`dispute-${id}-${plan.choice}`)
  if (plan.draft && plan.decision === 'dispute_resolution') {
    const attach = page.locator(`[id="attach-ms-${id}"]`)
    if ((await attach.count()) && !(await attach.isChecked())) await attach.check()
  }
  if (plan.conclusion && plan.decision !== 'confirm') await fillById(`conclusion-${id}`, plan.conclusion)
  if (plan.draft && !byDefault) await fillDraft(id, plan.draft)
  if (plan.reason) await fillById(`reason-${id}`, plan.reason)
  if (config.evidence_followup && subjectOf(view) === 'XJ-499' && view.task.procedure_id === 'GEN-02' && byDefault) {
    const editor = page.getByTestId('correction-editor')
    const descriptions = editor.locator('input[aria-label^="Entry description "]')
    const count = await descriptions.count()
    if (!count) throw new Error('更正建议没有可编辑分录')
    const amount = page.locator(`[id="ms-amount-${id}"]`)
    const original = await amount.inputValue()
    await amount.fill('749.99')
    await amount.fill(original)
    if (await descriptions.count() !== count) throw new Error('修改错报金额清空了更正建议')
    const first = descriptions.first()
    await first.fill(`Auditor reviewed: ${await first.inputValue()}`)
    result.correction_edited = true
  }
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

// ── 人工程序 (人工任务页): FIN-08 管理层回复登记 (需求 R163) ──

// ISO 时刻 → 浏览器本地时区的 datetime-local 值 (页面提交时按本机时区换回带时区的时刻)
async function localDateTime(iso) {
  return page.evaluate((value) => {
    const d = new Date(value)
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
  }, iso)
}

async function doManual(view) {
  const { task } = view
  const plan = config.manual[task.procedure_id]
  if (!plan) {
    issue(`驱动没有 ${task.procedure_id} 人工任务的做法`, '人工任务都在配置的范围内')
    throw new Stuck(`没有 ${task.procedure_id} 的人工结果`)
  }
  const labels = config.labels.manual
  result.decisions.push([task.id, 'manual_result'])
  await goto(`${base}/engagements/${result.eid}/manual/${encodeURIComponent(task.id)}`)
  const submit = page.getByRole('button', { name: labels.submit, exact: true })
  await submit.waitFor()
  // 登记表每条错报一组 (fieldset, 以错报编号为名); 组内的回复原文框把它与 "是否同意更正" 那一小组区分开
  const rows = page.getByRole('group').filter({ has: page.getByLabel(labels.response) })
  const count = await rows.count()
  // 撤回后重新认定时可能只有一条新错报要沟通, 登记行须逐一对应本次任务材料.
  const expectedRows = task.manual_request.material.misstatements
  if (count !== expectedRows.length) issue(`登记表有 ${count} 行`, `材料里待沟通的错报 ${expectedRows.length} 条各一行`)
  for (let i = 0; i < count; i += 1) {
    const row = rows.nth(i)
    if (!(await row.innerText()).includes(expectedRows[i].id)) throw new Error('沟通登记行与本次任务材料的错报不符')
    await row.getByLabel(labels.communicatedAt).fill(await localDateTime(plan.communicated_at))
    await row.getByLabel(labels.communicatedTo).fill(plan.communicated_to)
    await row.getByLabel(labels.respondedAt).fill(await localDateTime(plan.responded_at))
    await row.getByLabel(labels.response).fill(plan.response)
    await row.getByRole('radio', { name: plan.agrees ? labels.agrees : labels.disagrees, exact: true }).check()
  }
  await page.getByLabel(labels.performedOn).fill(plan.performed_on)
  await page.getByLabel(labels.conclusion, { exact: true }).fill(plan.conclusion)
  await page.getByLabel(labels.reason, { exact: true }).fill(plan.reason)
  await snap('form')
  if (await submit.isDisabled()) throw new Error(`${task.id} 的提交按钮不可点`)
  await click(submit)
  await waitFor(
    async () => {
      const pending = (await api(`/engagements/${result.eid}/review-tasks?status=pending`)).body ?? []
      return !pending.some((item) => item.task.id === task.id)
    },
    timeouts.action_ms,
    `${task.id} 的人工结果`,
  )
}

// ── 等客户更正 (总览的更正卡片, 需求 R163): 勾选待更正的错报与配置的更正分录, 提交 "客户已过账调整分录" ──

async function postCorrection(view) {
  const labels = config.labels.correction
  const ids = view.awaiting_correction_ids ?? []
  await goto(`${base}/engagements/${result.eid}`)
  const heading = page.getByRole('heading', { name: labels.title, exact: true })
  await heading.waitFor()
  const card = page.locator('section').filter({ has: heading })
  for (const id of ids) await card.getByRole('checkbox', { name: new RegExp(`^${id}\\b`) }).check()
  const stopping = Boolean(config.review_withdrawal)
  await card.getByRole('radio', { name: new RegExp(`^${stopping ? labels.stop : labels.posted}`) }).check()
  const entries = stopping ? [] : config.correction.entries
  for (const entry of entries) {
    const box = card.getByRole('checkbox', { name: new RegExp(`^${entry}\\b`) })
    await box.waitFor()
    await box.check()
  }
  await snap('form')
  const submit = card.getByRole('button', { name: labels.submit, exact: true })
  if (await submit.isDisabled()) throw new Error('更正通知的提交按钮不可点')
  await click(submit)
  result.corrections.push([stopping ? 'stop_waiting' : 'adjustments_posted', ids, entries])
  await waitFor(async () => !(await overview()).awaiting_correction, timeouts.action_ms, '更正通知')
}

// Issue #67 的两种撤回入口共用: 理由留空也能提交, 等任务重新待决; 读接口只核对状态, 写入都经页面.
async function withdrawDecision(view, button) {
  await click(button)
  const form = page.getByTestId(`withdraw-${view.withdrawable_record_id}`)
  const confirm = form.getByRole('button', { name: config.labels.withdraw.confirm, exact: true })
  await confirm.waitFor()
  if (await confirm.isDisabled()) throw new Error('撤回理由留空时不可提交')
  await click(confirm)
  await waitFor(async () => {
    const tasks = (await api(`/engagements/${result.eid}/review-tasks?status=pending`)).body ?? []
    return tasks.some((task) => task.task.id === view.task.id)
  }, timeouts.action_ms, '撤回后任务回到待复核')
  await idle('撤回之后的后台执行')
}

async function checkWithdrawal() {
  const gate = (await api(`/engagements/${result.eid}/gate`)).body
  const schedule = gate.material.schedule
  const stage = result.withdrawal_totals.length
  const total = [79853, 4853, 79853][stage]
  if (schedule.total_cents !== total) throw new Error(`撤回验收第 ${stage + 1} 次合计为 ${schedule.total_cents}, 应为 ${total}`)
  if (schedule.items.length !== (stage === 1 ? 1 : 2)) throw new Error('错报汇总条数不符')
  const tasks = (await api(`/engagements/${result.eid}/review-tasks?status=decided`)).body ?? []
  const view = tasks.find((task) => subjectOf(task) === 'XJ-499' && task.task.procedure_id === 'GEN-02')
  if (!view?.withdrawable_record_id) throw new Error('FIN-01 之前没有给出 XJ-499 的可撤回决定')
  if (stage === 0) {
    if (schedule.unrecognised.length) throw new Error('两笔均同意时出现了未认定建议')
    await goto(`${base}/engagements/${result.eid}/reviews/${encodeURIComponent(view.task.id)}`)
    await withdrawDecision(view, page.getByRole('button', { name: config.labels.withdraw.action, exact: true }))
    await recordButton().waitFor()
    await choose(`decision-${view.task.id}-disagree_not_misstatement`)
    if (await recordButton().isDisabled()) throw new Error('选择不是错报后没有预填结论')
    await click(recordButton())
    result.decisions.push([view.task.id, 'override'])
  } else if (stage === 1) {
    if (schedule.unrecognised.length !== 1 || schedule.unrecognised[0].subject_id !== 'XJ-499') throw new Error('未认定建议没有列出 XJ-499')
    await goto(`${base}/engagements/${result.eid}/gate`)
    const row = page.getByTestId('unrecognised-XJ-499')
    await row.waitFor()
    const text = await row.innerText()
    for (const expected of ['750.00', config.labels.withdraw.material, config.labels.withdraw.impact]) {
      if (!text.includes(expected)) throw new Error(`未认定建议缺少 ${expected}`)
    }
    if (!(await row.getAttribute('class')).includes('border-risk')) throw new Error('重大金额没有标红')
    await snap('unrecognised')
    await withdrawDecision(view, row.getByRole('button', { name: config.labels.withdraw.redecide, exact: true }))
    const pending = (await api(`/engagements/${result.eid}/review-tasks?status=pending`)).body
    await decideTask(pending.find((task) => task.task.id === view.task.id), pending)
  } else {
    if (schedule.unrecognised.length) throw new Error('重新同意后仍列出已认定建议')
    await decideGate('FIN-01')
    await idle('意见决定之后的后台执行')
    await goto(`${base}/engagements/${result.eid}/reviews/${encodeURIComponent(view.task.id)}`)
    await page.getByText(config.labels.withdraw.decided, { exact: true }).waitFor()
    if (await page.getByRole('button', { name: config.labels.withdraw.action, exact: true }).count()) throw new Error('FIN-01 通过后仍有撤回入口')
  }
  result.withdrawal_totals.push(total)
}

async function confirmCurrency() {
  await goto(`${base}/engagements/${result.eid}/data`)
  await page.getByLabel('Accounting currency', { exact: true }).fill(config.currency)
  await click(page.getByRole('button', { name: 'Confirm currency', exact: true }))
  await waitFor(async () => {
    const view = await overview()
    return view.engagement.currency === config.currency && view.engagement.currency_source === 'auditor'
  }, timeouts.action_ms, '保存项目币种')
}

async function editWorkpaper() {
  await goto(`${base}/engagements/${result.eid}/workpapers?ref=D-3`)
  await page.getByTestId('workpaper-preview').waitFor()
  await page.getByLabel('Action', { exact: true }).selectOption('conclusion')
  await page.getByLabel('Text', { exact: true }).fill('Reviewed the six invoices against the recorded evidence.')
  await click(page.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(async () => (await api(`/engagements/${result.eid}/workpapers/D-3`)).body?.workpaper.conclusion === 'Reviewed the six invoices against the recorded evidence.', timeouts.action_ms, '底稿结论未保存')
  await page.getByLabel('Action', { exact: true }).selectOption('signoff')
  await click(page.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(async () => (await api(`/engagements/${result.eid}/workpapers/D-3`)).body?.workpaper.status === 'signed', timeouts.action_ms, '底稿签署未保存')
  result.workpapers = { edited: true, previewed: false, downloaded: false }
}

async function requestEvidence(view, pending, unavailable = false) {
  await decideTask(view, pending, { decision: 'more_evidence', reason: 'Obtain additional evidence for this object.' })
  await idle('要求补证之后')
  if (!unavailable) { result.evidence_followup.requested = true; return }
  await goto(`${base}/engagements/${result.eid}/data`)
  const row = page.locator('[data-testid^="evidence-request-ER-"]').filter({ hasText: subjectOf(view) }).first()
  await row.getByRole('button', { name: 'Mark unavailable', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('button', { name: 'Mark unavailable', exact: true }).click()
  await waitFor(async () => ((await api(`/engagements/${result.eid}/evidence-requests`)).body ?? []).some(r => r.subject_id === subjectOf(view) && r.status === 'unavailable'), timeouts.action_ms, '范围受限未保存')
  result.evidence_followup.unavailable = true
}

async function uploadLateEvidence() {
  await goto(`${base}/engagements/${result.eid}/gate`)
  const summary = page.getByTestId('evidence-followup-summary')
  await summary.waitFor()
  const summaryText = await summary.innerText()
  for (const expected of ['XJ-397', 'XJ-575', '296.95']) {
    if (!summaryText.includes(expected)) throw new Error(`FIN-01 补证摘要缺少 ${expected}`)
  }
  await snap('FIN-01-followup-summary')
  await goto(`${base}/engagements/${result.eid}/data`)
  const row = page.locator('[data-testid^="evidence-request-ER-"]').filter({ hasText: 'XJ-397' }).first()
  await row.locator('input[type="file"]').setInputFiles(config.evidence_followup)
  await waitFor(async () => {
    const requests = (await api(`/engagements/${result.eid}/evidence-requests`)).body ?? []
    return requests.some(r => r.subject_id === 'XJ-397' && r.rejudged_judgment_id)
  }, timeouts.idle_ms, 'FIN-01 收到补证后未自动重判')
  result.evidence_followup.received = true
}

async function checkAuditFile() {
  await goto(`${base}/engagements/${result.eid}/workpapers?ref=D-3`)
  await page.getByTestId('workpaper-preview').waitFor()
  await page.getByRole('heading', { name: /D-3.*Three-way match/ }).waitFor()
  await page.getByLabel('Version', { exact: true }).selectOption('1')
  await page.getByTestId('workpaper-preview').getByText('ORC1026', { exact: true }).first().waitFor()
  if (!(await page.getByRole('button', { name: 'Save', exact: true }).isDisabled())) throw new Error('归档后的底稿仍可修改')
  const single = page.waitForEvent('download')
  await click(page.getByRole('button', { name: 'Download Excel', exact: true }))
  const file = await single
  if (!file.suggestedFilename().startsWith('D-3 ')) throw new Error('单份底稿文件名不正确')
  await file.saveAs(path.join(SHOTS, 'D-3.xlsx'))
  const bundled = page.waitForEvent('download')
  await click(page.getByRole('button', { name: 'Download all (ZIP)', exact: true }))
  const zip = await bundled
  await zip.saveAs(path.join(SHOTS, 'audit-file.zip'))
  result.workpapers.previewed = true
  result.workpapers.downloaded = true
}

async function checkArchivedCurrency() {
  await goto(`${base}/engagements/${result.eid}/data`)
  const input = page.getByLabel('Accounting currency', { exact: true })
  await input.waitFor()
  if (await input.inputValue() !== config.currency || !(await input.isDisabled())) throw new Error('归档后币种未保存或仍可修改')
  await goto(`${base}/engagements/${result.eid}/data/accounts`)
  await page.getByRole('columnheader', { name: `Current year (${config.currency})`, exact: true }).waitFor()
  result.currency = config.currency
}

// ── 主循环: 每次停下按当时的状态办 (同 R311 的 drive) ──

async function main() {
  if (!(await step('注册并登录', register))) return
  if (!(await step('新建演示项目', createEngagement))) return
  if (config.currency && !(await step('确认项目币种', confirmCurrency))) return
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
      if (config.currency) await step('核对归档后的币种与金额表头', checkArchivedCurrency)
      if (config.currency) await step('归档底稿的历史预览与整套下载', checkAuditFile)
      // 报告页上签字的那一稿带 "已签字归档" 标记; 等它出来再截图 (打开页面时内容还没渲染)
      await step('归档后的报告页', async () => {
        await goto(`${base}/engagements/${result.eid}/report`)
        await page.getByText(config.labels.signed).first().waitFor()
      })
      return
    }
    const pending = (await api(`/engagements/${result.eid}/review-tasks?status=pending`)).body ?? []
    const suggested = (await summary())?.demo?.suggested ?? null
    if (config.currency && !result.workpapers && pending.length && view.engagement.phase === 'P2_Executing') {
      const papers = (await api(`/engagements/${result.eid}/workpapers`)).body ?? []
      if (papers.some(p => p.ref === 'D-3' && p.availability === 'generated')) {
        if (!(await step('修改并签署工作底稿', editWorkpaper))) return
        continue
      }
    }
    let name
    let action
    if (pending.length && pending[0].task.kind === 'manual') {
      const task = pending[0]
      name = `人工程序 ${task.task.procedure_id}`
      action = () => doManual(task)
    } else if (pending.length) {
      const task = pending[0]
      if (config.evidence_followup && task.task.kind === 'normal' && task.task.procedure_id === 'GEN-02') {
        if (subjectOf(task) === 'XJ-397' && !result.evidence_followup.requested) {
          if (!(await step('要求 XJ-397 补充证据', async () => { await requestEvidence(task, pending); await idle('补证请求之后') }))) return
          continue
        }
        if (subjectOf(task) === 'XJ-575' && !result.evidence_followup.unavailable) {
          if (!(await step('无法取得 XJ-575 补证并记录范围受限', async () => { await requestEvidence(task, pending, true); await idle('范围受限之后') }))) return
          continue
        }
      }
      name = `复核 ${task.task.procedure_id} ${subjectOf(task)}`
      action = () => decideTask(task, pending)
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
      if (config.evidence_followup && view.current_gate === 'FIN-01' && !result.evidence_followup.received) {
        if (!(await step('FIN-01 上传补证并自动回到执行阶段', async () => { await uploadLateEvidence(); await idle('补证重判之后') }))) return
        continue
      }
      name = `闸门 ${view.current_gate}`
      action = config.review_withdrawal && view.current_gate === 'FIN-01' ? checkWithdrawal : () => decideGate(view.current_gate)
    } else if (view.awaiting_correction) {
      name = '提交更正通知'
      action = () => postCorrection(view)
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
    if (config.activity?.only && result.activity) return // 只核对系统忙时的说明, 不走完
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
