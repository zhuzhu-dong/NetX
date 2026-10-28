<template>
  <div class="chart-card event-timeline">
    <p class="chart-card__title">近期事件</p>
    <div v-if="!events.length" class="chart-card__empty">暂无事件</div>
    <ul v-else class="timeline-list">
      <li
        v-for="event in events"
        :key="event.id"
        class="timeline-item"
        :class="event.type"
        @click="$emit('event-click', event)"
      >
        <span class="tl-dot" :style="{ background: eventColor(event) }"></span>
        <span class="tl-time">{{ event.timeLabel }}</span>
        <span class="tl-text">{{ event.label }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useTelemetryStore } from '../../stores/telemetryStore'
import { useIntentStore } from '../../stores/intentStore'

const telemetry = useTelemetryStore()
const intentStore = useIntentStore()

defineEmits(['event-click'])

function eventColor(e) {
  if (e.type === 'alert') return '#ef4444'
  if (e.type === 'success') return '#10b981'
  return '#6366f1'
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts)
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

const events = computed(() => {
  const evts = []

  // Watcher incidents
  for (const inc of (telemetry.watcherIncidents || [])) {
    evts.push({
      id: `inc-${inc.id || inc.timestamp}`,
      label: inc.summary || inc.type || 'Watcher 告警',
      timeLabel: formatTime(inc.timestamp || inc.created_at),
      type: 'alert',
      link: null,
      raw: inc,
    })
  }

  // Recent intent milestones
  for (const intent of intentStore.intentHistory.slice(0, 5)) {
    if (!intent.milestones?.length) continue
    const last = intent.milestones[intent.milestones.length - 1]
    if (!last) continue
    evts.push({
      id: `ms-${intent.id}`,
      label: `${intent.title}: ${last.summary || last.stage}`,
      timeLabel: formatTime(intent.createdAt),
      type: intent.statusClass === 'success' ? 'success' : 'info',
      link: intent.id,
      raw: intent,
    })
  }

  return evts.slice(0, 8)
})
</script>

<style scoped>
.event-timeline {
  min-height: 200px;
}

.timeline-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.timeline-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(248, 250, 252, 0.5);
  cursor: pointer;
  transition: background 0.2s ease;
}

.timeline-item:hover {
  background: rgba(226, 232, 240, 0.5);
}

.tl-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tl-time {
  font-family: var(--font-mono);
  font-size: 0.68rem;
  font-weight: 700;
  color: var(--color-text-muted, #94a3b8);
  min-width: 36px;
}

.tl-text {
  font-size: 0.76rem;
  font-weight: 600;
  color: var(--color-text-primary, #0f172a);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
