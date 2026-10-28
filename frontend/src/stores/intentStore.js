/**
 * intentStore.js — 意图生命周期管理
 *
 * 从 network.js 拆分。职责：意图 CRUD、SSE 订阅、轮询、对话消息。
 * SSE/轮询代码保留以兼容 AgentRun.vue 的意图详情 SSE 订阅。
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import * as api from '../api/client'
import { useEventBus } from '../composables/useEventBus'
import { usePipelineStore } from './pipelineStore'
import { useIntentStreaming } from '../composables/useIntentStreaming'
import { buildExecutionSummary } from '../composables/useReportBuilder'
import {
  INTERACTION_MODE,
  isActivelyExecuting,
  isAwaitingUserInput,
  isTerminalLifecycle,
  lifecyclePresentation,
  normalizeLifecycleStatus,
} from '../utils/lifecyclePresentation.js'

const STATUS_CLASS = {
  pending: 'pending',
  sensing: 'running',
  thinking: 'running',
  checking: 'running',
  acting: 'running',
  verifying: 'running',
  success: 'success',
  failed: 'failed',
  cancelled: 'cancelled',
  pending_approval: 'pending',
  pending_clarification: 'clarification',
}

const STATUS_LABEL = {
  pending: 'planning',
  sensing: 'running',
  thinking: 'running',
  checking: 'running',
  acting: 'running',
  verifying: 'running',
  success: 'success',
  failed: 'failed',
  cancelled: 'cancelled',
  pending_approval: 'approval',
  pending_clarification: 'clarification',
}

/**
 * Interaction mode — governs whether the user can freely type, must respond
 * within a constrained context, or is blocked entirely.
 *
 *   'idle'       — No active intent; free-form input allowed.
 *   'blocked'    — Agent is computing; input must be disabled to prevent races.
 *   'awaiting'   — Agent is waiting for the user (clarification / approval);
 *                  input is enabled but scoped to the active intent's context.
 */
function deriveInteractionMode(selectedId, runningSet, selectedStatus, sseReconnecting) {
  // A terminal lifecycle is authoritative over a stale list-derived run id,
  // reconnecting SSE transport, or a job row that has not yet been compacted.
  if (isTerminalStatus(selectedStatus)) return INTERACTION_MODE.TERMINAL
  if (isAwaitingUserInput(selectedStatus)) return INTERACTION_MODE.AWAITING_INPUT
  if (sseReconnecting || runningSet.has(selectedId) || isActivelyExecuting(selectedStatus)) {
    return INTERACTION_MODE.ACTIVE
  }
  return INTERACTION_MODE.TERMINAL
}

/**
 * Whether the status represents a terminal state (no further transitions).
 */
function isTerminalStatus(status) {
  return isTerminalLifecycle(status)
}

function projectLifecycle(status) {
  const presentation = lifecyclePresentation(status)
  return {
    status: presentation.label,
    statusClass: presentation.tone,
    canonical: presentation.status,
  }
}

function authoritativeLifecycleValue(raw) {
  // Status is the persisted API result.  The normalized backend field is a
  // compatibility bridge for current-card projections; phase is last because
  // a legacy response can omit it even after an explicit terminal status.
  // `activeIntent` is intentionally null before a history selection. A
  // default parameter only handles undefined, so normalize null explicitly
  // before this startup-time projection reads any lifecycle fields.
  const source = raw || {}
  return source.status
    || source.lifecycle_status
    || source.intent_lifecycle
    || source.lifecycle
    || source.phase
    || source.snapshot?.phase
    || ''
}

function clearTerminalExecutionFields(record) {
  if (!record) return record
  record.activeRunId = ''
  record.pendingTurnId = ''
  record.pendingAssistantMessageId = ''
  record.activeTurnId = ''
  record.activeTurnIntentId = ''
  return record
}
const STAGE_ORDER = [
  'sense',
  'sensing',
  'parse',
  'parse_intent',
  'check',
  'check_conflict',
  'generate',
  'generate_policy',
  'validate',
  'validate_yang',
  'execute',
  'execute_flow',
  'verify',
  'verify_status',
  'human_approval',
]

function normalizeStageName(stage = '') {
  const value = String(stage || '').toLowerCase()
  if (value === 'sensing') return 'sense'
  if (value === 'checking') return 'check'
  if (value === 'acting') return 'execute'
  if (value === 'verifying') return 'verify'
  return value
}

function stageRank(stage = '') {
  const normalized = normalizeStageName(stage)
  const index = STAGE_ORDER.findIndex((item) => normalizeStageName(item) === normalized)
  return index >= 0 ? index : 999
}

function normalizeMilestone(item = {}) {
  const stage = item.stage || item.name || ''
  const summary = item.summary || item.content || item.text || ''

  if (!stage && !summary) return null

  return {
    ...item,
    stage,
    summary,
    status: item.status || 'running',
    agent_id: item.agent_id || item.agentId || item.agent || stage || 'agent',
    created_at: item.created_at || item.createdAt || new Date().toISOString(),
    updated_at: item.updated_at || item.updatedAt || new Date().toISOString(),
  }
}

function mergeMilestones(current = [], incoming = []) {
  const map = new Map()

  const add = (raw) => {
    const item = normalizeMilestone(raw)
    if (!item) return

    const stage = normalizeStageName(item.stage)
    const agent = item.agent_id || item.agentId || ''
    const key = item.id || `${stage || 'unknown'}::${agent}`

    const old = map.get(key)
    if (!old) {
      map.set(key, item)
      return
    }

    map.set(key, {
      ...old,
      ...item,
      summary: item.summary || old.summary,
      content: item.content || old.content,
      text: item.text || old.text,
      created_at: old.created_at || item.created_at,
      updated_at: item.updated_at || old.updated_at || new Date().toISOString(),
    })
  }

  current.forEach(add)
  incoming.forEach(add)

  return Array.from(map.values()).sort((a, b) => {
    const rankDiff = stageRank(a.stage) - stageRank(b.stage)
    if (rankDiff !== 0) return rankDiff
    return String(a.created_at || '').localeCompare(String(b.created_at || ''))
  })
}

function cleanResultText(value) {
  if (value === undefined || value === null) return ''
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  const normalized = String(text || '').replace(/\s+/g, ' ').trim()
  return ['none', 'null', 'undefined', '{}', '[]'].includes(normalized.toLowerCase())
    ? ''
    : normalized
}

function finalReplyForRuntime(runtime = {}) {
  const result = runtime.result || {}
  const decision = result.decision_report || {}
  const error = result.error
  const status = String(runtime.statusClass || runtime.status || '').toLowerCase()
  const failed = status.includes('fail') || status.includes('error')
  const cancelled = status.includes('cancel')

  const candidates = failed
    ? [
        decision.final_conclusion,
        decision.summary,
        result.failure_reason,
        result.error_message,
        result.message,
        typeof error === 'object' ? error.message : error,
        typeof error === 'object' ? error.detail : '',
        result.detail,
        result.details,
      ]
    : [decision.final_conclusion, decision.summary, result.message]

  const detail = candidates.map(cleanResultText).find(Boolean)
  if (failed) {
    return detail
      ? `执行失败：${detail.replace(/^执行失败[:：]?\s*/, '')}`
      : '执行失败：系统未返回明确原因，可能是拓扑不可用、参数不足、策略冲突或策略下发失败。'
  }
  if (cancelled) return '本次网络意图已取消，未继续下发网络策略。'
  return detail || '本次网络意图已执行完成，结果已同步到拓扑与系统详情。'
}

function fallbackExecutionSummary(intentId, runtime = {}, finalReply = '') {
  const status = String(runtime.statusClass || runtime.status || '').toLowerCase()
  const failed = status.includes('fail') || status.includes('error')
  const cancelled = status.includes('cancel')
  const stageCount = (runtime.milestones || []).length

  return {
    status: failed ? '执行失败' : cancelled ? '已取消' : '执行完成',
    conclusion: finalReply,
    metrics: [
      { label: '执行阶段', value: `${stageCount}` },
      { label: '当前状态', value: failed ? '失败' : cancelled ? '已取消' : '完成' },
      { label: '意图编号', value: String(intentId || '').slice(-8) || '-' },
      { label: '验证结果', value: failed ? '未通过' : cancelled ? '未执行' : '已完成' },
      { label: '拓扑同步', value: runtime.result ? '已同步' : '无结果' },
      { label: '执行记录', value: stageCount ? '已保存' : '暂无' },
    ],
    kpiVerification: null,
    pathChange: null,
    keyActions: [],
    diagnosis: failed ? { root_cause: finalReply.replace(/^执行失败[:：]?\s*/, '') } : null,
    candidateSolutions: [],
    agentVotes: [],
    evidence: (runtime.milestones || [])
      .slice(-4)
      .map((item) => cleanResultText(item.summary || item.content || item.text))
      .filter(Boolean),
  }
}
function toIntentRecord(raw) {
  const lifecycleValue = authoritativeLifecycleValue(raw)
  const projected = projectLifecycle(lifecycleValue)
  return {
    id: raw.intent_id,
    sessionId: raw.session_id || raw.sessionId || '',
    runIds: raw.intent_id ? [raw.intent_id] : [],
    activeRunId: raw.activeRunId || raw.intent_id || '',
    title: inferTitle(raw.text || ''),
    text: raw.text || '',
    owner: raw.user_id || raw.owner || 'anonymous',
    status: projected.status,
    statusClass: projected.statusClass,
    lifecycle: projected.canonical,
    rawStatus: lifecycleValue,
    priority: raw.priority || 'medium',
    createdAt: raw.created_at,
milestones: mergeMilestones([], raw.milestones || raw.snapshot?.milestones || []),    snapshot: raw.snapshot || null,
    result: raw.result || null,
    content: raw.text || '',
    dialogMessages: Array.isArray(raw.dialogMessages) ? raw.dialogMessages : [],
    intentResults: raw.intentResults || {},
    finalPresentation: raw.finalPresentation || {},
    intentTransport: raw.intentTransport || {},
    pendingTurnId: raw.pendingTurnId || '',
    pendingAssistantMessageId: raw.pendingAssistantMessageId || '',
    activeTurnId: raw.activeTurnId || '',
    activeTurnIntentId: raw.activeTurnIntentId || '',
  }
}

