/**
 * api/client.js
 * IDN System REST API client — real backend only.
 */

// ── Typed error classes ──────────────────────────────────────────────────────
// Callers can branch on `instanceof` instead of string-matching e.message.

export class ApiTimeoutError extends Error {
  constructor(message) {
    super(message)
    this.name = 'ApiTimeoutError'
  }
}

export class ApiHttpError extends Error {
  constructor(status, message, payload = null) {
    super(message)
    this.name = 'ApiHttpError'
    this.status = status
    this.payload = payload
  }
}

const BASE = '/api/v1'
const DEFAULT_TIMEOUT_MS = 30000
const pendingIdempotencyKeys = new Map()

// ── Safe UUID v4 — triple fallback for maximum compatibility ────────────────
function uuidv4() {
  // 1) Native (Chrome 92+, secure context)
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch (_) { /* fall through */ }

  // 2) crypto.getRandomValues (universal in modern browsers)
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
      return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (crypto.getRandomValues(new Uint8Array(1))[0] & 0x0f) >> (c === 'x' ? 0 : 4)
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
      })
    }
  } catch (_) { /* fall through */ }

  // 3) Last resort — Math.random (non-crypto, sufficient for trace IDs)
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = Math.random() * 16 | 0
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
  })
}

/**
 * Fetch wrapper with AbortController timeout.
 * @param {string} path - API path (without base prefix)
 * @param {object} options - fetch options
 * @param {number} [timeoutMs=30000] - request timeout in ms
 */
// Public deployments authenticate at the server-side reverse proxy.  Do not
// place an IDN API key in a browser bundle; a locally supplied VITE_API_KEY is
// still supported for an explicitly configured development environment.
export const API_KEY = import.meta.env?.VITE_API_KEY || ''

async function request(path, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const url = `${BASE}${path}`
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const externalSignal = options.signal
  const abortFromCaller = () => controller.abort()
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort()
    else externalSignal.addEventListener('abort', abortFromCaller, { once: true })
  }
  try {
    const res = await fetch(url, {
      ...options,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-Trace-Id': uuidv4(),
        ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
        ...options.headers,
      },
      signal: controller.signal,
    })
    if (!res.ok) {
      const body = await res.text()
      let msg = body
      let payload = null
      try {
        payload = JSON.parse(body)
        msg = payload.detail || payload.error?.message || body
      } catch (_) { /* raw text */ }
      throw new ApiHttpError(res.status, `[${res.status}] ${msg}`, payload)
    }
    if (res.status === 204) return null
    return res.json()
  } catch (err) {
    if (err.name === 'AbortError') {
      if (externalSignal?.aborted) throw err
      throw new ApiTimeoutError(`[TIMEOUT] Request to ${path} timed out after ${timeoutMs}ms`)
    }
    throw err
  } finally {
    clearTimeout(timer)
    externalSignal?.removeEventListener('abort', abortFromCaller)
  }
}

/** POST /intents — submit a new intent. Returns { intent_id, status, created_at }. */
export async function submitIntent({ text, priority = 'medium', userId = 'anonymous', sessionId, context, requestId } = {}) {
  const payload = { text, priority, user_id: userId, session_id: sessionId, context }
  const fingerprint = JSON.stringify(payload)
  const idempotencyKey = requestId || pendingIdempotencyKeys.get(fingerprint) || uuidv4()
  pendingIdempotencyKeys.set(fingerprint, idempotencyKey)
  try {
    const response = await request('/intents', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(payload),
    }, 15000)
    pendingIdempotencyKeys.delete(fingerprint)
    return response
  } catch (error) {
    // Keep the key after timeout/network ambiguity so an explicit retry replays
    // the server result instead of creating a second intent.
    throw error
  }
}

/** GET /intents — list intents with optional filters. */
export async function listIntents({ status, userId, page = 1, pageSize = 20 } = {}) {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (userId) params.set('user_id', userId)
  params.set('page', String(page))
  params.set('page_size', String(pageSize))
  return request(`/intents?${params.toString()}`)
}

