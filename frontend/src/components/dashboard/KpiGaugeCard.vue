<template>
  <div class="chart-card kpi-gauge-card" @click="$emit('card-click', metric)">
    <div class="kpi-gauge-head">
      <span class="kpi-label">{{ metric?.label || 'KPI' }}</span>
      <span :class="['kpi-status', toneClass]">{{ statusText }}</span>
    </div>
    <div class="kpi-gauge-body">
      <GaugeChart :data="gaugeOption" :height="148" :title="''" />
    </div>
    <div class="kpi-gauge-foot">
      <span class="kpi-value">{{ displayValue }}</span>
      <span :class="['kpi-change', trendClass]">{{ metric?.change || '-' }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import GaugeChart from '../charts/GaugeChart.vue'
import { toGaugeOption } from '../../composables/useChartData'

const props = defineProps({
  metric: { type: Object, default: () => ({}) },
})

defineEmits(['card-click'])

const gaugeOption = computed(() => toGaugeOption(props.metric))

const displayValue = computed(() => {
  const v = props.metric?.value
  if (v == null || v === '-') return '-'
  return v
})

const statusText = computed(() => {
  const s = props.metric?.status
  if (s === 'idle') return '待采集'
  if (s === 'live') return '实时'
  return s || '--'
})

const toneClass = computed(() => props.metric?.tone === 'red' ? 'tone-warn' : props.metric?.tone === 'green' ? 'tone-ok' : '')

const trendClass = computed(() => {
  const t = props.metric?.trend
  return t === 'up' ? 'trend-up' : t === 'down' ? 'trend-down' : ''
})
</script>

<style scoped>
.kpi-gauge-card {
  display: flex;
  flex-direction: column;
  padding: 12px var(--space-md, 14px);
  cursor: pointer;
  min-height: 200px;
}

.kpi-gauge-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2px;
}

.kpi-label {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  font-weight: 850;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: var(--color-text-secondary, #64748b);
}

.kpi-status {
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 0.64rem;
  font-weight: 800;
  color: var(--color-info, #0369a1);
  background: var(--color-info-bg, rgba(224,242,254,0.86));
}

.kpi-status.tone-warn {
  color: var(--color-warning, #b45309);
  background: var(--color-warning-bg, rgba(254,243,199,0.9));
}

.kpi-status.tone-ok {
  color: var(--color-success, #047857);
  background: var(--color-success-bg, rgba(209,250,229,0.84));
}

.kpi-gauge-body {
  flex: 1;
  min-height: 0;
}

.kpi-gauge-foot {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: 8px;
  margin-top: -8px;
}

.kpi-value {
  font-size: 1.3rem;
  font-weight: 900;
  color: var(--color-text-primary, #0f172a);
}

.kpi-change {
  font-size: 0.7rem;
  font-weight: 700;
  color: var(--color-text-secondary, #64748b);
}

.kpi-change.trend-up { color: var(--color-success, #047857); }
.kpi-change.trend-down { color: var(--color-failure, #b91c1c); }
</style>
