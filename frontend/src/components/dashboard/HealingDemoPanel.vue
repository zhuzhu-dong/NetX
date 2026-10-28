<template>
  <section class="healing-panel" :class="{ busy }">
    <div class="panel-head">
      <div>
        <p>Self Healing Demo</p>
        <h3>{{ demoTitle }}</h3>
      </div>
      <strong :class="statusClass">{{ statusLabel }}</strong>
    </div>

    <div v-if="busy" class="placeholder">
      正在执行：重置环境 -> 注入拥塞 -> 定位瓶颈 -> 切换备份路径 -> 复测 KPI
    </div>

    <div v-else-if="!report" class="placeholder">
      点击“一键自愈”后，这里会展示路径切换、拥塞定位和 KPI 对比。
    </div>

    <div v-else class="demo-body">
      <div class="path-row">
        <span>自愈前路径</span>
        <strong>{{ comparison.self_heal_before_path_text || '-' }}</strong>
      </div>
      <div class="path-row hot">
        <span>拥塞链路</span>
        <strong>{{ comparison.congested_link || '-' }}</strong>
      </div>
      <div class="path-row">
        <span>修复动作</span>
        <strong>{{ demoAction }}</strong>
      </div>
      <div class="path-row ok">
        <span>自愈后路径</span>
        <strong>{{ comparison.after_path_text || '-' }}</strong>
      </div>

      <div class="evidence-grid">
        <span>qdisc: {{ qdiscText }}</span>
        <span>INT: {{ intText }}</span>
      </div>

      <div class="kpi-grid">
        <div v-for="item in kpiRows" :key="item.key">
          <span>{{ item.label }}</span>
          <strong>{{ item.before }} -> {{ item.after }}</strong>
          <small>{{ item.delta }}</small>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  report: {
    type: Object,
    default: null,
  },
  busy: {
    type: Boolean,
    default: false,
  },
})

const comparison = computed(() => props.report?.comparison || {})
const evidence = computed(() => comparison.value.evidence || {})
const qdisc = computed(() => evidence.value.qdisc || {})
const intEvidence = computed(() => evidence.value.int || {})

const statusLabel = computed(() => {
  if (props.busy) return 'running'
  if (!props.report) return 'standby'
  if (comparison.value.switched_to_backup) return 'recovered'
  return props.report.status || 'partial'
})

const statusClass = computed(() => {
  if (props.busy) return 'running'
  if (comparison.value.switched_to_backup) return 'ok'
  if (props.report) return 'warn'
  return 'idle'
})

const demoTitle = computed(() => (
  comparison.value.switched_to_backup ? '路径切换与 KPI 对比' : 'KPI 恢复确认'
))

const demoAction = computed(() => (
  comparison.value.switched_to_backup ? '切换到备份路径 s_core2' : '执行修复策略并复测 KPI'
))

const qdiscText = computed(() => {
  if (!props.report) return '-'
  return qdisc.value.congestion_detected
    ? `检测到拥塞，队列 ${qdisc.value.queue_depth ?? 0} pkts`
    : '未检测到拥塞'
})

const intText = computed(() => {
  if (!props.report) return '-'
  if (intEvidence.value.primary_path) return '拥塞发生在主路径'
  if (intEvidence.value.secondary_path) return '流量已在备份路径'
  return '已采集逐跳路径'
})

function fmt(value, unit = '') {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'number') {
    const rounded = Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2)
    return `${rounded}${unit}`
  }
  return `${value}${unit}`
}

function deltaText(value, lowerIsBetter = true) {
  if (value === null || value === undefined) return 'delta -'
  const sign = value > 0 ? '+' : ''
  const improved = lowerIsBetter ? value < 0 : value > 0
  return `${sign}${fmt(value)} ${improved ? '改善' : '变化'}`
}

const kpiRows = computed(() => {
  const delta = comparison.value.kpi_delta || {}
  return [
    { key: 'latency_ms', label: '时延', unit: ' ms', lower: true },
    { key: 'packet_loss_rate', label: '丢包', unit: '', lower: true },
    { key: 'throughput_mbps', label: '吞吐', unit: ' Mbps', lower: false },
    { key: 'queue_depth', label: '队列', unit: ' pkts', lower: true },
  ].map((item) => {
    const value = delta[item.key] || {}
    return {
      ...item,
      before: fmt(value.before, item.unit),
      after: fmt(value.after, item.unit),
      delta: deltaText(value.delta, item.lower),
    }
  })
})
</script>

<style scoped>
.healing-panel {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid rgba(125, 211, 252, 0.62);
  border-radius: 15px;
  background:
    linear-gradient(135deg, rgba(240, 249, 255, 0.74), rgba(255, 255, 255, 0.62)),
    radial-gradient(circle at 100% 0%, rgba(14, 165, 233, 0.12), transparent 42%);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

p,
h3 {
  margin: 0;
}

p {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.66rem;
  font-weight: 900;
  letter-spacing: 1.4px;
  text-transform: uppercase;
}

h3 {
  margin-top: 3px;
  color: #0f172a;
  font-size: 0.9rem;
}

.panel-head strong {
  padding: 5px 9px;
  border-radius: 999px;
  font-size: 0.7rem;
}

.panel-head strong.ok {
  color: #047857;
  background: rgba(220, 252, 231, 0.9);
}

.panel-head strong.running {
  color: #0369a1;
  background: rgba(224, 242, 254, 0.9);
}

.panel-head strong.warn {
  color: #b45309;
  background: rgba(254, 243, 199, 0.9);
}

.panel-head strong.idle {
  color: #475569;
  background: rgba(241, 245, 249, 0.9);
}

.placeholder {
  margin-top: 10px;
  color: #64748b;
  font-size: 0.76rem;
  font-weight: 800;
  line-height: 1.45;
}

.demo-body {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.path-row {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  padding: 8px 9px;
  border-radius: 11px;
  background: rgba(255, 255, 255, 0.66);
}

.path-row span {
  color: #64748b;
  font-size: 0.68rem;
  font-weight: 900;
}

.path-row strong {
  color: #0f172a;
  font-size: 0.72rem;
  line-height: 1.35;
  word-break: break-word;
}

.path-row.hot strong {
  color: #b91c1c;
}

.path-row.ok strong {
  color: #047857;
}

.evidence-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.evidence-grid span,
.kpi-grid div {
  padding: 8px;
  border-radius: 11px;
  background: rgba(255, 255, 255, 0.56);
  color: #475569;
  font-size: 0.68rem;
  font-weight: 850;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.kpi-grid span,
.kpi-grid small {
  display: block;
  color: #64748b;
}

.kpi-grid strong {
  display: block;
  margin: 4px 0;
  color: #0f172a;
  font-size: 0.76rem;
}

.kpi-grid small {
  font-size: 0.65rem;
}
</style>