/** DELETE /intents/{id} — withdraw resources while retaining audit history. */
export async function deleteIntent(intentId) {
  return request(`/intents/${intentId}`, { method: 'DELETE' })
}

/** POST /intents/{id}/cancel — cancel a running or awaiting intent. */
export async function cancelIntent(intentId) {
  return request(`/intents/${intentId}/cancel`, { method: 'POST' })
}

/** GET /intents/{id} — fetch a single intent with milestones and snapshot. */
export async function getIntent(intentId) {
  return request(`/intents/${intentId}`)
}

/** GET /intents/{id}/details — one aggregate, side-effect-free detail read. */
export async function getIntentDetails(intentId, { signal } = {}) {
  return request(`/intents/${intentId}/details`, { signal })
}

/** GET /intents/current — durable, side-effect-free dashboard summary. */
export async function getCurrentIntents({ includeEvidence = false, includeProjectionDetails = true } = {}) {
  const params = new URLSearchParams()
  if (includeEvidence) params.set('include_evidence', 'true')
  if (includeProjectionDetails) params.set('include_projection_details', 'true')
  return request(`/intents/current?${params.toString()}`)
}

export async function getIntentSpec(intentId) {
  return request(`/intents/${intentId}/spec`)
}

export async function getIntentRuntime(intentId) {
  return request(`/intents/${intentId}/runtime`)
}

export async function getIntentPlan(intentId) {
  return request(`/intents/${intentId}/plan`)
}

export async function getIntentExecution(intentId) {
  return request(`/intents/${intentId}/execution`)
}

export async function getIntentAssurance(intentId) {
  return request(`/intents/${intentId}/assurance`)
}

export async function getIntentHistory(intentId) {
  return request(`/intents/${intentId}/history`)
}

export async function getIntentDecisions(intentId) {
  return request(`/intents/${intentId}/decisions`)
}

export async function getIntentLlmCalls(intentId) {
  return request(`/intents/${intentId}/llm-calls`)
}

export async function getIntentEvidence(intentId) {
  return request(`/intents/${intentId}/evidence`)
}

export async function getIntentHealing(intentId) {
  return request(`/intents/${intentId}/healing`)
}

export async function reevaluateIntent(intentId) {
  return request(`/intents/${intentId}/reevaluate`, { method: 'POST' }, 60000)
}

export async function replanIntent(intentId, { deploy = false, reason } = {}) {
  return request(`/intents/${intentId}/replan`, {
    method: 'POST',
    body: JSON.stringify({ deploy, reason: reason || 'operator requested replanning' }),
  }, 180000)
}

export async function withdrawIntent(intentId) {
  return request(`/intents/${intentId}/withdraw`, { method: 'POST' }, 120000)
}

/** Formal scope withdrawal guarded by the effective-owner snapshot. */
export async function withdrawEffectiveIntent(item, { requestId } = {}) {
  const key = requestId || uuidv4()
  return request(`/intents/effective/${encodeURIComponent(item.effective_scope_id)}/withdraw`, {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify({
      expected_policy_id: item.policy_id,
      expected_policy_version: item.policy_version,
      expected_binding_id: item.binding_id,
      reason: 'USER_REQUESTED',
    }),
  }, 120000)
}

export async function updateIntent(intentId, patch, { requestId } = {}) {
  const idempotencyKey = requestId || uuidv4()
  return request(`/intents/${intentId}`, {
    method: 'PATCH',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(patch || {}),
  })
}

/** GET /intents/{id}/decision-report — fetch the generated multi-agent report. */
export async function getDecisionReport(intentId) {
  return request(`/intents/${intentId}/decision-report`)
}

/** GET /intents/{id}/a2a-trace — fetch persisted runtime A2A protocol evidence. */
export async function getA2ATrace(intentId) {
  return request(`/intents/${intentId}/a2a-trace`)
}

