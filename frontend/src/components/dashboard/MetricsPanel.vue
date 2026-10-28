<template>
  <section class="metrics-panel">
    <div v-for="metric in metrics" :key="metric.id || metric.label" class="metric-item" :class="metric.tone">
      <div>
        <span>{{ metric.label }}</span>
        <strong>{{ metric.value }}</strong>
        <small :class="metric.trend">{{ metric.change }}</small>
      </div>
      <i></i>
    </div>
  </section>
</template>

<script setup>
defineProps({
  metrics: {
    type: Array,
    default: () => []
  }
})
</script>

<style scoped>
.metrics-panel {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.metric-item {
  position: relative;
  min-height: 78px;
  padding: 14px;
  border: 1px solid rgba(203, 213, 225, 0.82);
  border-radius: 15px;
  background: rgba(255, 255, 255, 0.7);
  overflow: hidden;
}

.metric-item::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 3px;
  background: linear-gradient(90deg, rgba(56, 189, 248, 0.95), rgba(129, 140, 248, 0.85));
}

.metric-item span {
  display: block;
  color: #64748b;
  font-size: 0.75rem;
  font-weight: 850;
}

.metric-item strong {
  display: block;
  margin-top: 8px;
  color: #0f172a;
  font-size: 1.42rem;
  line-height: 1;
}

.metric-item small {
  display: block;
  margin-top: 7px;
  color: #64748b;
  font-size: 0.72rem;
  font-weight: 850;
}

.metric-item small.up {
  color: #0369a1;
}

.metric-item small.down {
  color: #047857;
}

.metric-item i {
  position: absolute;
  right: 14px;
  bottom: 14px;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  border: 1px solid rgba(56, 189, 248, 0.22);
}

.metric-item i::before {
  content: '';
  position: absolute;
  inset: 10px;
  border-radius: 50%;
  background: rgba(56, 189, 248, 0.5);
  box-shadow: 0 0 20px rgba(56, 189, 248, 0.58);
}

.metric-item.indigo::before {
  background: linear-gradient(90deg, #818cf8, #38bdf8);
}

.metric-item.green::before {
  background: linear-gradient(90deg, #22c55e, #38bdf8);
}

.metric-item.slate::before {
  background: linear-gradient(90deg, #64748b, #38bdf8);
}

.metric-item.violet::before {
  background: linear-gradient(90deg, #8b5cf6, #38bdf8);
}

.metric-item.amber::before {
  background: linear-gradient(90deg, #f59e0b, #ef4444);
}

@media (max-width: 760px) {
  .metrics-panel {
    grid-template-columns: 1fr;
  }
}
</style>
