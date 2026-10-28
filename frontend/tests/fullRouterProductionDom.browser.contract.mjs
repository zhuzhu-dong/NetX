/**
 * Full Router Production DOM Contract
 *
 * This deliberately exercises the production Dockerfile and Nginx-served
 * dist. It does not mount NetworkOverview directly and it never calls a real
 * backend: route interception isolates the App -> RouterView -> Dashboard ->
 * nested RouterView -> NetworkOverview render path from credentials and API
 * mutations.
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright-core'

const PROJECT_ROOT = fileURLToPath(new URL('../../', import.meta.url))
const EDGE = process.env.IDN_BROWSER_PATH || join(process.env['PROGRAMFILES(X86)'] || process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe')
const PORT = Number(process.env.IDN_ROUTER_PRODUCTION_PORT || 5184)
const IMAGE = `idn-router-production-contract:${process.pid}`
const CONTAINER = `idn-router-production-contract-${process.pid}`
const ORIGIN = `http://127.0.0.1:${PORT}`
const INTENT_ID = 'INT-FULL-ROUTER-PRODUCTION-CONTRACT'
const SESSION_ID = 'SES-FULL-ROUTER-PRODUCTION-CONTRACT'

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function command(commandName, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(commandName, args, { cwd: PROJECT_ROOT, stdio: 'ignore', windowsHide: true })
    child.on('error', reject)
    child.on('exit', (code) => code === 0 ? resolve() : reject(new Error(`${commandName} ${args[0]} exited ${code}`)))
  })
}

async function waitForProductionServer() {
  const deadline = Date.now() + 45_000
  while (Date.now() < deadline) {
    try {
      const response = await fetch(ORIGIN)
      if (response.ok) return
    } catch {}
    await wait(250)
  }
  throw new Error('FULL_ROUTER_PRODUCTION_SERVER_NOT_READY')
}

function lifecycleFixture(mode) {
  const state = {
    ACTIVE: { status: 'running', phase: 'RUNNING' },
    AWAITING_INPUT: { status: 'pending_clarification', phase: 'AWAITING_USER_INPUT' },
    TERMINAL: { status: 'success', phase: 'COMPLETED' },
  }[mode]
  if (!state) return null
  return {
    intent_id: INTENT_ID,
    session_id: SESSION_ID,
    text: `full router ${mode}`,
    priority: 'medium',
    status: state.status,
    lifecycle_status: mode === 'TERMINAL' ? 'SATISFIED' : undefined,
    phase: state.phase,
    terminal: mode === 'TERMINAL',
    requires_user_input: mode === 'AWAITING_INPUT',
    needs_clarification: mode === 'AWAITING_INPUT',
    job_status: mode === 'TERMINAL' ? 'completed' : 'running',
    execution_status: mode === 'TERMINAL' ? 'SUCCESS' : 'RUNNING',
    snapshot: {
      phase: state.phase,
      requires_user_input: mode === 'AWAITING_INPUT',
      needs_clarification: mode === 'AWAITING_INPUT',
      clarification_prompt: mode === 'AWAITING_INPUT' ? '请补充目标主机和业务类型' : null,
      missing_fields: mode === 'AWAITING_INPUT' ? ['destination', 'service'] : [],
    },
    created_at: '2026-08-12T00:00:00+00:00',
    updated_at: '2026-08-12T00:00:00+00:00',
    milestones: [],
    result: {},
  }
}

async function attachFixture(page, mode) {
  const record = lifecycleFixture(mode)
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/v1/intents' && request.method() === 'GET') return json({ items: record ? [record] : [], total: record ? 1 : 0, page: 1, page_size: 20 })
    if (path === '/api/v1/intents/current') return json(record ? { items: [record] } : { items: [] })
    if (path === `/api/v1/intents/${INTENT_ID}`) return json(record)
    if (path === '/api/v1/conversations') return json({ items: record ? [{ session_id: SESSION_ID, user_id: 'operator', title: record.text }] : [] })
    if (path === `/api/v1/conversations/${SESSION_ID}/timeline` || path === `/api/v1/sessions/${SESSION_ID}/intents`) return json({ items: record ? [{ id: 'MSG-ROUTER-PRODUCTION', role: 'user', type: 'user_text', content: record.text, intent_id: INTENT_ID }] : [] })
    if (path.includes('/stream') || path.includes('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    return json({ items: [] })
  })
}

async function assertComposer(page, expectedDisabled) {
  await page.locator('.intent-composer').waitFor({ state: 'visible', timeout: 12_000 })
  assert.equal(await page.locator('.topology-workbench').count(), 1)
  assert.equal(await page.locator('.intent-composer').count(), 1)
  const textarea = page.locator('.intent-composer textarea')
  assert.equal(await textarea.count(), 1)
  assert.equal(await textarea.isDisabled(), expectedDisabled)
}

async function coldDashboard(mode, { via } = {}) {
  const browser = await chromium.launch({ executablePath: EDGE, headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 980 } })
  await context.addInitScript(() => localStorage.setItem('netpulse-auth-session', JSON.stringify({ email: 'full-router-production@example.invalid', role: 'OPERATOR' })))
  const page = await context.newPage()
  const events = { warning: [], error: [], pageerror: [], requestfailed: [] }
  page.on('console', (message) => {
    if (message.type() === 'warning') events.warning.push(message.text())
    if (message.type() === 'error') events.error.push(message.text())
  })
  page.on('pageerror', (error) => events.pageerror.push(error.message))
  page.on('requestfailed', (request) => events.requestfailed.push(request.url()))
  await attachFixture(page, mode)
  if (via) await page.goto(`${ORIGIN}${via}`, { waitUntil: 'domcontentloaded' })
  await page.goto(`${ORIGIN}/dashboard`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(700)
  if (mode !== 'NO_SELECTION') {
    await page.locator('.history-item').first().click()
    await page.waitForTimeout(300)
  }
  await assertComposer(page, mode === 'ACTIVE')
  assert.equal(events.warning.length, 0, events.warning.join('\n'))
  assert.equal(events.error.length, 0, events.error.join('\n'))
  assert.equal(events.pageerror.length, 0, events.pageerror.join('\n'))
  assert.equal(events.requestfailed.length, 0, events.requestfailed.join('\n'))
  await browser.close()
}

try {
  await command('docker', ['build', '--platform', 'linux/amd64', '-f', 'frontend/Dockerfile', '-t', IMAGE, '.'])
  await command('docker', ['run', '-d', '--name', CONTAINER, '-p', `127.0.0.1:${PORT}:5173`, IMAGE])
  await waitForProductionServer()
  for (const mode of ['NO_SELECTION', 'ACTIVE', 'AWAITING_INPUT', 'TERMINAL']) await coldDashboard(mode)
  await coldDashboard('NO_SELECTION', { via: '/dashboard/topology' })
  await coldDashboard('NO_SELECTION', { via: '/dashboard/workflow' })
  console.log('Full Router Production DOM Contract: PASS')
} finally {
  await command('docker', ['rm', '-f', CONTAINER]).catch(() => {})
}
