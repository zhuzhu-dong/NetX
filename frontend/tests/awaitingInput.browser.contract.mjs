/**
 * Awaiting Input Browser UI Contract
 *
 * This is intentionally a browser presentation contract, not a mutating
 * Browser→Backend E2E.  Every API response is intercepted so the test proves
 * that NetworkOverview renders the persisted lifecycle contract correctly
 * without requiring an operator credential or changing a real intent.
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'

const PORT = 5174
const BASE_URL = `http://127.0.0.1:${PORT}`
const EDGE = process.env.IDN_BROWSER_PATH || join(process.env['PROGRAMFILES(X86)'] || process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe')
const ARTIFACT_DIR = new URL('../artifacts/', import.meta.url)
const FRONTEND_ROOT = fileURLToPath(new URL('../', import.meta.url))
const VITE_CLI = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))
const artifactPath = (name) => fileURLToPath(new URL(`../artifacts/${name}`, import.meta.url))

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitForVite() {
  const deadline = Date.now() + 30_000
  while (Date.now() < deadline) {
    try {
      if ((await fetch(BASE_URL)).ok) return
    } catch {}
    await wait(250)
  }
  throw new Error('LOCAL_BROWSER_UI_CONTRACT_VITE_NOT_READY')
}

function intentFixture({ id, status, phase, prompt = null, missingFields = [] }) {
  const snapshot = {
    phase,
    requires_user_input: phase === 'AWAITING_USER_INPUT',
    needs_clarification: phase === 'AWAITING_USER_INPUT',
    clarification_prompt: prompt,
    clarification_question: prompt,
    missing_fields: missingFields,
    reason_code: phase === 'AWAITING_USER_INPUT' ? 'MISSING_REQUIRED_FIELDS' : null,
  }
  return {
    intent_id: id,
    session_id: `SES-${id}`,
    text: `Browser UI contract ${id}`,
    priority: 'medium',
    status,
    snapshot,
    created_at: '2026-08-12T00:00:00+00:00',
    updated_at: '2026-08-12T00:00:00+00:00',
    milestones: [],
    result: {},
  }
}

async function openFixturePage(fixture, { holdClarification = false, listFixture = fixture, currentFixture = { items: [] } } = {}) {
  const browser = await chromium.launch({ executablePath: EDGE, headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 980 } })
  const page = await context.newPage()
  const observed = { requests: [], pageErrors: [] }
  let releaseClarification
  const clarificationGate = new Promise((resolve) => { releaseClarification = resolve })

  page.on('pageerror', (error) => observed.pageErrors.push(error.message))
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/v1/')) {
      observed.requests.push({ method: request.method(), path: new URL(request.url()).pathname })
    }
  })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const json = (body, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })

    if (request.method() === 'POST' && path === `/api/v1/intents/${fixture.intent_id}/clarifications`) {
      if (holdClarification) await clarificationGate
      fixture.status = 'running'
      fixture.snapshot = {
        phase: 'RUNNING',
        requires_user_input: false,
        needs_clarification: false,
        clarification_prompt: null,
        clarification_question: null,
        missing_fields: [],
        reason_code: null,
      }
      return json({
        intent_id: fixture.intent_id,
        session_id: fixture.session_id,
        job_id: `JOB-${fixture.intent_id}`,
        status: 'running',
      }, 202)
    }
    if (request.method() === 'GET' && path === '/api/v1/intents') {
      return json({ items: listFixture ? [listFixture] : [], total: listFixture ? 1 : 0, page: 1, page_size: 20 })
    }
    if (request.method() === 'GET' && path === '/api/v1/intents/current') {
      return json(currentFixture)
    }
    if (request.method() === 'GET' && path === `/api/v1/intents/${fixture.intent_id}`) {
      return json(fixture)
    }
    if (request.method() === 'GET' && path === '/api/v1/conversations') {
      return json({ items: listFixture ? [{ session_id: fixture.session_id, user_id: 'operator', title: fixture.text }] : [] })
    }
    if (request.method() === 'GET' && (
      path === `/api/v1/conversations/${fixture.session_id}/timeline`
      || path === `/api/v1/sessions/${fixture.session_id}/intents`
    )) {
      return json({ items: [{
        id: `MSG-${fixture.intent_id}`,
        role: 'user',
        type: 'user_text',
        content: fixture.text,
        intent_id: fixture.intent_id,
      }] })
    }
    if (path.includes('/stream') || path.includes('/events')) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    return json({ items: [] })
  })

  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(() => localStorage.setItem('netpulse-auth-session', JSON.stringify({
    email: 'awaiting-ui-contract@example.invalid', role: 'OPERATOR',
  })))
  await page.goto(`${BASE_URL}/dashboard`, { waitUntil: 'domcontentloaded' })
  if (listFixture) await page.locator('.history-item').first().waitFor({ state: 'visible', timeout: 10_000 })
  // Dashboard loads conversation metadata and intent records independently.
  // Wait for the first authoritative intent reconciliation before selecting its
  // session; otherwise the UI correctly creates a temporary conversation shell
  // whose default PENDING status is not the fixture under test.
  await page.waitForTimeout(500)
  if (listFixture) await page.locator('.history-item').first().click()
  await page.locator('.intent-composer textarea').waitFor({ state: 'visible', timeout: 25_000 })
  await page.waitForTimeout(150)

  return { browser, context, page, observed, releaseClarification }
}

async function closeFixturePage(harness) {
  harness.releaseClarification()
  await harness.page.close().catch(() => {})
  await harness.context.close().catch(() => {})
  await harness.browser.close().catch(() => {})
}

async function assertButton(page, label, visible) {
  const button = page.getByRole('button', { name: label, exact: true })
  if (visible) await assert.doesNotReject(button.waitFor({ state: 'visible', timeout: 5_000 }))
  else assert.equal(await button.count(), 0, `unexpected visible button: ${label}`)
}

await mkdir(ARTIFACT_DIR, { recursive: true })
const vite = spawn(process.execPath, [VITE_CLI, '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: FRONTEND_ROOT,
  stdio: 'ignore',
  windowsHide: true,
})

try {
  await waitForVite()

  // Case 0 — NO_SELECTION: returning from a historical intent to a fresh
  // composer must keep the primary assistant surface mounted and editable.
  {
    const fixture = intentFixture({ id: 'INT-BROWSER-NO-SELECTION', status: 'completed', phase: 'COMPLETED' })
    const harness = await openFixturePage(fixture)
    try {
      const { page } = harness
      await page.locator('.new-intent-btn').click()
      const textarea = page.locator('.intent-composer textarea')
      await textarea.waitFor({ state: 'visible', timeout: 5_000 })
      assert.equal(await textarea.isDisabled(), false)
      await assertButton(page, '运行', true)
      await assertButton(page, '运行中', false)
      await assertButton(page, '发送补充信息', false)
    } finally {
      await closeFixturePage(harness)
    }
  }

  // Case A — ACTIVE: a real executing status locks the composer and exposes stop.
  {
    const fixture = intentFixture({ id: 'INT-BROWSER-ACTIVE', status: 'running', phase: 'RUNNING' })
    const harness = await openFixturePage(fixture)
    try {
      const textarea = harness.page.locator('.intent-composer textarea')
      assert.equal(await textarea.isDisabled(), true)
      await assertButton(harness.page, '终止执行', true)
      await assertButton(harness.page, '发送补充信息', false)
      await harness.page.screenshot({ path: artifactPath('awaiting-active.png') })
    } finally {
      await closeFixturePage(harness)
    }
  }

  // Case B — AWAITING_INPUT: render the authoritative prompt and submit only
  // a same-intent clarification request. The held route proves temporary lock.
  {
    const fixture = intentFixture({
      id: 'INT-BROWSER-AWAITING',
      status: 'pending_clarification',
      phase: 'AWAITING_USER_INPUT',
      prompt: '请补充目标主机和业务类型',
      missingFields: ['destination', 'service'],
    })
    const harness = await openFixturePage(fixture, { holdClarification: true })
    try {
      const { page } = harness
      const textarea = page.locator('.intent-composer textarea')
      const awaitingDom = await page.evaluate(() => ({
        textareaDisabled: document.querySelector('.intent-composer textarea')?.disabled,
        buttonLabels: [...document.querySelectorAll('.intent-composer button')].map((button) => button.textContent?.trim()),
        placeholder: document.querySelector('.intent-composer textarea')?.getAttribute('placeholder'),
      }))
      assert.equal(await textarea.isDisabled(), false, JSON.stringify({ awaitingDom, requests: harness.observed.requests }))
      assert.match(await textarea.getAttribute('placeholder'), /智能体需要你的补充信息/)
      await assertButton(page, '发送补充信息', true)
      await assertButton(page, '运行中', false)
      await assertButton(page, '终止执行', true)
      await assert.doesNotReject(page.getByText('请补充目标主机和业务类型', { exact: true }).waitFor({ state: 'visible' }))
      await page.screenshot({ path: artifactPath('awaiting-input.png') })

      await textarea.fill('目标主机是 h_dc')
      await page.getByRole('button', { name: '发送补充信息', exact: true }).click()
      await page.waitForTimeout(100)
      assert.equal(await textarea.isDisabled(), true, 'clarification HTTP request must lock temporarily')
      harness.releaseClarification()
      await page.getByRole('button', { name: '运行中', exact: true }).waitFor({ state: 'visible', timeout: 5_000 })
      assert.equal(await textarea.isDisabled(), true, 'accepted continuation enters ACTIVE fixture projection')

      const clarificationPosts = harness.observed.requests.filter((item) => (
        item.method === 'POST' && item.path === `/api/v1/intents/${fixture.intent_id}/clarifications`
      ))
      const newIntentPosts = harness.observed.requests.filter((item) => (
        item.method === 'POST' && item.path === '/api/v1/intents'
      ))
      assert.equal(clarificationPosts.length, 1)
      assert.equal(newIntentPosts.length, 0)
    } finally {
      await closeFixturePage(harness)
    }
  }

  // Case C — TERMINAL: a completed historical intent must not retain a lock.
  {
    const fixture = intentFixture({ id: 'INT-BROWSER-TERMINAL', status: 'completed', phase: 'COMPLETED' })
    const harness = await openFixturePage(fixture)
    try {
      const textarea = harness.page.locator('.intent-composer textarea')
      assert.equal(await textarea.isDisabled(), false)
      await assertButton(harness.page, '运行', true)
      await assertButton(harness.page, '发送补充信息', false)
      await harness.page.screenshot({ path: artifactPath('awaiting-terminal.png') })
    } finally {
      await closeFixturePage(harness)
    }
  }

  // Case D — SUCCESS reconciliation: reproduce the production shape where a
  // list/current projection omitted lifecycle fields, while the same intent's
  // authoritative detail says success. Detail must unlock the Composer.
  {
    const fixture = intentFixture({ id: 'INT-BROWSER-SUCCESS-RECONCILE', status: 'success', phase: undefined })
    const staleListFixture = {
      intent_id: fixture.intent_id,
      session_id: fixture.session_id,
      text: fixture.text,
      priority: fixture.priority,
      created_at: fixture.created_at,
      updated_at: fixture.updated_at,
    }
    const harness = await openFixturePage(fixture, {
      listFixture: staleListFixture,
      currentFixture: {
        items: [{
          intent_id: fixture.intent_id,
          lifecycle_status: 'SATISFIED',
          phase: 'COMPLETED',
          current_stage: 'success',
          terminal: true,
          requires_user_input: false,
          needs_clarification: false,
          execution_status: 'COMPLETED',
        }],
      },
    })
    try {
      const { page } = harness
      const textarea = page.locator('.intent-composer textarea')
      assert.equal(await textarea.isDisabled(), false)
      await assertButton(page, '运行', true)
      await assertButton(page, '运行中', false)
      await assertButton(page, '发送补充信息', false)
      assert.ok(harness.observed.requests.some((request) => (
        request.method === 'GET' && request.path === `/api/v1/intents/${fixture.intent_id}`
      )), 'same intent detail must reconcile the stale list record')
    } finally {
      await closeFixturePage(harness)
    }
  }

  console.log('Awaiting Input + Success Reconciliation Browser UI Contract: PASS')
} finally {
  vite.kill()
}
