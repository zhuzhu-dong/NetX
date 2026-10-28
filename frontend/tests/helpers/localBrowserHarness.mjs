import { chromium } from 'playwright-core'
import { join } from 'node:path'

export const LOCAL_BASE_URL = 'http://127.0.0.1:5173'
const EDGE = process.env.IDN_BROWSER_PATH || join(process.env['PROGRAMFILES(X86)'] || process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe')

export async function preflight(url = LOCAL_BASE_URL) {
  const check = async path => {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 5000)
    try { const response = await fetch(`${url}${path}`, { signal:controller.signal }); return { status:response.status, ok:response.ok } }
    catch (error) { return { error:error.message, ok:false } } finally { clearTimeout(timer) }
  }
  return { frontend:await check('/'), api_health:await check('/api/v1/health/ready') }
}

export async function openLocalDashboard(events, width = 1600, height = 900) {
  events.push('BROWSER_LAUNCHING'); const browser = await chromium.launch({ executablePath:EDGE, headless:true })
  events.push('BROWSER_LAUNCHED'); const context = await browser.newContext({ viewport:{ width, height } }); const page = await context.newPage()
  events.push('PAGE_CREATED'); await page.goto('http://localhost:5173/login', { waitUntil:'commit', timeout:10000 })
  await page.evaluate(() => localStorage.setItem('netpulse-auth-session', JSON.stringify({ email:'network-overview-scan@example.invalid', role:'OPERATOR' })))
  events.push('NAVIGATION_STARTED'); await page.goto('http://localhost:5173/dashboard', { waitUntil:'commit', timeout:10000 })
  await page.waitForTimeout(500); await page.waitForSelector('.chatops-page', { state:'attached', timeout:5000 }); events.push('READY_FOR_RESIZE')
  return { browser, context, page }
}

export async function closeHarness(harness) { await harness?.page?.close().catch(() => {}); await harness?.context?.close().catch(() => {}); await harness?.browser?.close().catch(() => {}) }
