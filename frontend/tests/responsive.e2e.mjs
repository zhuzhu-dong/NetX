import { mkdir, writeFile } from 'node:fs/promises'
import { closeHarness, openLocalDashboard, preflight } from './helpers/localBrowserHarness.mjs'

const widths = process.env.SMOKE === '1' ? [1600,1128] : [1600,1500,1400,1366,1320,1280,1250,1200,1180,1150,1128,1100,1080,1024,1000,900]
const artifact = 'artifacts/network-overview-breakpoint.json'
const errorsArtifact = 'artifacts/network-overview-browser-errors.json'
const timeout = (promise, ms) => Promise.race([promise, new Promise((_, reject) => setTimeout(() => reject(new Error('COLLECT_TIMEOUT')), ms))])

function summary(rows) {
  const visible = rows.filter(row => row.status === 'OK' && row.networkOverviewVisible && row.landingVisible)
  const failed = rows.filter(row => row.status !== 'OK' || !row.networkOverviewVisible || !row.landingVisible)
  return { last_known_good: visible.at(-1)?.width ?? null, first_known_bad: failed[0]?.width ?? null }
}

let harness; let page
const results=[]; const browserErrors=[]; const failedRequests=[]; const started=Date.now()
await mkdir('artifacts', { recursive:true })
const persist = async () => writeFile(artifact, JSON.stringify({ started_at:new Date(started).toISOString(), elapsed_ms:Date.now()-started, results, ...summary(results) }, null, 2))
try {
  const preflightResult = await preflight(); if (!preflightResult.frontend.ok || !preflightResult.api_health.ok) throw new Error(`STARTUP_PREFLIGHT_FAILED:${JSON.stringify(preflightResult)}`)
  harness = await openLocalDashboard([], 1600, 900); page = harness.page
  page.on('pageerror', error => browserErrors.push({ type:'pageerror', message:error.message }))
  page.on('console', message => { if (message.type() === 'error') browserErrors.push({ type:'console', message:message.text() }) })
  page.on('requestfailed', request => failedRequests.push({ url:request.url(), error:request.failure()?.errorText || '' }))
  for (const width of widths) {
    if (Date.now() - started > 40000) { results.push({ width, status:'GLOBAL_WATCHDOG' }); await persist(); break }
    try {
      await page.setViewportSize({ width, height:900 }); await page.waitForTimeout(100)
      const row = await timeout(page.evaluate(() => {
        const node = selector => { const el=document.querySelector(selector); if (!el) return { exists:false }; const r=el.getBoundingClientRect(), s=getComputedStyle(el); return { exists:true, rect:{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}, style:{display:s.display,visibility:s.visibility,opacity:s.opacity,position:s.position,transform:s.transform,overflow:s.overflow,overflowX:s.overflowX,overflowY:s.overflowY,zIndex:s.zIndex} } }
        const nodes={ shell:node('.app-shell'), main:node('.main-stage'), chatops:node('.chatops-page'), workbench:node('.topology-workbench'), hero:node('.conversation-empty-state'), composer:node('.intent-composer'), capabilities:node('.empty-capabilities') }
        const shown=n => n.exists && n.rect.width>10 && n.rect.height>10 && n.style.display!=='none' && n.style.visibility!=='hidden' && Number(n.style.opacity)>0
        return { matchMedia:{max1280:matchMedia('(max-width: 1280px)').matches,max1200:matchMedia('(max-width: 1200px)').matches,max1150:matchMedia('(max-width: 1150px)').matches,max1100:matchMedia('(max-width: 1100px)').matches,max1024:matchMedia('(max-width: 1024px)').matches}, nodes, networkOverviewVisible:shown(nodes.chatops), landingVisible:shown(nodes.composer) }
      }), 1500)
      results.push({ width, status:'OK', ...row })
    } catch (error) { results.push({ width, status:error.message === 'COLLECT_TIMEOUT' ? 'COLLECT_TIMEOUT' : 'COLLECT_ERROR', error:error.message }) }
    await persist()
  }
} catch (error) { results.push({ status:'STARTUP_ERROR', error:error.message }); await persist() }
finally {
  await writeFile(errorsArtifact, JSON.stringify({ browserErrors, failedRequests }, null, 2))
  await persist(); await closeHarness(harness)
}
console.log(JSON.stringify({ runtime_ms:Date.now()-started, completed:results.length, ...summary(results) }))
