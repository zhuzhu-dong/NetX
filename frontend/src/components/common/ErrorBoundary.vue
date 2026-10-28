<template>
  <div v-if="hasError" class="error-boundary-fallback">
    <div class="error-boundary-card">
      <div class="error-icon">⚠</div>
      <h3>{{ componentName }} 暂时不可用</h3>
      <p class="error-summary">{{ errorSummary }}</p>
      <button class="retry-btn" @click="retry">重试</button>
    </div>
  </div>
  <slot v-else :retry="retry" />
</template>

<script setup>
import { ref, onErrorCaptured } from 'vue'

const props = defineProps({
  componentName: { type: String, default: '组件' },
})

const hasError = ref(false)
const errorSummary = ref('')
const retryKey = ref(0)

const emit = defineEmits(['error'])

function retry() {
  hasError.value = false
  errorSummary.value = ''
  retryKey.value++
}

onErrorCaptured((err, instance, info) => {
  hasError.value = true
  errorSummary.value = err?.message || String(err)
  console.error(`[ErrorBoundary:${props.componentName}]`, err, info)
  emit('error', { componentName: props.componentName, error: err, info })
  return false // prevent propagation
})
</script>

<style scoped>
.error-boundary-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 120px;
  padding: 1rem;
}
.error-boundary-card {
  text-align: center;
  background: rgba(255, 255, 255, 0.85);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 12px;
  padding: 1.5rem 2rem;
  backdrop-filter: blur(8px);
  max-width: 360px;
}
.error-icon { font-size: 2rem; margin-bottom: 0.5rem; }
.error-boundary-card h3 { margin: 0 0 0.5rem; font-size: 1rem; color: #1e293b; }
.error-summary { font-size: 0.8rem; color: #64748b; margin: 0 0 1rem; word-break: break-all; }
.retry-btn {
  padding: 0.4rem 1.2rem;
  border: none;
  border-radius: 8px;
  background: #3b82f6;
  color: white;
  font-size: 0.85rem;
  cursor: pointer;
}
.retry-btn:hover { background: #2563eb; }
</style>
