import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getCurrentIntents } from '../api/client'

/** Bounded, visibility-aware polling for the read-only current intents API. */
export function useCurrentIntents() {
  const snapshot = ref({ summary: { total: 0, processing: 0, enforcing: 0, attention: 0 }, groups: { processing: [], enforcing: [], attention: [] } })
  const loading = ref(false)
  const error = ref('')
  const hasLoaded = ref(false)
  let timer = null
  let inFlight = false
  let delay = 5000
  const groups = computed(() => snapshot.value.groups || { processing: [], enforcing: [], attention: [] })
  function clearTimer() { if (timer) window.clearTimeout(timer); timer = null }
  function schedule() { clearTimer(); if (!document.hidden) timer = window.setTimeout(async () => { await refresh(); schedule() }, delay) }
  async function refresh() {
    if (inFlight || document.hidden) return snapshot.value
    inFlight = true; loading.value = !hasLoaded.value
    try { const next = await getCurrentIntents(); snapshot.value = next; hasLoaded.value = true; error.value = ''; delay = 5000; return next }
    catch (_) { error.value = '当前网络意图状态暂时无法刷新。'; delay = Math.min(delay * 2, 30000); return snapshot.value }
    finally { loading.value = false; inFlight = false }
  }
  function onVisibility() { if (!document.hidden) refresh().finally(schedule); else clearTimer() }
  onMounted(() => { document.addEventListener('visibilitychange', onVisibility); refresh().finally(schedule) })
  onUnmounted(() => { clearTimer(); document.removeEventListener('visibilitychange', onVisibility) })
  return { snapshot, groups, loading, error, hasLoaded, refresh }
}
