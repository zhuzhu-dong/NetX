<template>
  <div class="chart-card int-probe-card">
    <p class="chart-card__title">INT 带内遥测探针</p>
    <div v-if="!data" class="chart-card__empty">INT 探针未就绪 — 需 P4 交换机支持带内遥测</div>
    <template v-else>
      <div class="int-metrics">
        <div class="int-metric">
          <b>{{ data.kpi?.int_hop_count ?? '-' }}</b>
          <small>跳数</small>
        </div>
        <div class="int-metric">
          <b>{{ fmtLatency(data.kpi?.int_path_latency_ms) }}</b>
          <small>路径时延</small>
        </div>
        <div class="int-metric">
          <b>{{ data.kpi?.int_max_queue_occupancy ?? '-' }}</b>
          <small>最大队列占用</small>
        </div>
        <div class="int-metric">
          <b>{{ data.source || 'p4_int' }}</b>
          <small>数据源</small>
        </div>
      </div>
      <details v-if="hopRecords.length" class="int-hops">
        <summary>跳详情 ({{ hopRecords.length }})</summary>
        <div class="hop-table">
          <div v-for="h in hopRecords" :key="h.switch_id + h.ingress_port" class="hop-row">
            <span class="hop-sw">{{ h.switch_id }}</span>
            <span class="hop-port">端口 {{ h.ingress_port }} → {{ h.egress_port }}</span>
            <span class="hop-lat">{{ fmtNs(h.hop_latency_ns) }}</span>
          </div>
        </div>
      </details>
    </template>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: null },
})

const hopRecords = computed(() => (props.data?.hop_records || []).slice(0, 4))

function fmtLatency(ms) {
  if (ms == null) return '-'
  return Number(ms) < 1 ? `${(ms * 1000).toFixed(0)} µs` : `${Number(ms).toFixed(2)} ms`
}

function fmtNs(ns) {
  if (ns == null) return '-'
  if (ns < 1000) return `${ns} ns`
  if (ns < 1e6) return `${(ns / 1000).toFixed(1)} µs`
  return `${(ns / 1e6).toFixed(2)} ms`
}
</script>

<style scoped>
.int-probe-card { min-height: 120px; }

.int-metrics {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.int-metric {
  flex: 1;
  min-width: 70px;
  text-align: center;
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(248, 250, 252, 0.6);
}

.int-metric b {
  display: block;
  font-size: 1.05rem;
  font-weight: 950;
  color: #0f172a;
}

.int-metric small {
  font-size: 0.64rem;
  color: #64748b;
  font-weight: 700;
  text-transform: uppercase;
}

.int-hops {
  margin-top: 10px;
  border-top: 1px solid rgba(226, 232, 240, 0.5);
  padding-top: 8px;
}

.int-hops summary {
  font-size: 0.7rem;
  font-weight: 800;
  color: #64748b;
  cursor: pointer;
}

.hop-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  font-size: 0.7rem;
  border-bottom: 1px solid rgba(226, 232, 240, 0.3);
}

.hop-sw {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 700;
  color: #0f172a;
  min-width: 70px;
}

.hop-port { color: #64748b; flex: 1; }
.hop-lat { font-weight: 700; color: #0ea5e9; }
</style>
