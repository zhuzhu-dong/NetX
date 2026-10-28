/**
 * The single frontend projection of an authoritative backend lifecycle.
 *
 * No view may promote a status from a planned action, agent card, or local
 * timer. Only persisted API/SSE values enter this adapter.
 */
const DEFINITIONS = Object.freeze({
  UNKNOWN: { label: '状态待同步', tone: 'clarification', group: 'attention', terminal: false },
  PREVIEW: { label: '预览 / 尚未执行', tone: 'preview', group: 'preview', terminal: true },
  PENDING: { label: '等待处理', tone: 'running', group: 'processing', terminal: false },
  PLANNING: { label: '规划中', tone: 'running', group: 'processing', terminal: false },
  EVALUATED: { label: '已评估', tone: 'running', group: 'processing', terminal: false },
  AWAITING_USER_INPUT: { label: '等待补充信息', tone: 'clarification', group: 'attention', terminal: false },
  RUNNING: { label: '执行中', tone: 'running', group: 'processing', terminal: false },
  CANCELLING: { label: '正在终止执行', tone: 'running', group: 'processing', terminal: false },
  SATISFIED: { label: '生效中', tone: 'success', group: 'enforcing', terminal: true },
  UNSATISFIABLE: { label: '当前意图未能生效', tone: 'failed', group: 'attention', terminal: true },
  FAILED: { label: '系统执行失败', tone: 'failed', group: 'attention', terminal: true },
  DEGRADED: { label: '已降级，需要处理', tone: 'clarification', group: 'attention', terminal: false },
  WITHDRAWING: { label: '正在撤销', tone: 'running', group: 'processing', terminal: false },
  WITHDRAW_PENDING_CLEANUP: { label: '撤销清理中', tone: 'clarification', group: 'attention', terminal: false },
  WITHDRAWN: { label: '已撤销', tone: 'cancelled', group: 'history', terminal: true },
  CANCELLED: { label: '执行已终止', tone: 'cancelled', group: 'history', terminal: true },
  CANCELLED_AFTER_COMMIT: { label: '执行已终止（策略仍生效）', tone: 'cancelled', group: 'enforcing', terminal: true },
})

const BACKEND_ALIASES = Object.freeze({
  received: 'PENDING', pending: 'PENDING', understanding: 'PLANNING', parsed: 'PLANNING',
  sensing: 'PLANNING', planning: 'PLANNING', plan_ready: 'EVALUATED', checking: 'EVALUATED',
  needs_clarification: 'AWAITING_USER_INPUT', pending_clarification: 'AWAITING_USER_INPUT',
  clarification: 'AWAITING_USER_INPUT', clarification_required: 'AWAITING_USER_INPUT',
  needs_approval: 'EVALUATED', deploying: 'RUNNING',
  acting: 'RUNNING', executing: 'RUNNING', queued: 'PENDING', verifying: 'RUNNING',
  success: 'SATISFIED', succeeded: 'SATISFIED', completed: 'SATISFIED', done: 'SATISFIED',
  rejected: 'UNSATISFIABLE', cancelled: 'CANCELLED', canceled: 'CANCELLED',
  cancelled_after_commit: 'CANCELLED_AFTER_COMMIT', cancelling: 'CANCELLING',
  violated: 'DEGRADED', diagnosing: 'DEGRADED', replanning: 'PLANNING',
  redeploying: 'RUNNING', waiting_for_infrastructure: 'DEGRADED',
})

export const LIFECYCLE_STATUSES = Object.freeze(Object.keys(DEFINITIONS))

const ACTIVE_EXECUTION_STATUSES = new Set([
  'PENDING', 'PLANNING', 'EVALUATED', 'RUNNING', 'CANCELLING',
])

export const INTERACTION_MODE = Object.freeze({
  ACTIVE: 'ACTIVE',
  AWAITING_INPUT: 'AWAITING_INPUT',
  TERMINAL: 'TERMINAL',
})

export function normalizeLifecycleStatus(value) {
  const raw = String(value || '').trim()
  if (!raw) return 'UNKNOWN'
  const upper = raw.toUpperCase()
  if (upper === 'CLARIFICATION_REQUIRED') return 'AWAITING_USER_INPUT'
  if (DEFINITIONS[upper]) return upper
  return BACKEND_ALIASES[raw.toLowerCase()] || 'UNKNOWN'
}

export function lifecyclePresentation(value) {
  const status = normalizeLifecycleStatus(value)
  return { status, ...DEFINITIONS[status] }
}

export function isTerminalLifecycle(value) {
  return lifecyclePresentation(value).terminal
}

export function isAwaitingUserInput(value) {
  return normalizeLifecycleStatus(value) === 'AWAITING_USER_INPUT'
}

export function isActivelyExecuting(value) {
  return ACTIVE_EXECUTION_STATUSES.has(normalizeLifecycleStatus(value))
}

export function lifecycleInteractionMode(value) {
  if (isAwaitingUserInput(value)) return INTERACTION_MODE.AWAITING_INPUT
  if (isActivelyExecuting(value)) return INTERACTION_MODE.ACTIVE
  return INTERACTION_MODE.TERMINAL
}

export function isCurrentLifecycle(value) {
  return lifecyclePresentation(value).group !== 'history'
}