/** POST /intents/{id}/clarifications — answer a clarification and resume execution. */
export async function answerClarification(intentId, { text, context } = {}) {
  return request(`/intents/${intentId}/clarifications`, {
    method: 'POST',
    body: JSON.stringify({ text, context }),
  })
}

/** GET /telemetry/current - fetch the live telemetry snapshot. */
export async function getTelemetry({ includeProbe = false, includeIperf = false, serviceId = '' } = {}) {
  const params = new URLSearchParams()
  if (includeProbe) params.set('include_probe', 'true')
  if (includeIperf) params.set('include_iperf', 'true')
  if (serviceId) params.set('service_id', serviceId)
  const query = params.toString()
  return request(`/telemetry/current${query ? `?${query}` : ''}`)
}

export async function getWatcherStatus() {
  return request('/watcher/status')
}

export async function getWatcherIncidents({ limit = 5 } = {}) {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  return request(`/watcher/incidents?${params.toString()}`)
}

export async function getHealthReady() {
  return request('/health/ready')
}

export async function getServices() {
  return request('/services')
}

export async function getWorkloads() {
  return request('/workloads')
}

export async function getPolicies() {
  return request('/system/policies')
}

export async function getResourceLedger() {
  return request('/resources/ledger')
}

export async function getFlows() {
  return request('/system/flows')
}

export async function getQueues() {
  return request('/system/queues')
}

export async function getTelemetryHistory({ limit = 200 } = {}) {
  return request(`/telemetry/history?limit=${encodeURIComponent(limit)}`)
}

export async function getFaults() {
  return request('/faults?include_removed=false')
}

export async function getReconciliationStatus() {
  return request('/reconciliation/status')
}

export async function getAuditEvents({ limit = 200 } = {}) {
  return request(`/audit?limit=${encodeURIComponent(limit)}`)
}

export async function getService(serviceId) {
  return request(`/services/${encodeURIComponent(serviceId)}`)
}

export async function validateService(serviceId, options = {}) {
  return request(`/services/${encodeURIComponent(serviceId)}/validate`, {
    method: 'POST',
    body: JSON.stringify({ service_id: serviceId, ...options }),
  }, 180000)
}

export async function getServiceMetrics(serviceId) {
  return request(serviceId
    ? `/service-metrics/${encodeURIComponent(serviceId)}`
    : '/service-metrics/current')
}

export async function getTrafficScenarios() {
  return request('/traffic-scenarios')
}

export async function runTrafficScenario(scenarioId, options = {}) {
  return request(`/traffic-scenarios/${encodeURIComponent(scenarioId)}/run`, {
    method: 'POST',
    body: JSON.stringify(options),
  }, 180000)
}

// ── Generic SSE stream parser ────────────────────────────────────────────────
// Reads a ReadableStreamDefaultReader, parses SSE wire format, dispatches
// JSON-parsed payloads keyed by event-type.  Pure parser — no fetch, no
// business-logic disambiguation, no side-effects.
//
// @param {ReadableStreamDefaultReader} reader
// @param {{ onData: (eventType: string, data: object|null) => void,
//              onComment?: (comment: string) => void }} callbacks
// @returns {Promise<void>}
async function parseSSEStream(reader, { onData, onComment } = {}) {
  const decoder = new TextDecoder()
  let buffer = ''
  let eventType = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const rawLine of lines) {
      const line = rawLine.trimEnd()
      if (!line) { eventType = ''; continue }
      if (line.startsWith(':')) {
        if (onComment) onComment(line.slice(1).trimStart())
        continue
      }
      if (line.startsWith('event:')) { eventType = line.slice(6).trim(); continue }
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      if (payload === '[DONE]') {
        if (onData) onData('done', null)
        eventType = ''
        continue
      }
      try {
        const data = JSON.parse(payload)
        if (onData) onData(eventType, data)
      } catch (_) { /* skip malformed */ }
      eventType = ''
    }
  }
}

