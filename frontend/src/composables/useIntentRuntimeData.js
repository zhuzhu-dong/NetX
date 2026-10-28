import { computed, ref, unref } from 'vue'
import * as api from '../api/client'
import { normalizeLifecycle } from '../utils/intentPresentation'

const PROJECTIONS = Object.freeze({
  record: api.getIntent,
  specResponse: api.getIntentSpec,
  runtimeResponse: api.getIntentRuntime,
  plan: api.getIntentPlan,
  execution: api.getIntentExecution,
  assurance: api.getIntentAssurance,
  history: api.getIntentHistory,
  decisions: api.getIntentDecisions,
  llmCalls: api.getIntentLlmCalls,
  evidence: api.getIntentEvidence,
  healing: api.getIntentHealing,
})

function emptyState() {
  return Object.fromEntries(Object.keys(PROJECTIONS).map((key) => [key, {}]))
}

/**
 * One source of truth for all Intent-scoped read projections.
 * Reading a historical Intent is deliberately side-effect free: only GET APIs
 * are used here; execute/replan/withdraw operations are never called.
 */
export function useIntentRuntimeData(intentIdSource) {
  const data = ref(emptyState())
  const loading = ref(false)
  const errors = ref({})
  const loadedIntentId = ref(null)

  const intentId = computed(() => String(unref(intentIdSource) || ''))
  const spec = computed(() => data.value.specResponse?.intent_spec || data.value.record?.intent_spec || {})
  const runtime = computed(() => data.value.runtimeResponse?.runtime || data.value.record?.intent_runtime || {})
  const lifecycle = computed(() => normalizeLifecycle(
    runtime.value.lifecycle || data.value.record?.intent_lifecycle,
    data.value.record?.status,
  ))

  async function load(explicitIntentId) {
    const id = String(explicitIntentId || intentId.value || '')
    if (!id) {
      data.value = emptyState()
      errors.value = {}
      loadedIntentId.value = null
      return null
    }

    loading.value = true
    const entries = Object.entries(PROJECTIONS)
    const settled = await Promise.allSettled(entries.map(([, loader]) => loader(id)))
    const next = emptyState()
    const nextErrors = {}

    settled.forEach((result, index) => {
      const key = entries[index][0]
      if (result.status === 'fulfilled') next[key] = result.value || {}
      else nextErrors[key] = result.reason?.message || String(result.reason)
    })

    data.value = next
    errors.value = nextErrors
    loadedIntentId.value = id
    loading.value = false
    return next
  }

  return {
    data,
    loading,
    errors,
    loadedIntentId,
    intentId,
    spec,
    runtime,
    lifecycle,
    load,
  }
}