function inferTitle(text) {
  if (!text) return '未命名意图'
  // Truncate to first 24 characters — honest, does not pretend AI understanding
  return text.length > 24 ? text.slice(0, 24) + '…' : text
}

function toConversationRecord(raw) {
  const sessionId = raw.session_id || raw.sessionId || raw.id || ''
  const title = inferTitle(raw.title || raw.text || '新的对话')
  return {
    id: sessionId,
    sessionId,
    runIds: [],
    activeRunId: raw.activeRunId || '',
    title,
    text: raw.title || title,
    owner: raw.user_id || raw.owner || 'operator',
    status: raw.status || 'conversation',
    statusClass: raw.statusClass || 'success',
    priority: raw.priority || 'medium',
    createdAt: raw.created_at || raw.createdAt,
    updatedAt: raw.updated_at || raw.updatedAt,
    milestones: [],
    snapshot: null,
    result: { chat_only: true, intent_detected: false },
    content: raw.title || title,
    dialogMessages: Array.isArray(raw.dialogMessages) ? raw.dialogMessages : [],
    intentResults: raw.intentResults || {},
    finalPresentation: raw.finalPresentation || {},
    intentTransport: raw.intentTransport || {},
    pendingTurnId: raw.pendingTurnId || '',
    pendingAssistantMessageId: raw.pendingAssistantMessageId || '',
    activeTurnId: raw.activeTurnId || '',
    activeTurnIntentId: raw.activeTurnIntentId || '',
    isConversation: true,
  }
}

function toIntentRuntimeRecord(detail = {}) {
  const status = authoritativeLifecycleValue(detail)
  const projected = projectLifecycle(status)
  return {
    id: detail.intent_id || detail.id || '',
    status,
    statusClass: projected.statusClass,
    lifecycle: projected.canonical,
    result: detail.result || null,
milestones: mergeMilestones([], detail.milestones || detail.snapshot?.milestones || []),    snapshot: detail.snapshot || null,
    text: detail.text || '',
    updatedAt: detail.updated_at || detail.updatedAt || detail.created_at || '',
  }
}

function timelineEntryId(entry, sessionId, index) {
  return entry.id || entry.message_id || `${sessionId}-timeline-${index}`
}

const USER_TIMELINE_TYPES = new Set([
  'user_text',
  'clarification_answer',
  'approval_decision',
])

const ASSISTANT_VISIBLE_TYPES = new Set([
  'assistant_text',
  'clarification_question',
  'error',
])

const INTERNAL_TIMELINE_TYPES = new Set([
  'intent',
  'intent_submitted',
  'assistant_thinking',
  'stage_progress',
  'approval_required',
  'execution_result',
  'verification_result',
  'system',
])

const TIMELINE_TAG_LABELS = {
  assistant_text: 'NetPulse',
  clarification_question: '需要补充信息',
  clarification_answer: 'operator',
  approval_decision: 'operator',
  error: 'NetPulse',
  intent_submitted: '深度思考',
  stage_progress: '执行过程',
  execution_result: '执行结果',
  verification_result: '验证结果',
  status: 'NetPulse',
  system: '深度思考',
}

function normalizeTimelineContent(type, content) {
  const text = String(content || '').trim()
  if (type === 'system' && text.includes('已收到补充')) {
    return '已收到补充信息，正在继续执行该网络意图。'
  }
  return text
}

function normalizeTimelineMessages(entries = [], sessionId = 'conversation') {
  const finalByOriginal = new Map()
  for (const entry of entries) {
    const originalId = entry.metadata?.original_message_id || entry.payload?.original_message_id
    if (originalId && entry.metadata?.final !== false) {
      finalByOriginal.set(originalId, entry)
    }
  }

  return entries
    .filter((entry) => {
      const id = entry.id || entry.message_id
      return !(id && finalByOriginal.has(id))
    })
    .map((entry, index) => {
      const metadata = entry.metadata || entry.payload || {}
      const type = String(entry.type || entry.message_type || '').trim().toLowerCase()
      const content = normalizeTimelineContent(type, entry.content)
      if (type === 'intent') return null

      const role = USER_TIMELINE_TYPES.has(type) ? 'user' : 'assistant'
      const isExecutionResult = type === 'execution_result'
      const isAssistantVisible = ASSISTANT_VISIBLE_TYPES.has(type)
        || (type === 'system' && content.includes('已收到补充信息'))
      const isInternal = INTERNAL_TIMELINE_TYPES.has(type) && !isAssistantVisible
      return {
        id: timelineEntryId(entry, sessionId, index),
        role,
        tag: TIMELINE_TAG_LABELS[type] || (role === 'user' ? 'operator' : 'NetPulse'),
        messageType: type,
        content,
        intentId: entry.intent_id || '',
        action: isExecutionResult ? 'topology' : '',
        report: metadata && isExecutionResult ? metadata : null,
        thoughtOnly: role === 'assistant' && isInternal,
      }
    })
    .filter((message) => message?.content)
}

function extractTimelineIntentIds(entries = []) {
  return [...new Set(entries.map((entry) => entry.intent_id).filter(Boolean))]
}