// ── Fetch-based SSE reader — supports custom headers (unlike EventSource) ──
function fetchSSE(url, { headers = {}, onEvent, onError } = {}) {
  const controller = new AbortController()

  fetch(url, {
    headers: { 'X-API-Key': API_KEY, 'X-Trace-Id': uuidv4(), ...headers },
    signal: controller.signal,
  }).then(async (response) => {
    if (!response.ok && onError) { onError(new Error(`SSE ${response.status}`)); return }
    const reader = response.body.getReader()
    await parseSSEStream(reader, {
      onData(eventType, data) {
        if (onEvent) onEvent(eventType, data)
      },
    })
  }).catch((err) => {
    if (err.name !== 'AbortError' && onError) onError(err)
  })

  return () => controller.abort()
}

/** GET /topology/events — SSE stream of topology snapshots. */
export function subscribeTopologyEvents({ onUpdate, onError } = {}) {
  const url = `${BASE}/topology/events?fields=frontend`
  return fetchSSE(url, {
    onEvent(_eventType, data) {
      if (onUpdate) onUpdate(data)
    },
    onError,
  })
}

/** GET /topology/current — fetch a single topology snapshot. */
export async function getTopology() {
  return request('/topology/current')
}

/** GET /intents/{id}/events stream for pipeline progress and runtime A2A traces. */
export function subscribeIntentEvents(intentId, {
  onStageChange, onA2AMessage, onHeartbeat, onThinkingToken, onStreamEnd, onPhaseProgress, onError,
} = {}) {
  const url = `${BASE}/intents/${intentId}/events`

  const handlers = {
    stage_change: onStageChange,
    a2a_message: onA2AMessage,
    heartbeat: onHeartbeat,
    thinking_token: onThinkingToken,
    stream_end: onStreamEnd,
    phase_progress: onPhaseProgress,
  }

  return fetchSSE(url, {
    onEvent(eventType, data) {
      const handler = handlers[eventType]
      if (handler) handler(data)
    },
    onError,
  })
}

/** POST /chat — lightweight chat that does NOT trigger the STAV pipeline. */
export async function chatMessage({ text, userId = 'anonymous', sessionId } = {}) {
  return request('/chat', {
    method: 'POST',
    body: JSON.stringify({ text, user_id: userId, session_id: sessionId }),
  })
}

/** POST /chat/stream — streaming SSE chat with token-by-token output.
 *  Returns the final structured response (reply, intent_detected, etc.)
 *  after consuming the token stream.
 *
 *  @param {{ text, userId?, sessionId?, onToken? }} options
 *  @param {function(string):void} [options.onToken] — called with each token
 *  @returns {Promise<{reply, intent_detected, intent_hint, intent_type, confidence, session_id}>}
 */
