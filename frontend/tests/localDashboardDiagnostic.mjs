import { chromium } from 'playwright-core'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const EDGE = process.env.IDN_BROWSER_PATH || join(process.env['PROGRAMFILES(X86)'] || process.env.PROGRAMFILES || '', 'Microsoft', 'Edge', 'Application', 'msedge.exe')
const browser = await chromium.launch({ executablePath: EDGE, headless:true })
const page = await browser.newPage({ viewport:{ width:1600, height:900 } })
const errors=[]; const failed=[]
page.on('pageerror', error => errors.push({ type:'pageerror', message:error.message, stack:error.stack }))
page.on('console', message => { if (message.type() === 'error') errors.push({ type:'console', message:message.text() }) })
page.on('requestfailed', request => failed.push({ url:request.url(), error:request.failure()?.errorText || '' }))
page.on('response', response => { if (response.status() >= 400) failed.push({ url:response.url(), status:response.status() }) })
await page.goto('http://localhost:5173/login', { waitUntil:'commit', timeout:10000 })
await page.evaluate(() => localStorage.setItem('netpulse-auth-session', JSON.stringify({ email:'local-layout-diagnostic@example.invalid', role:'OPERATOR' })))
await page.goto('http://localhost:5173/dashboard', { waitUntil:'commit', timeout:10000 })
await page.waitForTimeout(5000)
const report = await page.evaluate(() => {
  const inspect = (selector) => {
    const el=document.querySelector(selector); if (!el) return { exists:false }
    const r=el.getBoundingClientRect(), s=getComputedStyle(el)
    return { exists:true, box:{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}, display:s.display, visibility:s.visibility, opacity:s.opacity, position:s.position, transform:s.transform, overflow:s.overflow, overflowX:s.overflowX, overflowY:s.overflowY, zIndex:s.zIndex }
  }
  return { url:location.href, routeText:document.body.innerText.slice(0,500), viewport:{width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth}, app:inspect('#app'), shell:inspect('.app-shell'), sidebar:inspect('.global-sidebar'), main:inspect('.main-stage'), chatops:inspect('.chatops-page'), workbench:inspect('.topology-workbench'), composer:inspect('.intent-composer') }
})
await mkdir('artifacts', { recursive:true })
await writeFile('artifacts/local-dashboard-dom.json', JSON.stringify(report,null,2))
await writeFile('artifacts/local-dashboard-browser-errors.json', JSON.stringify({errors,failed},null,2))
await page.screenshot({ path:'artifacts/local-dashboard-1600.png' })
await browser.close()
console.log(JSON.stringify(report)); console.log(JSON.stringify({errors,failed}))
