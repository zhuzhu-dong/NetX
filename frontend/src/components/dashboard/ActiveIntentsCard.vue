<template>
  <div class="chart-card active-intents">
    <p class="chart-card__title">活跃意图 ({{ activeList.length }})</p>
    <div v-if="!activeList.length" class="chart-card__empty">暂无活跃意图</div>
    <ul v-else class="intents-list">
      <li
        v-for="intent in activeList"
        :key="intent.id"
        class="intent-item"
        @click="$emit('intent-click', intent)"
      >
        <span :class="['intent-dot', intent.statusClass]"></span>
        <span class="intent-name">{{ intent.title }}</span>
        <span :class="['intent-status', intent.statusClass]">{{ intent.status }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useIntentStore } from '../../stores/intentStore'

const intentStore = useIntentStore()

defineEmits(['intent-click'])

const activeList = computed(() =>
  intentStore.intentHistory.filter((i) => !['success', 'failed', 'cancelled'].includes(i.status))
)
</script>

<style scoped>
.active-intents {
  min-height: 200px;
}

.intents-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.intent-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(248, 250, 252, 0.5);
  cursor: pointer;
  transition: background 0.2s ease;
}

.intent-item:hover {
  background: rgba(226, 232, 240, 0.5);
}

.intent-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.intent-dot.running {
  background: #38bdf8;
  animation: pulse-dot 1.4s ease-in-out infinite;
}

.intent-dot.pending {
  background: #f59e0b;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(1.6); }
}

.intent-name {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--color-text-primary, #0f172a);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.intent-status {
  font-family: var(--font-mono);
  font-size: 0.66rem;
  font-weight: 800;
  padding: 2px 8px;
  border-radius: 999px;
}

.intent-status.running {
  color: #0369a1;
  background: rgba(224, 242, 254, 0.86);
}

.intent-status.pending {
  color: #92400e;
  background: rgba(254, 243, 199, 0.86);
}

.intent-status.clarification {
  color: #6d28d9;
  background: rgba(237, 233, 254, 0.86);
}
</style>