export async function chatStream({ text, userId = 'anonymous', sessionId, onToken, timeoutMs = 60000 } = {}) {
  const url = `${BASE}/chat`
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  let response
  try {
    response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': API_KEY,
      'X-Trace-Id': uuidv4(),
    },
      body: JSON.stringify({
        text,
        user_id: userId,
        session_id: sessionId,
      }),
      signal: controller.signal,
    })
  } catch (err) {
    clearTimeout(timer)
    if (err.name === 'AbortError') {
      throw new ApiTimeoutError(`[TIMEOUT] Request to /chat timed out after ${timeoutMs}ms`)
    }
    throw err
  }

  if (!response.ok) {
    const body = await response.text()
    let msg = body
    try {
      const parsed = JSON.parse(body)
      msg = parsed.detail || parsed.error?.message || body
    } catch (_) {
      // raw text
    }
    throw new ApiHttpError(response.status, `[${response.status}] ${msg}`)
  }

  const reader = response.body.getReader()

  let reply = ''
  let result = null
  let streamError = ''
  let latestMeta = {}
  let hasTokenStream = false

  function applyData(data, currentEvent = '') {
    if (!data || typeof data !== 'object') return

    // 记录后台过程中出现过的关键元数据
    latestMeta = {
      ...latestMeta,
      session_id: data.session_id || latestMeta.session_id,
      intent_id: data.intent_id || latestMeta.intent_id,
      intent_type: data.intent_type || latestMeta.intent_type,
      confidence: data.confidence ?? latestMeta.confidence,
      intent_detected: data.intent_detected ?? latestMeta.intent_detected,
    }

    // token 流式输出：只有明确 token 字段才直接追加到页面。
    if (typeof data.token === 'string') {
      hasTokenStream = true
      reply += data.token
      if (onToken) onToken(data.token)
      return
    }

    // 错误事件
    if (data.type === 'error' || currentEvent === 'error') {
      streamError = data.content || data.message || '聊天流返回错误'
      if (onToken && streamError) onToken(streamError)
      return
    }

    // 兼容多种结束格式。注意：必须先判断 final，再处理 content。
    // 有些后台会在最终事件里把完整回复放到 content/reply。
    // 这种完整回复只能作为最终 result.reply，不能再次 onToken，否则页面会重复显示一遍。
    const isFinal =
      data.done ||
      data.final ||
      data.type === 'done' ||
      data.type === 'final' ||
      currentEvent === 'done' ||
      currentEvent === 'final' ||
      currentEvent === 'message_completed' ||
      currentEvent === 'chat_completed' ||
      currentEvent === 'response_completed'

    if (isFinal) {
      result = {
        ...latestMeta,
        ...data,
        reply: data.reply || data.content || reply,
        intent_detected: data.intent_detected ?? Boolean(data.intent_id || latestMeta.intent_id),
        intent_id: data.intent_id || latestMeta.intent_id || null,
        session_id: data.session_id || latestMeta.session_id || sessionId,
      }
      return
    }

    // content 经常是“当前完整快照/最终完整文本”，不是增量 token。
    // 如果前面已经收过 data.token，再把 content onToken 一次就会导致页面重复一遍。
    // 所以：有 token 流时，content 只用于修正最终 reply，不再推给页面。
    if (typeof data.content === 'string' && currentEvent !== 'message_created') {
      const content = data.content

      if (hasTokenStream) {
        // content 是完整快照时，只保留更完整的 reply；绝不 onToken。
        if (content.length > reply.length && content.startsWith(reply)) {
          reply = content
        }
      } else {
        // 没有 token 字段的后台，才允许把明确的 delta/content_delta 当增量显示。
        const isDeltaEvent =
          currentEvent === 'token' ||
          currentEvent === 'delta' ||
          currentEvent === 'content_delta' ||
          currentEvent === 'message_delta' ||
          data.type === 'token' ||
          data.type === 'delta' ||
          data.type === 'content_delta' ||
          data.type === 'message_delta'

        if (isDeltaEvent) {
          reply += content
          if (onToken) onToken(content)
        } else {
          // 普通 content 当作快照/完整内容，只更新 reply，不直接显示。
          reply = content
        }
      }
    }

    if (typeof data.reply === 'string') {
      reply = data.reply
    }
  }

  await parseSSEStream(reader, {
    onData(eventName, data) {
      // [DONE] sentinel
      if (data === null) {
        result = {
          ...latestMeta,
          reply,
          intent_detected: latestMeta.intent_detected ?? Boolean(latestMeta.intent_id),
          intent_id: latestMeta.intent_id || null,
          session_id: latestMeta.session_id || sessionId,
        }
        return
      }
      applyData(data, eventName)
    },
  })

  // 后台可能没有显式 done/message_completed，但 token 已经完整返回了
  if (!result && reply) {
    result = {
      ...latestMeta,
      reply,
      intent_detected: latestMeta.intent_detected ?? Boolean(latestMeta.intent_id),
      intent_id: latestMeta.intent_id || null,
      session_id: latestMeta.session_id || sessionId,
    }
  }

  if (!result) {
    if (streamError) {
      return {
        reply: streamError,
        intent_detected: false,
        intent_id: null,
        session_id: latestMeta.session_id || sessionId,
      }
    }

    return {
      reply: '',
      intent_detected: false,
      intent_id: null,
      session_id: latestMeta.session_id || sessionId,
    }
  }

  clearTimeout(timer)
  return result
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** Lightweight readiness probe used before entering the dashboard.
 *  It verifies only non-mutating backend endpoints.
 *  Do NOT call /chat here: even a "ping" message creates a visible conversation record.
 */