export const useIntentStore = defineStore('intent', () => {
  /* ---- helpers ---- */
  const _bus = () => useEventBus()
  const _log = (msg) => _bus().emit('log:add', msg)
  // Direct store method calls replace 1:1 EventBus emission.
  // log:add remains on EventBus (genuine 1:N cross-cutting concern).
  const _pipelineReset = () => usePipelineStore().resetPipeline()
  const _pipelineMilestones = (milestones) => usePipelineStore()._updatePipelineFromMilestones(milestones)
  const _pipelineFromStatus = (status) => usePipelineStore().setPipelineFromStatus(status)

  /* ---- state ---- */
  const intentHistory = ref([])
  const conversationHistory = ref([])
  const selectedIntentId = ref(null)
  const intentText = ref('')
  const clarificationAnswer = ref('')
  const isAnsweringClarification = ref(false)
  const priority = ref('High')
  const isComposing = ref(false)

  // ── Streaming state (token-level typewriter effect) ──
  /** Token buffer: [{seq, token}, ...]. Sorted on stream_end for ordered display. */
  const _tokenBuffer = ref([])
  /** Computed streaming text from ordered token buffer. */
  const streamingText = computed(() => {
    const sorted = [..._tokenBuffer.value].sort((a, b) => a.seq - b.seq)
    return sorted.map(t => t.token).join('')
  })
  /** True while a `thinking_token` SSE stream is active. */
  const isStreaming = ref(false)
  /** The intent_id that the current stream belongs to. */
  const streamingIntentId = ref(null)
  /** Timer for auto-ending stuck streams (30s no new token + no stream_end). */
  let _streamTimeout = null
  /** Set of all non-terminal intent IDs (for global interaction mode). */
  const runningIntents = ref(new Set())
  function clearTerminalExecutionState(record, runtimeId = '') {
    const ownedRuntime = runtimeId || record?.activeRunId || record?.id
    clearTerminalExecutionFields(record)
    if (ownedRuntime) {
      const next = new Set(runningIntents.value)
      next.delete(ownedRuntime)
      runningIntents.value = next
    }
    return record
  }
  // Submission and cancellation requests are transport state only. They are
  // never a substitute for the persisted lifecycle returned by the backend.
  const isSubmitting = ref(false)
  const cancellingIntentIds = ref(new Set())
  const cancellationError = ref('')
  /** Last known session id — used to chain multi-turn messages. */
  const _lastSessionId = ref(null)
  /** Invalidates slower history requests when the user selects another chat. */
  let _conversationSelectionSeq = 0

  function _resetTokenStream(ownerId = '') {
    if (ownerId && streamingIntentId.value && streamingIntentId.value !== ownerId) return
    if (_streamTimeout) {
      clearTimeout(_streamTimeout)
      _streamTimeout = null
    }
    _tokenBuffer.value = []
    isStreaming.value = false
    streamingIntentId.value = null
  }

  function _clearTransientAgentState(record, { turnId = '', intentId = '' } = {}) {
    if (record) {
      const clearAll = !turnId && !intentId
      const ownsPendingTurn = Boolean(turnId && record.pendingTurnId === turnId)
      const ownsActiveTurn = Boolean(turnId && record.activeTurnId === turnId)
      const ownsIntent = Boolean(intentId && record.activeTurnIntentId === intentId)

      if (clearAll || ownsPendingTurn || ownsIntent) {
        record.pendingTurnId = ''
        record.pendingAssistantMessageId = ''
      }
      if (clearAll || ownsActiveTurn || ownsIntent) {
        record.activeTurnId = ''
        record.activeTurnIntentId = ''
      }
      _replaceHistoryRecord(record)
    }

    _resetTokenStream(intentId || turnId)
  }

  function _findRecordByRuntimeId(intentId, sessionId = '') {
    return intentHistory.value.find((i) => (
      i.id === intentId
      || i.activeRunId === intentId
      || (Array.isArray(i.runIds) && i.runIds.includes(intentId))
      || (sessionId && (i.sessionId === sessionId || i.id === sessionId))
    ))
  }

  function _firstUserTitle(record) {
    return inferTitle(
      (record?.dialogMessages || []).find((message) => message.role === 'user')?.content
      || record?.text
      || record?.content
      || record?.title
      || '新的对话'
    )
  }

  function _syncConversationSummary(record, overrides = {}) {
    const sessionId = overrides.session_id || overrides.sessionId || record?.sessionId || record?.id
    if (!sessionId) return

    const summary = toConversationRecord({
      ...record,
      ...overrides,
      session_id: sessionId,
      title: overrides.title || _firstUserTitle(record),
      updated_at: overrides.updated_at || new Date().toISOString(),
    })

    const index = conversationHistory.value.findIndex((item) => item.sessionId === sessionId || item.id === sessionId)
    if (index >= 0) {
      const next = [...conversationHistory.value]
      next[index] = {
        ...next[index],
        ...summary,
        title: summary.title || next[index].title,
      }
      conversationHistory.value = next
    } else {
      conversationHistory.value = [summary, ...conversationHistory.value]
    }
  }

  function _replaceHistoryRecord(record) {
    if (!record?.id) return
    const index = intentHistory.value.findIndex((item) => (
      item.id === record.id
      || (record.sessionId && item.sessionId === record.sessionId)
      || (record.activeRunId && item.activeRunId === record.activeRunId)
    ))
    if (index >= 0) {
      intentHistory.value[index] = { ...intentHistory.value[index], ...record }
    }
  }

  function _setIntentTransportState(intentId, update = {}) {
    const record = _findRecordByRuntimeId(intentId)
    if (!record || !intentId) return

    const current = record.intentTransport?.[intentId] || {}
    record.intentTransport = {
      ...(record.intentTransport || {}),
      [intentId]: {
        ...current,
        ...update,
        updatedAt: new Date().toISOString(),
      },
    }
    _replaceHistoryRecord(record)
  }

  function _setDialogMessages(record, messages = []) {
    if (!record) return []
    const nextMessages = [...messages]
    record.dialogMessages = nextMessages
    _replaceHistoryRecord(record)
    return nextMessages
  }

function _appendDialogMessage(record, message) {
  if (!record || (!message?.content && !message?.pending)) return []
  const currentMessages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
  const exists = message.id && currentMessages.some((item) => item.id === message.id)
    const nextMessages = exists
      ? currentMessages.map((item) => item.id === message.id ? { ...item, ...message } : item)
      : [...currentMessages, message]
    return _setDialogMessages(record, nextMessages)
  }

  function _updateDialogMessage(record, message) {
    if (!record || !message?.id) return []
    const currentMessages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
    const index = currentMessages.findIndex((item) => item.id === message.id)
    const nextMessages = [...currentMessages]
    if (index >= 0) {
      nextMessages[index] = { ...nextMessages[index], ...message }
    } else {
      nextMessages.push(message)
  }
  return _setDialogMessages(record, nextMessages)
}

  function _dialogMessageIntentId(message = {}) {
    return message.intentId || message.intent_id || ''
  }

  function _isTerminalErrorMessage(message = {}) {
    const type = String(message.messageType || message.message_type || '').trim().toLowerCase()
    return message.role === 'assistant' && type === 'error'
  }

  function _isBareTerminalErrorMessage(message = {}) {
    if (!_isTerminalErrorMessage(message)) return false
    const content = cleanResultText(message.content)
    return /^(?:x|error|failed|失败|❌|✖|✕|×)[\s:：.!。！?？-]*$/i.test(content)
  }

  /**
   * Keep a terminal reply inside its own intent turn. History hydration runs
   * after the complete timeline has arrived, so appending here would place
   * every intent's conclusion at the end of the conversation and detach its
   * execution summary from the reply that owns it.
   */
  function _placeFinalDialogMessage(record, intentId, message) {
    if (!record || !intentId || !message?.content) return message?.id || ''

    const currentMessages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
    const nextMessages = [...currentMessages]
    const normalizedFinal = cleanResultText(message.content)
    const sameIntent = (item) => _dialogMessageIntentId(item) === intentId

    let targetIndex = nextMessages.findIndex((item) => item.id === message.id)
    if (targetIndex < 0) {
      targetIndex = nextMessages.findIndex((item) => (
        item.role === 'assistant'
        && sameIntent(item)
        && cleanResultText(item.content) === normalizedFinal
      ))
    }
    if (targetIndex < 0) {
      for (let index = nextMessages.length - 1; index >= 0; index -= 1) {
        if (sameIntent(nextMessages[index]) && _isTerminalErrorMessage(nextMessages[index])) {
          targetIndex = index
          break
        }
      }
    }

    const finalMessage = {
      ...(targetIndex >= 0 ? nextMessages[targetIndex] : {}),
      ...message,
      role: 'assistant',
      tag: 'NetPulse',
      messageType: 'assistant_text',
      intentId,
      finalOutcome: true,
    }

    if (targetIndex >= 0) {
      nextMessages[targetIndex] = finalMessage
    } else {
      let lastOwnedIndex = -1
      nextMessages.forEach((item, index) => {
        if (sameIntent(item)) lastOwnedIndex = index
      })
      nextMessages.splice(lastOwnedIndex >= 0 ? lastOwnedIndex + 1 : nextMessages.length, 0, finalMessage)
    }

    const cleanedMessages = nextMessages.filter((item) => !(
      item.id !== finalMessage.id
      && sameIntent(item)
      && _isBareTerminalErrorMessage(item)
    ))
    _setDialogMessages(record, cleanedMessages)
    return finalMessage.id
  }

  function _bindExchangeToIntent(record, messageIds = [], intentId = '') {
    if (!record || !intentId || !messageIds.length) return
    const idSet = new Set(messageIds.filter(Boolean))
    const currentMessages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
    record.dialogMessages = currentMessages.map((message) => (
      idSet.has(message.id)
        ? { ...message, intentId }
        : message
    ))
    _replaceHistoryRecord(record)
  }

  function _fallbackAssistantReply(result, isNetworkIntent) {
    const reply = String(result?.reply || '').trim()
    if (reply) return reply
    if (isNetworkIntent) {
      return '已识别为网络意图，正在进入智能体编排流程。执行过程中如果需要补充信息，我会在这里明确说明。'
    }
    return '我已收到你的消息，但后台没有返回有效回复。你可以稍后重试，或换一种方式描述需求。'
  }

  function _friendlyRequestFailure(error) {
    if (error instanceof api.ApiTimeoutError) {
      return '请求超时：后台长时间没有返回有效进展，本次请求尚未进入执行流程。请稍后重试。'
    }

    if (error instanceof api.ApiHttpError) {
      if (error.status === 401 || error.status === 403) {
        return '请求失败：身份校验未通过，请检查 API Key 后重试。'
      }
      if ([502, 503, 504].includes(error.status)) {
        return `请求失败：后台服务暂时不可用（${error.status}），本次请求尚未进入执行流程，请稍后重试。`
      }
      if (error.status >= 500) {
        return `请求失败：后台处理异常（${error.status}），本次请求尚未进入执行流程，请稍后重试。`
      }
      return `请求失败：后台拒绝了本次请求（${error.status}），请检查输入后重试。`
    }

    if (error instanceof TypeError) {
      return '请求失败：无法连接后台服务，请检查服务是否已启动后重试。'
    }

    return `请求失败：${error?.message || '后台暂时不可用，请稍后重试。'}`
  }

  const intentTemplates = [
    {
      name: '流量监控',
      summary: '监控 s_core1 全端口双向流量',
      priority: 'medium',
      text: '持续监控交换机 s_core1 的全部端口双向流量，采样周期为 5 秒，持续 10 分钟；展示吞吐量、包速率和丢包率，仅采集数据，不修改任何网络策略。',
    },
    {
      name: '隔离主机',
      summary: '临时隔离 h_backup 并保留管理通道',
      priority: 'critical',
      text: '立即隔离主机 h_backup，阻断其与全网所有节点的双向业务通信，包括入站和出站流量；保留控制面管理通道，隔离持续 10 分钟，结束后自动恢复。',
    },
    {
      name: '限制带宽',
      summary: '限制 h_research 到 h_video 为 100 Mbps',
      priority: 'high',
      text: '将主机 h_research 发往主机 h_video 的业务流量带宽上限设置为 100 Mbps，策略立即生效并持续 10 分钟；保留控制面流量，超限流量执行整形而不是直接丢弃。',
    },
  ]

  /* ---- computed ---- */
  const activeIntent = computed(() => {
    if (!selectedIntentId.value) return null
    return intentHistory.value.find((item) => item.id === selectedIntentId.value) || null
  })

  function recordOwnsRuntime(record, runtimeId) {
    if (!record || !runtimeId) return false
    return [
      record.id,
      record.sessionId,
      record.session_id,
      record.activeRunId,
      record.intent_id,
      record.intentId,
      ...(record.runIds || []),
      ...Object.keys(record.intentResults || {}),
    ].filter(Boolean).some((id) => String(id) === String(runtimeId))
  }

  function awaitingState(record, preferredRuntimeId = '') {
    if (!record) return null

    const runtimeId = preferredRuntimeId
      || record.activeRunId
      || record.runIds?.at(-1)
      || ''
    const runtime = runtimeId ? record.intentResults?.[runtimeId] : null
    const lifecycle = runtime?.lifecycle
      || record.lifecycle
      || normalizeLifecycleStatus(runtime?.status || record.rawStatus || record.status)
    const rawStatus = String(runtime?.status || record.rawStatus || record.status || '').toLowerCase()
    const snapshot = runtime?.snapshot || record.snapshot || null

    // A stale clarification snapshot must never reopen a runtime which the
    // backend has already moved to a running or terminal state.
    if (isTerminalStatus(lifecycle) || isActivelyExecuting(lifecycle)) return null

    if (
      isAwaitingUserInput(lifecycle)
      || Boolean(snapshot?.requires_user_input || snapshot?.needs_clarification)
    ) {
      return { kind: 'clarification', runtimeId, snapshot }
    }

    if (['approval', 'pending_approval'].includes(rawStatus)) {
      return { kind: 'approval', runtimeId, snapshot }
    }

    return null
  }

  const activeClarificationQuestion = computed(() => {
    const state = awaitingState(activeIntent.value)
    if (state?.kind !== 'clarification') return ''
    const snapshot = state.snapshot
    return snapshot?.clarification_prompt || snapshot?.clarification_question || '当前意图需要补充说明。'
  })

  /**
   * Single-flight guard for the entire workspace. Users may inspect another
   * conversation while an intent runs, but no second chat/intent may start.
   */
  const globalInteractionLock = computed(() => {
    const pendingRecord = intentHistory.value.find((record) => (
      isSubmitting.value && record.pendingTurnId && !awaitingState(record)
    ))
    if (pendingRecord) {
      return { locked: true, kind: 'classifying', recordId: pendingRecord.id, runtimeId: '' }
    }

    const runtimeId = [...runningIntents.value][0] || ''
    const activeAwaiting = awaitingState(
      activeIntent.value,
      runtimeId && recordOwnsRuntime(activeIntent.value, runtimeId) ? runtimeId : '',
    )
    if (activeAwaiting && (!runtimeId || recordOwnsRuntime(activeIntent.value, runtimeId))) {
      return {
        locked: true,
        kind: activeAwaiting.kind,
        recordId: activeIntent.value.id,
        runtimeId: activeAwaiting.runtimeId,
      }
    }

    if (runtimeId) {
      const owner = intentHistory.value.find((record) => recordOwnsRuntime(record, runtimeId))
      const ownerAwaiting = awaitingState(owner, runtimeId)
      if (ownerAwaiting) {
        return {
          locked: true,
          kind: ownerAwaiting.kind,
          recordId: owner?.id || '',
          runtimeId,
        }
      }
      return {
        locked: true,
        kind: 'running',
        recordId: owner?.id || '',
        runtimeId,
      }
    }

    const awaitingRecord = intentHistory.value.find((record) => awaitingState(record))

    if (awaitingRecord) {
      const state = awaitingState(awaitingRecord)
      return {
        locked: true,
        kind: state.kind,
        recordId: awaitingRecord.id,
        runtimeId: state.runtimeId,
      }
    }

    // Covers the short chat-stream interval before an intent id is returned.
    if (isStreaming.value) {
      return {
        locked: true,
        kind: 'streaming',
        recordId: activeIntent.value?.id || '',
        runtimeId: streamingIntentId.value || '',
      }
    }

    return { locked: false, kind: '', recordId: '', runtimeId: '' }
  })

  const isGlobalIntentLocked = computed(() => globalInteractionLock.value.locked)
  const canAnswerActiveClarification = computed(() => {
    const lock = globalInteractionLock.value
    const active = activeIntent.value
    return Boolean(
      lock.locked
      && lock.kind === 'clarification'
      && active?.id
      && (
        String(active.id) === String(lock.recordId)
        || recordOwnsRuntime(active, lock.runtimeId)
      )
      && awaitingState(active, lock.runtimeId)?.kind === 'clarification',
    )
  })
  const isComposerLocked = computed(() => (
    isSubmitting.value || interactionMode.value === INTERACTION_MODE.ACTIVE
  ))
  const cancellableRuntimeId = computed(() => {
    const lock = globalInteractionLock.value
    if (!lock.runtimeId) return ''
    return ['running', 'streaming', 'classifying', 'clarification'].includes(lock.kind) ? lock.runtimeId : ''
  })
  const isCancellingActiveIntent = computed(() => (
    Boolean(cancellableRuntimeId.value)
    && cancellingIntentIds.value.has(cancellableRuntimeId.value)
  ))
  /** Whether SSE is in a reconnecting state for the selected intent. */
  const sseReconnecting = computed(() => {
    const id = activeIntent.value?.activeRunId || selectedIntentId.value
    if (!id) return false
    return streaming.isReconnecting(id)
  })
  /** Governs whether the composer is blocked, awaiting input, or idle. */
  const interactionMode = computed(() => {
    const selectedLifecycle = activeIntent.value?.lifecycle
      || normalizeLifecycleStatus(authoritativeLifecycleValue(activeIntent.value))
    return deriveInteractionMode(
      activeIntent.value?.activeRunId || selectedIntentId.value,
      runningIntents.value,
      selectedLifecycle,
      sseReconnecting.value,
    )
  })
  /** True only when the agent is actively computing (not awaiting user). */
  const isAgentBusy = computed(() => interactionMode.value === INTERACTION_MODE.ACTIVE)

  /* ---- SSE & polling (dual-channel) — delegated to useIntentStreaming ---- */
  const streaming = useIntentStreaming({
    onStageChange(intentId, data) {
      usePipelineStore()._onStageChange({ intentId, data })
      if (data.stage === 'parse' && (data.status === 'success' || data.status === 'failed')) {
        isStreaming.value = false
      }
    },
    onMilestonePush: _pushLiveMilestone,
    onA2AAppend: _appendA2AMessage,
    onThinkingToken(intentId, data) {
      if (streamingIntentId.value !== intentId) {
        streamingIntentId.value = intentId
        _tokenBuffer.value = []
      }
      isStreaming.value = true
      _tokenBuffer.value.push({ seq: data.seq ?? _tokenBuffer.value.length, token: data.token })
      if (_streamTimeout) clearTimeout(_streamTimeout)
      _streamTimeout = setTimeout(() => {
        isStreaming.value = false
        _tokenBuffer.value = []
      }, 30000)
    },
    onStreamEnd() {
      isStreaming.value = false
      if (_streamTimeout) { clearTimeout(_streamTimeout); _streamTimeout = null }
    },
    onLog: _log,
    onApplyDetail: _applyIntentDetail,
    onTransportState: _setIntentTransportState,
    isTerminalStatus,
  })

  /* ---- actions ---- */
  async function loadIntents() {
    try {
      const data = await api.listIntents()
      const existing = new Map(intentHistory.value.map((item) => [item.id, item]))
      const serverRecords = (data.items || []).map(toIntentRecord).map((record) => {
        const current = existing.get(record.id)
        const merged = current
        ? {
            ...record,
            dialogMessages: current.dialogMessages || record.dialogMessages,
            runIds: [...new Set([...(current.runIds || []), ...(record.runIds || [])].filter(Boolean))],
            activeRunId: current.activeRunId || record.activeRunId || record.id,
            milestones: mergeMilestones(current.milestones || [], record.milestones || []),
            intentResults: {
              ...(record.intentResults || {}),
              ...(current.intentResults || {}),
            },
            finalPresentation: {
              ...(record.finalPresentation || {}),
              ...(current.finalPresentation || {}),
            },
          }
          : record
        return isTerminalStatus(merged.lifecycle || merged.rawStatus)
          ? clearTerminalExecutionState(merged, current?.activeRunId || merged.activeRunId || merged.id)
          : merged
      })
      const serverIds = new Set(serverRecords.map((item) => item.id))
      const localConversationRecords = intentHistory.value.filter((item) => (
        item.isConversation
        || item.result?.chat_only
        || String(item.id || '').startsWith('CHAT-')
      ) && !serverIds.has(item.id))
      intentHistory.value = [...localConversationRecords, ...serverRecords]
      // Update runningIntents set
      const running = new Set()
      for (const item of intentHistory.value) {
        if (
          !item.isConversation
          && !item.result?.chat_only
          && isActivelyExecuting(item.lifecycle || item.rawStatus || item.status)
        ) {
          running.add(item.activeRunId || item.id)
        }
      }
      runningIntents.value = running
    } catch (e) {
      _log(`加载意图列表失败: ${e.message}`)
    }
  }

  async function loadConversations() {
    try {
      const data = await api.listConversations({ pageSize: 80 })
      const currentBySession = new Map(conversationHistory.value.map((item) => [item.sessionId || item.id, item]))
      conversationHistory.value = (data.items || [])
        .map(toConversationRecord)
        .map((item) => {
          const current = currentBySession.get(item.sessionId || item.id)
          return current?.title ? { ...item, title: current.title } : item
        })
        .filter((item) => item.sessionId)
    } catch (e) {
      _log(`加载历史对话失败: ${e.message}`)
    }
  }

  async function selectConversation(conversation, { force = false } = {}) {
    const sessionId = conversation?.sessionId || conversation?.session_id || conversation?.id
    if (!sessionId) return null

    const requestSeq = ++_conversationSelectionSeq
    let previousRecord = intentHistory.value.find((item) => (
      item.sessionId === sessionId || item.id === sessionId
    ))

    // Selection must never wait for the network. Reuse the hydrated record or
    // install a lightweight shell so the view changes on the same click.
    if (!previousRecord) {
      previousRecord = {
        ...toConversationRecord({ ...conversation, session_id: sessionId }),
        _historyLoaded: false,
        _historyLoading: true,
      }
      intentHistory.value = [
        previousRecord,
        ...intentHistory.value.filter((item) => item.sessionId !== sessionId && item.id !== sessionId),
      ]
    }

    selectedIntentId.value = previousRecord.id
    intentText.value = ''
    _lastSessionId.value = sessionId

    if (previousRecord.milestones?.length) {
      _pipelineMilestones(previousRecord.milestones)
    } else {
      _pipelineFromStatus(previousRecord.status)
    }

    // A hydrated conversation is immutable presentation data. SSE updates the
    // same in-memory record, so reopening it does not need another N+1 fetch.
    if (previousRecord._historyLoaded && !force) return previousRecord

    try {
      const timeline = await api.getConversationTimeline(sessionId, { pageSize: 200 })
      if (requestSeq !== _conversationSelectionSeq) return null

      const entries = timeline.items || []
      const dialogMessages = normalizeTimelineMessages(entries, sessionId)
      const intentIds = extractTimelineIntentIds(entries)
      const activeRunId = intentIds[intentIds.length - 1] || ''

      // Render the conversation as soon as the timeline arrives. Intent
      // details continue loading without keeping the history item "stuck".
      const timelineRecord = {
        ...previousRecord,
        dialogMessages,
        activeRunId: activeRunId || previousRecord.activeRunId || '',
        runIds: intentIds.length ? intentIds : (previousRecord.runIds || []),
        title: inferTitle(
          dialogMessages.find((message) => message.role === 'user')?.content
          || conversation.title
          || previousRecord.title
        ),
        _historyLoading: true,
      }
      intentHistory.value = [
        timelineRecord,
        ...intentHistory.value.filter((item) => item.sessionId !== sessionId && item.id !== sessionId),
      ]
      previousRecord = timelineRecord

      const intentDetailById = {}
      if (intentIds.length) {
        const detailResults = await Promise.allSettled(intentIds.map((intentId) => api.getIntent(intentId)))
        if (requestSeq !== _conversationSelectionSeq) return null

        detailResults.forEach((result, index) => {
          if (result.status !== 'fulfilled' || !result.value) return
          const detail = result.value
          const intentId = detail.intent_id || intentIds[index]
          if (intentId) intentDetailById[intentId] = detail
        })
      }

      let record = toConversationRecord({ ...conversation, session_id: sessionId })
      if (activeRunId) {
        const detail = intentDetailById[activeRunId]
        if (detail) {
          record = {
            ...toIntentRecord(detail),
            id: sessionId,
            sessionId,
            activeRunId,
            runIds: intentIds,
            isConversation: true,
          }
        } else {
          record.activeRunId = activeRunId
          record.runIds = intentIds
        }
      }

      record.dialogMessages = dialogMessages
      record.intentResults = Object.fromEntries(
        Object.entries(intentDetailById).map(([intentId, detail]) => [intentId, toIntentRuntimeRecord(detail)]),
      )
      record.finalPresentation = {
        ...(previousRecord.finalPresentation || {}),
        ...(conversation?.finalPresentation || {}),
        ...(record.finalPresentation || {}),
      }
      record.milestones = mergeMilestones(previousRecord.milestones || [], record.milestones || [])

      for (const intentId of intentIds) {
        const runtime = record.intentResults?.[intentId]
        if (!runtime) continue

        const previousRuntime = previousRecord.intentResults?.[intentId] || {}
        record.intentResults = {
          ...(record.intentResults || {}),
          [intentId]: {
            ...previousRuntime,
            ...runtime,
            milestones: mergeMilestones(
              previousRuntime.milestones || [],
              runtime.milestones || (intentId === record.activeRunId ? record.milestones || [] : []),
            ),
          },
        }
        _freezeFinalPresentation(record, intentId, record.intentResults[intentId])
      }

      record.title = timelineRecord.title
      record.text = record.text || record.title
      record.content = record.content || record.title
      record.isConversation = true
      record._historyLoaded = true
      record._historyLoading = false

      if (requestSeq !== _conversationSelectionSeq) return null

      intentHistory.value = [
        record,
        ...intentHistory.value.filter((item) => item.sessionId !== sessionId && item.id !== sessionId),
      ]
      selectedIntentId.value = record.id
      _syncConversationSummary(record, { title: record.title })

      if (record.milestones?.length) _pipelineMilestones(record.milestones)
      else _pipelineFromStatus(record.status)

      return record
    } catch (e) {
      if (requestSeq !== _conversationSelectionSeq) return null

      const current = intentHistory.value.find((item) => item.sessionId === sessionId || item.id === sessionId)
      if (current) {
        current._historyLoading = false
        _replaceHistoryRecord(current)
      }
      _log(`加载对话详情失败: ${e.message}`)
      return current || null
    }
  }

  function _sameRuntimeMessage(message, intentId) {
    return (message?.intentId || message?.intent_id || '') === intentId
  }

  function _isClarificationQuestionMessage(message = {}) {
    const type = String(message.messageType || message.message_type || '').toLowerCase()
    const tag = String(message.tag || '').toLowerCase()
    return Boolean(
      type === 'clarification_question'
      || tag === 'clarification'
      || tag.includes('需要补充')
    )
  }

  function _appendClarificationQuestion(record, intentId, question) {
    if (!record || !intentId) return

    const content = `需要补充信息：${question || '当前意图还需要补充信息，请补充关键对象、范围或约束后继续。'}`
    const normalizedContent = String(content).replace(/\s+/g, ' ').trim()
    const messages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
    let lastQuestionIndex = -1
    let lastUserIndex = -1

    messages.forEach((message, index) => {
      if (!_sameRuntimeMessage(message, intentId)) return
      if (message.role === 'user') lastUserIndex = index
      if (message.role === 'assistant' && _isClarificationQuestionMessage(message)) {
        lastQuestionIndex = index
      }
    })

    if (lastQuestionIndex > lastUserIndex) {
      const current = messages[lastQuestionIndex]
      _updateDialogMessage(record, {
        ...current,
        tag: 'clarification',
        messageType: 'clarification_question',
        content,
        intentId,
      })
      return
    }

    if (lastQuestionIndex >= 0 && lastUserIndex > lastQuestionIndex) {
      const lastQuestion = messages[lastQuestionIndex]
      const lastQuestionContent = String(lastQuestion?.content || '').replace(/\s+/g, ' ').trim()
      if (lastQuestionContent === normalizedContent) {
        return
      }
    }

    _appendDialogMessage(record, {
      id: `${record.id || intentId}-clarification-question-${intentId}-${Date.now()}`,
      role: 'assistant',
      tag: 'clarification',
      messageType: 'clarification_question',
      content,
      intentId,
    })
  }

  function _freezeFinalPresentation(record, intentId, runtime = {}) {
    if (!record || !intentId) return null

    const terminalStatus = String(runtime.status || '').toLowerCase()
    const terminalClass = String(runtime.statusClass || '').toLowerCase()
    if (!isTerminalStatus(terminalStatus) && !['success', 'failed', 'cancelled'].includes(terminalClass)) {
      return null
    }

    const existing = record.finalPresentation?.[intentId] || {}
    const finalReply = existing.finalReply || finalReplyForRuntime(runtime)
    const stableMessageId = existing.finalMessageId
      || `${record.sessionId || record.id || 'conversation'}-${intentId}-terminal-response`
    const messages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
    const matchingMessage = messages.find((message) => (
      message.role === 'assistant'
      && (message.intentId || message.intent_id) === intentId
      && cleanResultText(message.content) === cleanResultText(finalReply)
    ))
    const finalMessageId = _placeFinalDialogMessage(record, intentId, {
      id: existing.finalMessageId || matchingMessage?.id || stableMessageId,
      role: 'assistant',
      tag: 'NetPulse',
      messageType: 'assistant_text',
      content: finalReply,
      intentId,
      finalOutcome: true,
      action: runtime.result ? 'topology' : (matchingMessage?.action || ''),
      report: runtime.result || matchingMessage?.report || null,
      source: matchingMessage?.source || 'final-presentation',
    })

    // A terminal presentation is an immutable snapshot. Polling and SSE can
    // deliver the same terminal intent more than once; accepting later payloads
    // would make an already rendered conclusion jump between values.
    if (existing.finalMessageId && existing.finalReply && existing.executionSummary) {
      if (existing.finalMessageId !== finalMessageId) {
        record.finalPresentation = {
          ...(record.finalPresentation || {}),
          [intentId]: { ...existing, finalMessageId },
        }
      }
      _clearTransientAgentState(record, { intentId })
      return record.finalPresentation[intentId]
    }

    const summaryInput = {
      ...runtime,
      id: intentId,
      statusClass: terminalClass.includes('fail') || terminalStatus === 'failed'
        ? 'failed'
        : 'success',
    }
    const executionSummary = buildExecutionSummary(summaryInput)
      || existing.executionSummary
      || fallbackExecutionSummary(intentId, runtime, finalReply)
    const stages = mergeMilestones(existing.stages || [], runtime.milestones || [])

    record.finalPresentation = {
      ...(record.finalPresentation || {}),
      [intentId]: {
        ...existing,
        intentId,
        sessionId: record.sessionId || record.id,
        status: runtime.status,
        statusClass: runtime.statusClass,
        result: runtime.result || existing.result || null,
        milestones: mergeMilestones(existing.milestones || [], runtime.milestones || []),
        snapshot: runtime.snapshot || existing.snapshot || null,
        text: runtime.text || existing.text || record.text || record.content || '',
        finalReply,
        finalMessageId,
        executionSummary,
        stages,
        savedAt: existing.savedAt || new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      },
    }

    _clearTransientAgentState(record, { intentId })

    _replaceHistoryRecord(record)
    return record.finalPresentation[intentId]
  }

  function _applyIntentDetail(data) {
    const record = _findRecordByRuntimeId(data.intent_id, data.session_id)
    if (!record) return null

    const runtimeId = data.intent_id
    const previousRuntime = record.intentResults?.[data.intent_id] || {}
    const nextRuntime = toIntentRuntimeRecord(data)

    record.intentResults = {
      ...(record.intentResults || {}),
      [data.intent_id]: {
        ...previousRuntime,
        ...nextRuntime,
        milestones: mergeMilestones(
          previousRuntime.milestones || [],
          nextRuntime.milestones || []
        ),
      },
    }

    record.sessionId = data.session_id || record.sessionId || ''
    record.runIds = [...new Set([...(record.runIds || []), runtimeId].filter(Boolean))]

    // Late events from an older intent must still update that intent's own
    // runtime cache, but must never take ownership of the current conversation.
    const isCurrentRuntime = record.activeRunId === runtimeId
      || (!record.activeRunId && !record.pendingTurnId)

    if (isCurrentRuntime) {
       const lifecycleValue = authoritativeLifecycleValue(data)
       const projected = projectLifecycle(lifecycleValue)
      record.status = projected.status
      record.statusClass = projected.statusClass
      record.lifecycle = projected.canonical
       record.rawStatus = lifecycleValue
      record.milestones = mergeMilestones(
        record.activeRunId === runtimeId ? record.milestones || [] : [],
        data.milestones || data.snapshot?.milestones || []
      )
      record.snapshot = data.snapshot || null
      record.result = data.result || null
      record.activeRunId = runtimeId
      record.content = data.execution_text || data.text || record.content
      record.text = data.text || record.text
    }

    if (isTerminalStatus(authoritativeLifecycleValue(data))) {
      _freezeFinalPresentation(record, data.intent_id, record.intentResults[data.intent_id])
      clearTerminalExecutionState(record, data.intent_id)
      isSubmitting.value = false
    }

    if (isAwaitingUserInput(authoritativeLifecycleValue(data)) || record.snapshot?.requires_user_input || record.snapshot?.needs_clarification) {
      const question = record.snapshot?.clarification_prompt || record.snapshot?.clarification_question || '当前意图还需要补充信息，请补充关键对象、范围或约束后继续。'
      _appendClarificationQuestion(record, data.intent_id, question)
    }

    // Update runningIntents set
    const canonicalStatus = normalizeLifecycleStatus(authoritativeLifecycleValue(data))
    if (!isActivelyExecuting(canonicalStatus)) {
      const next = new Set(runningIntents.value)
      next.delete(data.intent_id)
      runningIntents.value = next
      if (isTerminalStatus(canonicalStatus)) {
        const cancelling = new Set(cancellingIntentIds.value)
        cancelling.delete(data.intent_id)
        cancellingIntentIds.value = cancelling
      }
    } else if (data.status) {
      runningIntents.value = new Set([...runningIntents.value, data.intent_id])
    }

    if (isCurrentRuntime) {
      _pipelineMilestones(record.milestones || [])
    }
    return record
  }

  /** Push a synthetic milestone from SSE events so the conversation view
   *  shows live pipeline progress instead of the static placeholder.
   *  Milestones are stored in record.milestones; buildGeneratedMessages()
   *  converts them to display messages — no duplicate path needed. */
  function _pushLiveMilestone(intentId, entry) {
  const record = _findRecordByRuntimeId(intentId)
  if (!record) return

  const previousRuntime = record.intentResults?.[intentId] || {
    id: intentId,
    status: record.status,
    statusClass: record.statusClass,
    result: null,
    milestones: [],
    snapshot: null,
    text: record.text || record.content || '',
  }

  record.intentResults = {
    ...(record.intentResults || {}),
    [intentId]: {
      ...previousRuntime,
      milestones: mergeMilestones(previousRuntime.milestones || [], [entry]),
      updatedAt: new Date().toISOString(),
    },
  }

  if (record.activeRunId === intentId) {
    record.milestones = mergeMilestones(record.milestones || [], [entry])
  }

  _replaceHistoryRecord(record)
}

  function _appendA2AMessage(intentId, message) {
    const record = _findRecordByRuntimeId(intentId)
    if (!record || !message?.id) return
    const previousRuntime = record.intentResults?.[intentId] || {
      id: intentId,
      status: 'running',
      statusClass: 'running',
      result: null,
      milestones: [],
      snapshot: null,
      text: record.text || record.content || '',
    }
    const currentResult = previousRuntime.result || {}
    const currentTimeline = currentResult.a2a_timeline || []
    if (currentTimeline.some((item) => item.id === message.id)) return
    const currentEvidence = currentResult.a2a_runtime_evidence || {}
    const currentMessages = currentEvidence.messages || currentTimeline

    const nextResult = {
      ...currentResult,
      a2a_timeline: [...currentTimeline, message].slice(-60),
      a2a_runtime_evidence: {
        ...currentEvidence,
        protocol_version: message.protocol_version || currentEvidence.protocol_version,
        runtime_trace_ready: true,
        message_count: (currentEvidence.message_count || currentMessages.length) + 1,
        messages: [...currentMessages, message].slice(-60),
      },
    }

    record.intentResults = {
      ...(record.intentResults || {}),
      [intentId]: {
        ...previousRuntime,
        result: nextResult,
        updatedAt: new Date().toISOString(),
      },
    }

    if (record.activeRunId === intentId) {
      record.result = nextResult
    }
    _replaceHistoryRecord(record)
  }

async function submitIntent({ appendToActive = true } = {}) {
    if (isGlobalIntentLocked.value) {
      _log('当前意图尚未结束，请等待执行完成后再发起新的对话。')
      return null
    }

    const text = intentText.value.trim()
    if (!text) return null

    isSubmitting.value = true

    _resetTokenStream()

    const tempId = `CHAT-${Date.now()}`
    const turnId = `TURN-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    const targetRecord = appendToActive && selectedIntentId.value ? activeIntent.value : null

    const userMessage = {
      id: `${turnId}-user`,
      role: 'user',
      tag: 'operator',
      messageType: 'user_text',
      content: text,
      turnId,
    }

    const optimistic = targetRecord || {
      id: tempId,
      sessionId: _lastSessionId.value || '',
      runIds: [],
      title: text.slice(0, 24) || '新对话',
      owner: 'operator',
      status: 'chatting',
      statusClass: 'running',
      content: text,
      text,
      priority: priority.value,
      milestones: [],
      snapshot: null,
      result: { chat_only: true, intent_detected: false },
      dialogMessages: [],
    }
    optimistic._historyLoaded = false
    optimistic.pendingTurnId = turnId
    optimistic.pendingAssistantMessageId = `${turnId}-assistant`
    optimistic.activeTurnId = turnId
    optimistic.activeTurnIntentId = ''

    _appendDialogMessage(optimistic, userMessage)

    optimistic.status = 'chatting'
    optimistic.statusClass = 'running'
    optimistic.content = text
    optimistic.text = text
    optimistic.priority = priority.value
    optimistic.result = { chat_only: true, intent_detected: false }
    // These fields describe only the currently executing turn. Completed
    // intent data already lives in intentResults/finalPresentation.
    optimistic.milestones = []
    optimistic.snapshot = null

    // 新对话才插入历史；在历史对话里继续问，就追加到当前 record。
    if (!targetRecord) {
      intentHistory.value = [optimistic, ...intentHistory.value].slice(0, 30)
      selectedIntentId.value = tempId
    }

    _pipelineReset()
    intentText.value = ''

    const assistantMessage = {
      id: `${turnId}-assistant`,
      role: 'assistant',
      tag: 'NetPulse',
      messageType: 'assistant_text',
      content: '',
      pending: true,
      turnId,
    }

    _appendDialogMessage(optimistic, assistantMessage)

    try {
      isStreaming.value = true
      streamingIntentId.value = turnId

      const result = await api.chatStream({
        text,
        userId: 'operator',
        sessionId: optimistic.sessionId || _lastSessionId.value || undefined,
        onToken(token) {
          console.log('[CHAT TOKEN]', token)
          const currentMessages = Array.isArray(optimistic.dialogMessages)
            ? optimistic.dialogMessages
            : []

          const messageIndex = currentMessages.findIndex((message) => message.id === assistantMessage.id)
          const currentAssistant = messageIndex >= 0
            ? currentMessages[messageIndex]
            : assistantMessage

          const nextAssistant = {
            ...currentAssistant,
            id: assistantMessage.id,
            role: 'assistant',
            tag: currentAssistant.tag || assistantMessage.tag || 'NetPulse',
            content: `${currentAssistant.content || ''}${token}`,
            pending: false,
          }

          assistantMessage.content = nextAssistant.content
          _updateDialogMessage(optimistic, nextAssistant)
        },
      })

      isStreaming.value = false
      streamingIntentId.value = null
      _tokenBuffer.value = []

      if (result?.reply && !assistantMessage.content) {
        const currentMessages = Array.isArray(optimistic.dialogMessages)
          ? optimistic.dialogMessages
          : []
        const messageIndex = currentMessages.findIndex((message) => message.id === assistantMessage.id)
        const nextAssistant = {
          ...(messageIndex >= 0 ? currentMessages[messageIndex] : assistantMessage),
          id: assistantMessage.id,
          role: 'assistant',
          tag: assistantMessage.tag || 'NetPulse',
          content: result.reply,
          pending: false,
        }
        assistantMessage.content = result.reply
        _updateDialogMessage(optimistic, nextAssistant)
      }

      if (result?.session_id) {
        const previousId = optimistic.id
        optimistic.sessionId = result.session_id
        optimistic.id = result.session_id
        optimistic.isConversation = true
        _lastSessionId.value = result.session_id

        if (previousId !== optimistic.id) {
          const historyIndex = intentHistory.value.findIndex((item) => item.id === previousId)
          if (historyIndex >= 0) {
            intentHistory.value[historyIndex] = optimistic
          }
          if (selectedIntentId.value === previousId) {
            selectedIntentId.value = optimistic.id
          }
        }
      }

      const detectedIntentId = result?.intent_id || result?.intentId || null
      const isNetworkIntent = Boolean(result?.intent_detected || detectedIntentId)
      const assistantReply = _fallbackAssistantReply(result, isNetworkIntent)
      if (!assistantMessage.content && assistantReply) {
        assistantMessage.content = assistantReply
        assistantMessage.pending = false
        _updateDialogMessage(optimistic, {
          ...assistantMessage,
          content: assistantReply,
          pending: false,
        })
      } else if (assistantMessage.content) {
        assistantMessage.pending = false
        _updateDialogMessage(optimistic, { ...assistantMessage, pending: false })
      }

      /**
       * 普通聊天：后台 /chat 没有返回 intent_id。
       * 只保留聊天内容，不启动 SSE，不轮询 /intents，不进入阶段流程。
       */
      if (!isNetworkIntent || !detectedIntentId) {
        optimistic.status = 'success'
        optimistic.statusClass = 'success'
        optimistic.result = {
          chat_only: true,
          intent_detected: false,
          confidence: result?.confidence || 0,
        }
        optimistic.milestones = []
        optimistic.snapshot = null
        optimistic.isConversation = true
        optimistic.activeRunId = ''
        _clearTransientAgentState(optimistic, { turnId })
        runningIntents.value.delete(optimistic.id)
        _syncConversationSummary(optimistic)
        _pipelineReset()
        _log('已完成普通对话。')
        return optimistic
      }

      /**
       * 网络意图：后台 /chat 已创建真实 intent_id。
       * 前端不要再 POST /intents，只需要把当前对话绑定到真实 INT-*，然后监听阶段。
       */
      const realId = detectedIntentId
      const oldId = optimistic.id
      const idx = intentHistory.value.findIndex((item) => item.id === oldId)
      const stableConversationId = optimistic.sessionId || result.session_id || realId
      userMessage.intentId = realId
      assistantMessage.intentId = realId
      _bindExchangeToIntent(optimistic, [userMessage.id, assistantMessage.id], realId)

      const realRecord = {
        ...optimistic,
        id: stableConversationId,
        sessionId: stableConversationId,
        activeRunId: realId,
        pendingTurnId: '',
        pendingAssistantMessageId: '',
        activeTurnId: turnId,
        activeTurnIntentId: realId,
        runIds: [...new Set([...(optimistic.runIds || []), realId].filter(Boolean))],
        status: 'planning',
        statusClass: 'running',
        isConversation: true,
        result: {
          chat_only: false,
          intent_detected: true,
          intent_type: result.intent_type,
          confidence: result.confidence,
        },
        intentResults: {
          ...(optimistic.intentResults || {}),
          [realId]: {
            id: realId,
            status: 'running',
            statusClass: 'running',
            result: null,
            milestones: [],
            snapshot: null,
            text,
            updatedAt: new Date().toISOString(),
          },
        },
      }

      if (idx >= 0) {
        intentHistory.value[idx] = realRecord
      } else {
        intentHistory.value.unshift(realRecord)
      }

      selectedIntentId.value = stableConversationId
      runningIntents.value.delete(oldId)
      runningIntents.value.add(realId)

      _syncConversationSummary(realRecord, { session_id: stableConversationId })

      _log(`${realId.slice(-8)} 已识别为网络意图，进入自治编排。`)
      streaming.start(realId)

      return realRecord
    } catch (e) {
      _clearTransientAgentState(optimistic, { turnId })

      assistantMessage.content = _friendlyRequestFailure(e)
      assistantMessage.pending = false
      assistantMessage.messageType = 'error'
      _updateDialogMessage(optimistic, assistantMessage)
      optimistic.status = 'failed'
      optimistic.statusClass = 'failed'
      runningIntents.value.delete(optimistic.id)

      _log(`对话失败: ${e.message}`)
      return optimistic
    } finally {
      isSubmitting.value = false
    }
  }

  async function refreshIntent(intentId) {
    if (!intentId) return null
    const current = _findRecordByRuntimeId(intentId)
    const runtimeId = current?.activeRunId || intentId
    try {
      const data = await api.getIntent(runtimeId)
      return _applyIntentDetail(data)
    } catch (e) {
      _log(`刷新意图状态失败: ${e.message}`)
      return null
    }
  }

  async function answerClarification(intentId, text) {
    const answer = (text || '').trim()
    if (!intentId || !answer || isAnsweringClarification.value) return null

    const record = _findRecordByRuntimeId(intentId)
    const lock = globalInteractionLock.value
    const runtimeId = lock.kind === 'clarification' && lock.runtimeId
      ? lock.runtimeId
      : (record?.activeRunId || intentId)

    // Clarification belongs to one exact conversation/runtime. Never let a
    // delayed click or a history switch submit text to another intent.
    if (
      !record
      || !canAnswerActiveClarification.value
      || lock.kind !== 'clarification'
      || !recordOwnsRuntime(record, runtimeId)
      || awaitingState(record, runtimeId)?.kind !== 'clarification'
    ) {
      _log('补充信息未提交：当前对话与待补充意图不匹配，请重新打开对应历史对话。')
      return null
    }

    const submissionId = `${record.id || runtimeId}-clarification-${Date.now()}`
    record._historyLoaded = false
    _appendDialogMessage(record, {
      id: `${submissionId}-answer`,
      role: 'user',
      tag: 'operator',
      messageType: 'clarification_answer',
      content: answer,
      intentId: runtimeId,
    })

    isAnsweringClarification.value = true
    isSubmitting.value = true
    try {
      const result = await api.answerClarification(runtimeId, { text: answer })
      record.status = 'planning'
      record.statusClass = 'running'
      record.lifecycle = 'PLANNING'
      record.rawStatus = 'pending'
      record.snapshot = null
      if (record.intentResults?.[runtimeId]) {
        record.intentResults = {
          ...(record.intentResults || {}),
          [runtimeId]: {
            ...record.intentResults[runtimeId],
            status: 'planning',
            statusClass: 'running',
            lifecycle: 'PLANNING',
            snapshot: null,
            updatedAt: new Date().toISOString(),
          },
        }
      }
      _appendDialogMessage(record, {
        id: `${submissionId}-resume`,
        role: 'assistant',
        tag: 'NetPulse',
        content: '已收到补充信息，正在继续执行该网络意图。',
        intentId: runtimeId,
      })
      clarificationAnswer.value = ''
      _pipelineReset()
      selectedIntentId.value = record.id
      runningIntents.value.add(runtimeId)
      _log(`${runtimeId.slice(-8)} 已提交澄清回答，继续执行。`)
      streaming.start(runtimeId)
      return result
    } catch (e) {
      // A 409 often means a previous click was accepted and the backend has
      // already left pending_clarification. Reconcile once before declaring
      // failure so we neither duplicate the answer nor leave a false spinner.
      let reconciled = null
      if (e instanceof api.ApiHttpError && e.status === 409) {
        try {
          const latest = await api.getIntent(runtimeId)
          reconciled = _applyIntentDetail(latest)
          const latestStatus = String(latest?.status || '').toLowerCase()

          if (['pending', 'planning', 'running', 'executing', 'sensing', 'thinking', 'checking', 'acting', 'verifying'].includes(latestStatus)) {
            _appendDialogMessage(record, {
              id: `${submissionId}-reconciled`,
              role: 'assistant',
              tag: 'NetPulse',
              messageType: 'status',
              content: '补充信息已被后台接收，当前意图正在继续执行。',
              intentId: runtimeId,
            })
            runningIntents.value.add(runtimeId)
            streaming.start(runtimeId)
            _log(`${runtimeId.slice(-8)} 补充状态已重新同步，继续执行。`)
            return { reconciled: true, ...latest }
          }

          if (isTerminalStatus(latestStatus)) {
            runningIntents.value.delete(runtimeId)
            streaming.stop(runtimeId)
            _resetTokenStream(runtimeId)
            return { reconciled: true, ...latest }
          }
        } catch (refreshError) {
          _log(`补充失败后同步状态失败: ${refreshError.message}`)
        }
      }

      streaming.stop(runtimeId)
      runningIntents.value.delete(runtimeId)
      _resetTokenStream(runtimeId)
      _setIntentTransportState(runtimeId, {
        state: 'error',
        error: e?.detail || e?.message || 'Clarification request failed',
      })

      // Keep a genuine pending-clarification record retryable. If the latest
      // backend state says otherwise, _applyIntentDetail above is authoritative.
      if (!reconciled || awaitingState(reconciled, runtimeId)?.kind === 'clarification') {
        record.status = 'clarification'
        record.statusClass = 'clarification'
      }

      const reason = e instanceof api.ApiHttpError && e.status === 409
        ? '后台状态已发生变化，补充内容未被重复提交。请根据当前提示重新确认后再试。'
        : _friendlyRequestFailure(e)
      _appendDialogMessage(record, {
        id: `${submissionId}-error`,
        role: 'assistant',
        tag: 'NetPulse',
        messageType: 'error',
        content: `补充信息提交失败：${reason}`,
        intentId: runtimeId,
      })
      _log(`提交澄清失败: ${e.message}`)
      return null
    } finally {
      isAnsweringClarification.value = false
      isSubmitting.value = false
    }
  }

  function applyTemplate(template) {
    if (isGlobalIntentLocked.value) return false
    intentText.value = template.text
    priority.value = template.priority
    _log(`已载入「${template.name}」模板。`)
    return true
  }

  function selectIntent(intent) {
    if (!intent) return
    selectedIntentId.value = intent.id
    intentText.value = ''

    if (intent.milestones && intent.milestones.length) {
      _pipelineMilestones(intent.milestones)
    } else {
      _pipelineFromStatus(intent.status)
    }
    _log(`已查看 ${intent.id.slice(-8)}：${intent.title}。`)
  }

  function setDialogMessages(intentId, messages = []) {
    const record = intentHistory.value.find((i) => i.id === intentId)
    if (!record || record.dialogMessages?.length) return
    record.dialogMessages = messages.map((message, index) => ({
      id: message.id || `${intentId}-dialog-${index}`,
      role: message.role || 'assistant',
      tag: message.tag || '',
      content: message.content || '',
    })).filter((message) => message.content)
  }

  async function cancelIntent(intentId) {
    const id = intentId || selectedIntentId.value
    if (!id) return null
    const record = _findRecordByRuntimeId(id)
    const runtimeId = record?.activeRunId || id
    if (!runtimeId || cancellingIntentIds.value.has(runtimeId)) return null
    cancellationError.value = ''
    cancellingIntentIds.value = new Set([...cancellingIntentIds.value, runtimeId])
    try {
      const response = await api.cancelIntent(runtimeId)
      // The API response only acknowledges a durable request. Keep polling
      // until an authoritative terminal record is observed; do not locally
      // fabricate CANCELLED or unlock the composer here.
      if (String(response?.status || '').toUpperCase() === 'CANCELLING') {
        _log(`${runtimeId.slice(-8)} 正在安全终止执行。`)
      }
      await refreshIntent(runtimeId)
      await loadIntents()
      return response
    } catch (e) {
      cancellationError.value = e?.detail || e?.message || '取消请求失败'
      _log(`取消意图失败: ${cancellationError.value}`)
      return null
    } finally {
      // A terminal readback clears this set in _applyIntentDetail/loadIntents;
      // retain it only while the server is still resolving CANCELLING.
      const latest = _findRecordByRuntimeId(runtimeId)
      if (latest && isTerminalStatus(latest.status)) {
        const cancelling = new Set(cancellingIntentIds.value)
        cancelling.delete(runtimeId)
        cancellingIntentIds.value = cancelling
      }
    }
  }

  async function deleteIntent(intentId) {
    try {
      await api.deleteIntent(intentId)
    } catch (e) {
      _log(`删除意图失败: ${e.message}`)
      return  // API failure — keep local state intact
    }
    // Only clean up local state after confirmed backend deletion
    streaming.stop(intentId)
    runningIntents.value.delete(intentId)
    const idx = intentHistory.value.findIndex(i => i.id === intentId)
    if (idx >= 0) intentHistory.value.splice(idx, 1)
    if (selectedIntentId.value === intentId) selectedIntentId.value = null
  }

  async function deleteConversation(conversation) {
    const sessionId = typeof conversation === 'string'
      ? conversation
      : (conversation?.sessionId || conversation?.session_id || conversation?.id)
    if (!sessionId) return false

    try {
      await api.deleteConversation(sessionId)
    } catch (e) {
      _log(`删除历史对话失败: ${e.message}`)
      return false
    }

    conversationHistory.value = conversationHistory.value.filter((item) => item.sessionId !== sessionId && item.id !== sessionId)
    intentHistory.value = intentHistory.value.filter((item) => item.sessionId !== sessionId && item.id !== sessionId)
    if (activeIntent.value?.sessionId === sessionId || selectedIntentId.value === sessionId) {
      selectedIntentId.value = null
      _lastSessionId.value = null
      _pipelineReset()
    }
    return true
  }

  function startNewIntent() {
    intentText.value = ''
    selectedIntentId.value = null
    _resetTokenStream()
    _lastSessionId.value = null
    _pipelineReset()
  }

  /* ---- intent polling: 定期刷新侧边栏意图列表 ---- */
  let _intentPollTimer = null
  const INTENT_POLL_MS = 5000

  function startIntentPolling() {
    reconcileActiveExecution()
    loadConversations()
    stopIntentPolling()
    _intentPollTimer = setInterval(() => {
      loadIntents()
      loadConversations()
    }, INTENT_POLL_MS)
  }

  async function reconcileActiveExecution() {
    await loadIntents()
    const activeId = activeIntent.value?.activeRunId
    if (activeId) await refreshIntent(activeId)
  }

  function stopIntentPolling() {
    if (!_intentPollTimer) return
    clearInterval(_intentPollTimer)
    _intentPollTimer = null
  }

  /* ---- cleanup ---- */
  function cleanup() {
    streaming.cleanup()
    stopIntentPolling()
  }

  function saveFinalPresentation(intentId, payload = {}) {
    if (!intentId) return

    const record = _findRecordByRuntimeId(intentId)
    if (!record) return

    const sessionId = record.sessionId || record.id

    record.finalPresentation = {
      ...(record.finalPresentation || {}),
      [intentId]: {
        ...(record.finalPresentation?.[intentId] || {}),
        ...payload,
        intentId,
        sessionId,
        savedAt: new Date().toISOString(),
      },
    }

    _replaceHistoryRecord(record)
    _syncConversationSummary(record, { title: record.title })
  }

  return {
    intentHistory, conversationHistory, selectedIntentId, intentText, clarificationAnswer,
    isAnsweringClarification, priority, isComposing, isSubmitting,
    streamingText, isStreaming, streamingIntentId,
    runningIntents, cancellingIntentIds, cancellationError,
    intentTemplates,
    activeIntent, activeClarificationQuestion,
    interactionMode, isAgentBusy, sseReconnecting,
    globalInteractionLock, isGlobalIntentLocked, canAnswerActiveClarification, isComposerLocked,
    cancellableRuntimeId, isCancellingActiveIntent,
    INTERACTION_MODE, isAwaitingUserInput, isActivelyExecuting,
    _lastSessionId,
    loadIntents, loadConversations, selectConversation, submitIntent, refreshIntent, answerClarification,
    cancelIntent,
    applyTemplate, selectIntent, setDialogMessages, deleteIntent, deleteConversation, startNewIntent,
    startIntentPolling, stopIntentPolling, reconcileActiveExecution,
    _startDualChannel: streaming.start, _subscribePipeline: streaming.start, cleanup,
    saveFinalPresentation
  }
})
