<template>
  <div class="chart-card topology-donut">
    <p class="chart-card__title">网络拓扑健康分布</p>
    <div class="donut-layout">
      <div class="donut-chart">
        <DonutChart :data="donutOption" :title="''" :height="200" />
      </div>
      <div class="donut-legend">
        <div v-for="item in legendItems" :key="item.name" class="legend-item">
          <span class="legend-dot" :style="{ background: item.color }"></span>
          <span class="legend-label">{{ item.name }}</span>
          <span class="legend-count">{{ item.value }}</span>
        </div>
        <div class="legend-total">
          <span>总计节点</span>
          <strong>{{ totalNodes }}</strong>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import DonutChart from '../charts/DonutChart.vue'
import { toDonutOption } from '../../composables/useChartData'
import { useTopologyStore } from '../../stores/topologyStore'
import { STATE_COLOR_MAP } from '../../styles/echarts-theme'

const topology = useTopologyStore()

const legendItems = computed(() => {
  const map = {}
  for (const n of topology.topologyNodes) {
    const state = n.state || 'normal'
    map[state] = (map[state] || 0) + 1
  }
  const labels = { normal: '正常', degraded: '降级', failed: '故障', attack: '异常' }
  return Object.entries(map).map(([key, value]) => ({
    key,
    name: labels[key] || key,
    value,
    color: STATE_COLOR_MAP[key] || '#94a3b8',
  }))
})

const totalNodes = computed(() => topology.topologyNodes.length)

const donutOption = computed(() => {
  if (!totalNodes.value) return null
  return toDonutOption(legendItems.value)
})
</script>

<style scoped>
.topology-donut {
  min-height: 240px;
}

.donut-layout {
  display: flex;
  align-items: center;
  gap: 14px;
}

.donut-chart {
  flex: 1;
  min-width: 140px;
}

.donut-legend {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 90px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
  flex-shrink: 0;
}

.legend-label {
  font-size: 0.74rem;
  font-weight: 600;
  color: var(--color-text-secondary, #64748b);
  flex: 1;
}

.legend-count {
  font-family: var(--font-mono);
  font-size: 0.82rem;
  font-weight: 900;
  color: var(--color-text-primary, #0f172a);
}

.legend-total {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px solid rgba(203,213,225,0.5);
  font-size: 0.7rem;
  color: var(--color-text-secondary, #64748b);
}

.legend-total strong {
  font-family: var(--font-mono);
  font-size: 0.9rem;
  color: var(--color-text-primary, #0f172a);
}
</style>