export async function checkSystemReady(_options = {}) {
  await request('/health', {}, 5000)
  await request('/topology/current', {}, 10000)
  return true
}

/** Keep retrying until backend services are ready, or maxAttempts is reached.
 *  Returns `true` when ready, or `false` when maxAttempts exhausted.
 *  Callers should use the return value to decide whether to gate entry or
 *  let the user proceed with graceful degradation. */
export async function waitForSystemReady({ intervalMs = 1500, maxAttempts = 40, onAttempt } = {}) {
  let attempt = 0
  while (attempt < maxAttempts) {
    attempt += 1
    try {
      await checkSystemReady()
      return true
    } catch (error) {
      // Fast-fail on auth errors — retries won't help
      if (error instanceof ApiHttpError && (error.status === 401 || error.status === 403)) {
        return false
      }
      if (onAttempt) onAttempt(error, attempt, maxAttempts)
      await sleep(intervalMs)
    }
  }
  return false
}

/** GET /conversations (reuses sessions endpoint). */
export async function listConversations({ userId, page = 1, pageSize = 50 } = {}) {
  const params = new URLSearchParams()
  if (userId) params.set('user_id', userId)
  params.set('page', String(page))
  params.set('page_size', String(pageSize))
  return request(`/conversations?${params.toString()}`)
}

export const loginOperator = ({ username, password }) => request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) })
export const logoutOperator = () => request('/auth/logout', { method: 'POST' })
export const getCurrentOperator = () => request('/auth/me', {}, 8000)

/**
 * POST /intents/preview — semantic/admission preview only.  The backend
 * contract guarantees this endpoint does not create policy, binding, ONOS,
 * P4Runtime or TC resources.  Callers must still use submitIntent/chat for a
 * formal lifecycle run.
 */
export async function previewIntent({ text, priority = 'medium', userId = 'anonymous', sessionId, context } = {}) {
  return request('/intents/preview', {
    method: 'POST',
    body: JSON.stringify({ text, priority, user_id: userId, session_id: sessionId, context }),
  }, 60000)
}

// Submission visual stores use these names.  Keep the data source authoritative:
// session history and telemetry are read through the existing relative API client.
export async function getConversationTimeline(sessionId, { pageSize = 200 } = {}) {
  return request(`/sessions/${encodeURIComponent(sessionId)}/intents?page_size=${pageSize}`)
}

export async function deleteConversation(sessionId) {
  return request(`/conversations/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

export async function getTelemetryInt() {
  return getTelemetry({ includeProbe: true, includeIperf: false })
}

/** GET /agents — authoritative Agent Card registry for the workflow view. */
export async function getAgents() {
  return request('/agents')
}

/** GET /experience-memory/cases — bounded historical evidence list. */
export async function getExperienceCases({ limit = 20 } = {}) {
  return request(`/experience-memory/cases?limit=${encodeURIComponent(limit)}`)
}

/** GET /intents/{id}/jobs — worker evidence for one intent execution. */
export async function getIntentJobs(intentId) {
  return request(`/intents/${encodeURIComponent(intentId)}/jobs`)
}

/** POST /intents/{id}/approve — explicit operator approval only. */
export async function approveIntent(intentId, { reason = '' } = {}) {
  return request(`/intents/${encodeURIComponent(intentId)}/approve`, {
    method: 'POST',
    body: JSON.stringify({ decision: 'approve', reason }),
  })
}

/** POST /intents/{id}/rollback — explicit operator rollback only. */
export async function rollbackIntent(intentId) {
  return request(`/intents/${encodeURIComponent(intentId)}/rollback`, { method: 'POST' })
}
