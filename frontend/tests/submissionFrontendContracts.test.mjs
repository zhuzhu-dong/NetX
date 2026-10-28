import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import {
  LIFECYCLE_STATUSES,
  INTERACTION_MODE,
  isActivelyExecuting,
  isAwaitingUserInput,
  isCurrentLifecycle,
  isTerminalLifecycle,
  lifecycleInteractionMode,
  lifecyclePresentation,
  normalizeLifecycleStatus,
} from '../src/utils/lifecyclePresentation.js'
import { currentIntentGroups } from '../src/utils/currentIntentPresentation.js'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const client = read('../src/api/client.js')
const router = read('../src/router/index.js')
const studio = read('../src/views/dashboard/NetworkOverview.vue')
const detail = read('../src/views/dashboard/IntentDetail.vue')
const evidence = read('../src/views/dashboard/NetworkEvidence.vue')
const diagnostics = read('../src/views/dashboard/SystemDiagnostics.vue')
const authStore = read('../src/stores/authStore.js')
const dashboard = read('../src/views/Dashboard.vue')
const demoAuth = read('../src/auth/competitionDemoAuth.js')
const login = read('../src/views/Login.vue')
const intentStore = read('../src/stores/intentStore.js')

test('1 API base is same-origin relative', () => assert.match(client, /const BASE = '\/api\/v1'/))
test('2 browser client has no default API key', () => assert.match(client, /VITE_API_KEY \|\| ''/))
test('3 browser client contains no known server credential names', () => {
  for (const secret of ['IDN_API_KEY', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY', 'WANGTIAO2026_API_KEY', 'IDN_PROXY_API_KEY']) assert.doesNotMatch(client, new RegExp(secret))
})
test('4 browser client contains no local backend origin', () => assert.doesNotMatch(client, /(?:localhost|127\.0\.0\.1):8000/))
test('5 preview uses the formal read-only endpoint', () => assert.match(client, /request\('\/intents\/preview'/))
test('6 submit uses the formal intent endpoint', () => assert.match(client, /request\('\/intents',/))
test('7 clarification uses the continuation endpoint', () => assert.match(client, /\/intents\/\$\{intentId\}\/clarifications/))
test('8 withdrawal uses the formal withdrawal endpoint', () => assert.match(client, /\/intents\/\$\{intentId\}\/withdraw/))
test('9 current intents use the current projection endpoint', () => assert.match(client, /\/intents\/current/))
test('10 intent detail uses aggregate details endpoint', () => assert.match(detail, /getIntentDetails/))
test('11 evidence is loaded from real evidence API', () => assert.match(evidence, /getIntentRuntimeData|api\.get|useIntentRuntimeData/))
test('12 diagnostics reads health and topology APIs', () => {
  assert.match(diagnostics, /getHealthReady/)
  assert.match(diagnostics, /getTopology/)
})
test('13 topology uses the real topology client', () => assert.match(client, /request\('\/topology\/current'\)/))
test('14 telemetry uses the real telemetry client', () => assert.match(client, /request\(`\/telemetry\/current/))
test('15 intent studio exposes read-only preview', () => {
  assert.match(studio, /预览 \/ 尚未执行/)
  assert.match(studio, /previewIntentOnly/)
})
test('16 intent studio has a separate formal run action', () => assert.match(studio, /@submit\.prevent="runIntent"/))
test('17 competition router restores main-style local demo-session authentication', () => {
  assert.match(router, /auth\.refresh\(\)/)
  assert.match(router, /name: 'login'/)
  assert.match(demoAuth, /netpulse-local-account/)
  assert.match(demoAuth, /netpulse-auth-session/)
})
test('18 router supports intent detail deep links', () => assert.match(router, /intents\/:intentId\/detail/))
test('19 router supports evidence deep links', () => assert.match(router, /evidence\/:intentId/))
test('20 router supports topology and runtime routes', () => {
  assert.match(router, /path: 'topology'/)
  assert.match(router, /path: 'runtime'/)
})
test('21 lifecycle adapter contains the required canonical statuses', () => {
  for (const status of ['UNKNOWN', 'PREVIEW', 'PENDING', 'PLANNING', 'EVALUATED', 'AWAITING_USER_INPUT', 'RUNNING', 'SATISFIED', 'UNSATISFIABLE', 'FAILED', 'DEGRADED', 'WITHDRAWING', 'WITHDRAW_PENDING_CLEANUP', 'WITHDRAWN']) assert.ok(LIFECYCLE_STATUSES.includes(status))
})
test('22 lifecycle mapping never promotes planning to success', () => {
  assert.equal(normalizeLifecycleStatus('planning'), 'PLANNING')
  assert.equal(lifecyclePresentation('SATISFIED').tone, 'success')
  assert.equal(isTerminalLifecycle('PLANNING'), false)
})
test('23 withdrawn intents cannot be current-effective', () => {
  assert.equal(isCurrentLifecycle('WITHDRAWN'), false)
  const grouped = currentIntentGroups({ items: [{ lifecycle: 'WITHDRAWN' }, { lifecycle: 'SATISFIED' }] })
  assert.equal(grouped.enforcing.length, 1)
  assert.equal(grouped.processing.length + grouped.attention.length, 0)
})

test('24 unauthenticated dashboard redirects and demo authentication survives refresh', () => {
  assert.match(router, /router\.beforeEach\(async/)
  assert.match(router, /return \{ name: 'login', query: \{ redirect: to\.fullPath \} \}/)
  assert.match(authStore, /createCompetitionDemoAuth/)
  assert.match(authStore, /demoAuth\.currentUser\(\)/)
})

test('25 real API client remains secret-free while competition login avoids operator APIs', () => {
  assert.match(client, /credentials: 'include'/)
  assert.match(client, /\/auth\/login/)
  assert.match(client, /\/auth\/logout/)
  assert.match(client, /\/auth\/me/)
  assert.doesNotMatch(authStore, /loginOperator|getCurrentOperator|logoutOperator/)
})

test('26 sidebar topology and agent actions have concrete routes', () => {
  assert.match(dashboard, /to="\/dashboard\/topology"/)
  assert.match(dashboard, /to="\/dashboard\/workflow"/)
  assert.doesNotMatch(dashboard, /:class="\{ disabled: !agentDetailIntentId \}"/)
  assert.match(router, /path: 'topology', name: 'topology'/)
})

test('27 workflow page imports only implemented authoritative API adapters', () => {
  for (const name of ['getAgents', 'getExperienceCases', 'getIntentJobs', 'approveIntent', 'rollbackIntent']) {
    assert.match(client, new RegExp(`export async function ${name}\\(`))
  }
})

test('28 main-style demo registration/login stores no plaintext password or API credential', () => {
  assert.match(login, /注册演示账号/)
  assert.match(demoAuth, /verifier/)
  assert.doesNotMatch(demoAuth, /password:\s*password/)
  for (const secret of ['IDN_API_KEY', 'OPENAI_API_KEY', 'DASHSCOPE_API_KEY', 'IDN_SESSION_SECRET', 'IDN_OPERATOR_PASSWORD_HASH', 'demo-key-xxx']) assert.doesNotMatch(demoAuth, new RegExp(secret))
})

test('29 cancellation lifecycle is terminal only after authoritative backend acknowledgement', () => {
  for (const status of ['CANCELLING', 'CANCELLED', 'CANCELLED_AFTER_COMMIT']) assert.ok(LIFECYCLE_STATUSES.includes(status))
  assert.equal(isTerminalLifecycle('CANCELLING'), false)
  assert.equal(isTerminalLifecycle('CANCELLED'), true)
  assert.equal(isTerminalLifecycle('CANCELLED_AFTER_COMMIT'), true)
  assert.equal(normalizeLifecycleStatus('completed'), 'SATISFIED')
  assert.equal(normalizeLifecycleStatus('rejected'), 'UNSATISFIABLE')
  assert.equal(normalizeLifecycleStatus('cancelled_after_commit'), 'CANCELLED_AFTER_COMMIT')
})

test('30 composer cancellation calls the real API, keeps polling, and never fabricates a terminal state', () => {
  assert.match(client, /request\(`\/intents\/\$\{intentId\}\/cancel`/)
  assert.match(studio, /@click="stopActiveExecution"/)
  assert.match(studio, /正在终止\.\.\./)
  assert.match(intentStore, /await api\.cancelIntent\(runtimeId\)/)
  assert.match(intentStore, /await refreshIntent\(runtimeId\)/)
  assert.match(intentStore, /await loadIntents\(\)/)
  assert.doesNotMatch(intentStore, /record\.status = 'cancelled'/)
  assert.match(intentStore, /isSubmitting\.value = false/)
  assert.match(intentStore, /reconcileActiveExecution\(\)/)
})

test('31 awaiting-user-input is not active execution and has an explicit interaction mode', () => {
  assert.equal(normalizeLifecycleStatus('pending_clarification'), 'AWAITING_USER_INPUT')
  assert.equal(normalizeLifecycleStatus('CLARIFICATION_REQUIRED'), 'AWAITING_USER_INPUT')
  assert.equal(isAwaitingUserInput('pending_clarification'), true)
  assert.equal(isActivelyExecuting('pending_clarification'), false)
  assert.equal(isActivelyExecuting('PLANNING'), true)
  assert.equal(lifecycleInteractionMode('pending_clarification'), INTERACTION_MODE.AWAITING_INPUT)
  assert.equal(lifecycleInteractionMode('CANCELLED'), INTERACTION_MODE.TERMINAL)
})

test('32 composer locks only an explicit active execution or an in-flight HTTP submission', () => {
  assert.match(intentStore, /isSubmitting\.value \|\| interactionMode\.value === INTERACTION_MODE\.ACTIVE/)
  assert.match(intentStore, /isActivelyExecuting\(item\.lifecycle \|\| item\.rawStatus \|\| item\.status\)/)
  assert.match(intentStore, /isAwaitingUserInput\(lifecycle\)/)
  assert.match(studio, /发送补充信息/)
  assert.match(studio, /network\.INTERACTION_MODE\.AWAITING_INPUT/)
})

test('33 missing or unknown lifecycle never becomes an implicit active execution', () => {
  assert.equal(normalizeLifecycleStatus(''), 'UNKNOWN')
  assert.equal(normalizeLifecycleStatus('legacy-unrecognized-state'), 'UNKNOWN')
  assert.equal(isActivelyExecuting(''), false)
  assert.equal(lifecycleInteractionMode(''), INTERACTION_MODE.TERMINAL)
})

test('34 terminal aliases win even when a legacy payload omits phase', () => {
  for (const status of ['success', 'succeeded', 'completed', 'failed', 'rejected', 'unsatisfiable', 'cancelled', 'cancelled_after_commit']) {
    assert.equal(isTerminalLifecycle(status), true, status)
    assert.equal(lifecycleInteractionMode(status), INTERACTION_MODE.TERMINAL, status)
  }
  assert.equal(lifecycleInteractionMode('pending_clarification'), INTERACTION_MODE.AWAITING_INPUT)
  assert.equal(lifecycleInteractionMode('running'), INTERACTION_MODE.ACTIVE)
})

test('35 terminal detail reconciliation clears stale execution ownership before interaction mode is derived', () => {
  assert.match(intentStore, /if \(isTerminalStatus\(selectedStatus\)\) return INTERACTION_MODE\.TERMINAL/)
  assert.match(intentStore, /clearTerminalExecutionState\(record, data\.intent_id\)/)
  assert.match(intentStore, /record\.pendingTurnId = ''/)
  assert.match(intentStore, /source\.lifecycle_status/)
})

test('36 the primary assistant route remains lazy, while secondary operational views remain lazy', () => {
  assert.match(router, /const NetworkOverview = \(\) => import\('\.\.\/views\/dashboard\/NetworkOverview\.vue'\)/)
  assert.match(router, /const WorkflowPage = \(\) => import/)
})

test('37 lifecycle normalization accepts an explicit null selected intent before RouterView mounts the composer', () => {
  assert.match(intentStore, /function authoritativeLifecycleValue\(raw\)/)
  assert.match(intentStore, /const source = raw \|\| \{\}/)
  assert.match(intentStore, /source\.status/)
})
