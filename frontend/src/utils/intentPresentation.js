export const INTENT_LIFECYCLES = Object.freeze([
  'RECEIVED',
  'UNDERSTANDING',
  'NEEDS_CLARIFICATION',
  'PARSED',
  'SENSING',
  'PLANNING',
  'PLAN_READY',
  'CHECKING',
  'NEEDS_APPROVAL',
  'DEPLOYING',
  'VERIFYING',
  'SATISFIED',
  'DEGRADED',
  'VIOLATED',
  'DIAGNOSING',
  'REPLANNING',
  'REDEPLOYING',
  'UNSATISFIABLE',
  'FAILED',
  'WITHDRAWN',
])

const LEGACY_LIFECYCLE = Object.freeze({
  success: 'SATISFIED',
  cancelled: 'WITHDRAWN',
  canceled: 'WITHDRAWN',
  clarification: 'NEEDS_CLARIFICATION',
  pending_clarification: 'NEEDS_CLARIFICATION',
  pending_approval: 'NEEDS_APPROVAL',
  pending: 'RECEIVED',
  sensing: 'SENSING',
  thinking: 'UNDERSTANDING',
  planning: 'PLANNING',
  checking: 'CHECKING',
  acting: 'DEPLOYING',
  deploying: 'DEPLOYING',
  verifying: 'VERIFYING',
  failed: 'FAILED',
})

export const RUNTIME_STEPS = Object.freeze([
  { id: 'understand', label: 'Understand' },
  { id: 'sense', label: 'Sense' },
  { id: 'plan', label: 'Plan' },
  { id: 'check', label: 'Check' },
  { id: 'deploy', label: 'Deploy' },
  { id: 'assure', label: 'Assure' },
  { id: 'heal', label: 'Heal' },
])

const ACTIVE_STEP = Object.freeze({
  RECEIVED: 'understand',
  UNDERSTANDING: 'understand',
  NEEDS_CLARIFICATION: 'understand',
  PARSED: 'sense',
  SENSING: 'sense',
  PLANNING: 'plan',
  PLAN_READY: 'check',
  CHECKING: 'check',
  NEEDS_APPROVAL: 'check',
  DEPLOYING: 'deploy',
  VERIFYING: 'assure',
  SATISFIED: 'assure',
  DEGRADED: 'assure',
  VIOLATED: 'assure',
  DIAGNOSING: 'heal',
  REPLANNING: 'plan',
  REDEPLOYING: 'deploy',
  UNSATISFIABLE: 'heal',
  FAILED: 'heal',
  WITHDRAWN: null,
})

const ORDER = Object.freeze({ understand: 0, sense: 1, plan: 2, check: 3, deploy: 4, assure: 5, heal: 6 })

export function normalizeLifecycle(lifecycle, legacyStatus) {
  const primary = String(lifecycle || '').trim().toUpperCase()
  if (INTENT_LIFECYCLES.includes(primary)) return primary
  const legacy = String(legacyStatus || lifecycle || '').trim().toLowerCase()
  return LEGACY_LIFECYCLE[legacy] || (primary || 'RECEIVED')
}

export function lifecycleTone(value) {
  const lifecycle = normalizeLifecycle(value)
  if (lifecycle === 'SATISFIED') return 'success'
  if (['VIOLATED', 'FAILED', 'UNSATISFIABLE'].includes(lifecycle)) return 'failed'
  if (['DEGRADED', 'NEEDS_APPROVAL', 'NEEDS_CLARIFICATION'].includes(lifecycle)) return 'clarification'
  if (lifecycle === 'WITHDRAWN') return 'cancelled'
  return 'running'
}

export function runtimeStepStates(value) {
  const lifecycle = normalizeLifecycle(value)
  const active = ACTIVE_STEP[lifecycle]
  const activeIndex = active == null ? -1 : ORDER[active]
  const terminalFailure = ['VIOLATED', 'UNSATISFIABLE', 'FAILED'].includes(lifecycle)
  return RUNTIME_STEPS.map((step, index) => {
    let state = index < activeIndex ? 'complete' : index === activeIndex ? 'active' : 'waiting'
    if (lifecycle === 'SATISFIED' && step.id === 'assure') state = 'complete'
    if (lifecycle === 'WITHDRAWN') state = 'withdrawn'
    if (terminalFailure && index === activeIndex) state = 'failed'
    return { ...step, state }
  })
}

export function missingSemantic(value, fallback = 'MISSING') {
  if (value === null || value === undefined || value === '') return fallback
  if (Array.isArray(value) && value.length === 0) return 'NO_SAMPLE'
  return value
}
