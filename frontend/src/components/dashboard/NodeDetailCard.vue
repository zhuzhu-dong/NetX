<template>
  <div
    class="node-detail-card"
    :class="{ reverse }"
    :style="cardStyle"
    @pointerdown.stop
  >
    <!-- Close button -->
    <button type="button" class="close-btn" aria-label="关闭节点详情" @click.stop="$emit('close')">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M6 6l12 12M18 6 6 18"></path>
      </svg>
    </button>

    <!-- Header: type icon + name + status (draggable) -->
    <div class="card-header" @pointerdown="onDragStart">
      <div class="header-type">
        <span class="type-icon" :class="node.type">
          <svg v-if="node.type === 'switch'" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="3" y="4" width="18" height="12" rx="2"></rect>
            <path d="M7 8h4M7 12h6"></path>
          </svg>
          <svg v-else-if="node.type === 'host'" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="2" y="3" width="20" height="14" rx="2"></rect>
            <path d="M8 21h8M12 17v4"></path>
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="10"></circle>
            <path d="M12 6v6l4 2"></path>
          </svg>
        </span>
        <div>
          <p class="type-label">{{ typeLabel }}</p>
          <h3>{{ node.name || node.id }}</h3>
        </div>
      </div>
      <span class="status-chip" :class="node.state">{{ stateLabel }}</span>
    </div>

    <!-- Identity -->
    <div class="identity-row">
      <span>{{ props.identityLabel.label }}</span>
      <code>{{ props.identityLabel.value }}</code>
    </div>

    <!-- Key metrics mini dashboard -->
    <div class="metrics-row" v-if="metricCards.length">
      <div
        v-for="m in metricCards"
        :key="m.key"
        class="metric-cell"
        :class="m.status"
      >
        <span class="metric-label">{{ m.label }}</span>
        <strong class="metric-value">{{ m.value }}</strong>
      </div>
    </div>

    <!-- Role & KPI quick stats -->
    <div class="quick-stats">
      <div class="stat">
        <span>角色</span>
        <strong>{{ node.role || shortRole }}</strong>
      </div>
      <div class="stat">
        <span>指标</span>
        <strong>{{ node.kpi || '-' }}</strong>
      </div>
      <div class="stat">
        <span>链路</span>
        <strong>{{ props.connectionCount }}</strong>
      </div>
      <div class="stat">
        <span>端口</span>
        <strong>{{ portLabel }}</strong>
      </div>
    </div>

    <!-- Ports table (switch only) -->
    <div class="ports-section" v-if="node.type === 'switch' && node.ports && node.ports.length">
      <div class="section-title">端口列表</div>
      <div class="ports-table">
        <div
          v-for="port in node.ports.slice(0, 8)"
          :key="port.port ?? port.name"
          class="port-row"
        >
          <span class="port-num">#{{ port.port ?? port.name }}</span>
          <span class="port-peer">{{ port.peer || port.name || '-' }}</span>
          <span class="port-state" :class="port.state || 'normal'"></span>
        </div>
        <div v-if="node.ports.length > 8" class="port-more">+{{ node.ports.length - 8 }} 更多端口</div>
      </div>
    </div>

    <!-- Quick intent actions -->
    <div class="actions-section">
      <div class="section-title">快捷操作</div>
      <div class="action-buttons">
        <template v-if="node.type === 'switch'">
          <button class="action-btn" @click="$emit('quick-intent', { text: `查看 ${node.name || node.id} 的实时流表统计`, priority: 'medium' })">
            <svg viewBox="0 0 24 24"><path d="M3 3v18h18"></path><path d="M7 16l4-8 4 4 4-6"></path></svg>
            流量统计
          </button>
          <button class="action-btn" @click="$emit('quick-intent', { text: `监控 ${node.name || node.id} 的所有端口状态`, priority: 'medium' })">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><path d="M12 6v6l4 2"></path></svg>
            端口监控
          </button>
          <button class="action-btn warn" @click="$emit('quick-intent', { text: `将 ${node.name || node.id} 从转发路径中临时隔离`, priority: 'high' })">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><path d="M4.93 4.93l14.14 14.14"></path></svg>
            隔离设备
          </button>
        </template>
        <template v-else-if="node.type === 'host'">
          <button class="action-btn" @click="$emit('quick-intent', { text: `持续监控主机 ${node.name || node.id} 的全部接口双向流量，采样周期为 5 秒，持续 10 分钟；展示吞吐量、包速率和丢包率，仅采集数据，不修改任何网络策略。`, priority: 'medium' })">
            <svg viewBox="0 0 24 24"><path d="M3 3v18h18"></path><path d="M7 16l4-8 4 4 4-6"></path></svg>
            监控流量
          </button>
          <button class="action-btn" @click="$emit('quick-intent', { text: `将主机 ${node.name || node.id} 的全部业务出站流量带宽上限设置为 100 Mbps，策略立即生效并持续 10 分钟；保留控制面流量，超限流量执行整形而不是直接丢弃。`, priority: 'high' })">
            <svg viewBox="0 0 24 24"><path d="M22 12A10 10 0 1 1 12 2a10 10 0 0 1 10 10z"></path><path d="M8 15h8"></path></svg>
            限制带宽
          </button>
          <button class="action-btn critical" @click="$emit('quick-intent', { text: `立即隔离主机 ${node.name || node.id}，阻断其与全网所有节点的双向业务通信，包括入站和出站流量；保留控制面管理通道，隔离持续 10 分钟，结束后自动恢复。`, priority: 'critical' })">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><path d="M4.93 4.93l14.14 14.14"></path></svg>
            隔离主机
          </button>
        </template>
        <template v-else>
          <button class="action-btn" @click="$emit('quick-intent', { text: `查看 ${node.name || node.id} 的自治编排状态`, priority: 'medium' })">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><path d="M12 6v6l4 2"></path></svg>
            编排状态
          </button>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useDraggable } from '../../composables/useDraggable'
import { NETWORK_STATE_LABEL, NODE_TYPE_LABEL } from '../../constants'

const props = defineProps({
  node: { type: Object, required: true },
  connectionCount: { type: Number, default: 0 },
  peerNames: { type: String, default: '' },
  linkSummary: { type: String, default: '' },
  linkRoleSummary: { type: String, default: '' },
  identityLabel: { type: Object, default: () => ({ label: '', value: '' }) },
  metrics: { type: Array, default: () => [] },
  style: { type: Object, default: () => ({}) },
  reverse: { type: Boolean, default: false },
})

defineEmits(['close', 'quick-intent'])

const { onDragStart, dragStyle } = useDraggable()

const cardStyle = computed(() => ({
  ...props.style,
  ...dragStyle.value,
}))

const typeLabel = computed(() => NODE_TYPE_LABEL[props.node.type] || '网络节点')

const stateLabel = computed(() => NETWORK_STATE_LABEL[props.node.state] || props.node.state || '未知')

const shortRole = computed(() => {
  const n = props.node
  if (n.type === 'host') return '主机'
  if (n.type === 'agent') return '智能体'
  if ((n.role || '').includes('核心')) return '核心交换机'
  if ((n.role || '').includes('汇聚')) return '汇聚交换机'
  return '交换机'
})

const portLabel = computed(() => {
  if (Array.isArray(props.node.ports)) return `${props.node.ports.length} 个`
  if (props.node.interface) return props.node.interface
  return '-'
})

const metricCards = computed(() => {
  const preferred = ['latency_ms', 'packet_loss_rate', 'throughput_mbps', 'queue_depth']
  const fallbackLabel = {
    latency_ms: '时延',
    packet_loss_rate: '丢包',
    throughput_mbps: '吞吐',
    queue_depth: '队列',
  }
  const byId = new Map(props.metrics.map(m => [m.id || m.key || m.label, m]))
  const selected = preferred
    .map(id => {
      const metric = byId.get(id)
      if (!metric) return null
      return {
        key: id,
        label: fallbackLabel[id] || metric.label || id,
        value: metric.value ?? '-',
        status: metric.status || 'idle',
      }
    })
    .filter(Boolean)
  if (selected.length) return selected
  return props.metrics.slice(0, 4).map((m, i) => ({
    key: m.id || m.key || `${m.label}-${i}`,
    label: m.label || '指标',
    value: m.value ?? '-',
    status: m.status || 'idle',
  }))
})
</script>

<style scoped>
.node-detail-card {
  position: absolute;
  z-index: 25;
  width: 300px;
  box-sizing: border-box;
  transform: translateY(-50%);
  padding: 16px;
  border: 1px solid rgba(148, 163, 184, 0.42);
  border-radius: 20px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.94), rgba(248, 250, 252, 0.78)),
    radial-gradient(circle at 10% 0%, rgba(56, 189, 248, 0.16), transparent 44%),
    radial-gradient(circle at 100% 100%, rgba(99, 102, 241, 0.1), transparent 52%);
  box-shadow:
    0 22px 52px rgba(15, 23, 42, 0.14),
    0 0 0 1px rgba(255, 255, 255, 0.72) inset;
  backdrop-filter: blur(20px) saturate(1.12);
  animation: detailPop 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
}

.node-detail-card.reverse {
  transform: translate(calc(-100% - 24px), -50%);
}

.close-btn {
  position: absolute;
  right: 10px;
  top: 10px;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(203, 213, 225, 0.68);
  border-radius: 999px;
  color: #64748b;
  background: rgba(255, 255, 255, 0.68);
  cursor: pointer;
}

.close-btn:hover {
  color: #0f172a;
  border-color: rgba(56, 189, 248, 0.58);
  background: rgba(224, 242, 254, 0.72);
}

.close-btn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
}

/* ── Header ───────────────────────────────────────────────────────────────── */
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-right: 30px;
  margin-bottom: 12px;
  cursor: grab;
  user-select: none;
}
.card-header:active { cursor: grabbing; }

.header-type {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.type-icon {
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: #0284c7;
  background: rgba(224, 242, 254, 0.82);
}

.type-icon.switch {
  color: #4f46e5;
  background: rgba(238, 242, 255, 0.82);
}

.type-icon.host {
  color: #0d9488;
  background: rgba(204, 251, 241, 0.78);
}

.type-icon svg {
  width: 20px;
  height: 20px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.type-label {
  margin: 0;
  color: #2563eb;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.6rem;
  font-weight: 950;
  letter-spacing: 1.2px;
  text-transform: uppercase;
}

.card-header h3 {
  margin: 2px 0 0;
  color: #0f172a;
  font-size: 1.08rem;
  font-weight: 900;
  line-height: 1.15;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-chip {
  flex: 0 0 auto;
  padding: 4px 10px;
  border: 1px solid rgba(148, 163, 184, 0.36);
  border-radius: 999px;
  background: rgba(239, 246, 255, 0.76);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.6rem;
  font-weight: 950;
  color: #2563eb;
}

.status-chip.normal {
  color: #047857;
  background: rgba(236, 253, 245, 0.74);
  border-color: rgba(16, 185, 129, 0.24);
}

.status-chip.degraded {
  color: #b45309;
  background: rgba(255, 251, 235, 0.78);
  border-color: rgba(245, 158, 11, 0.28);
}

.status-chip.failed,
.status-chip.attack {
  color: #b91c1c;
  background: rgba(254, 242, 242, 0.78);
  border-color: rgba(239, 68, 68, 0.26);
}

/* ── Identity ─────────────────────────────────────────────────────────────── */
.identity-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 10px;
  margin-bottom: 10px;
  border: 1px solid rgba(203, 213, 225, 0.44);
  border-radius: 10px;
  background: rgba(241, 245, 249, 0.56);
}

.identity-row span {
  color: #64748b;
  font-size: 0.6rem;
  font-weight: 850;
  letter-spacing: 0.05em;
}

.identity-row code {
  color: #0f172a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.66rem;
  font-weight: 700;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Metrics ──────────────────────────────────────────────────────────────── */
.metrics-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  margin-bottom: 10px;
  padding: 8px;
  border: 1px solid rgba(186, 230, 253, 0.52);
  border-radius: 14px;
  background:
    linear-gradient(135deg, rgba(239, 246, 255, 0.56), rgba(255, 255, 255, 0.42)),
    radial-gradient(circle at 18% 0%, rgba(56, 189, 248, 0.12), transparent 54%);
}

.metric-cell {
  min-width: 0;
  padding: 7px 6px;
  border: 1px solid rgba(203, 213, 225, 0.46);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.62);
  text-align: center;
}

.metric-cell.ok {
  border-color: rgba(16, 185, 129, 0.24);
  background: rgba(240, 253, 250, 0.66);
}

.metric-cell.warn {
  border-color: rgba(245, 158, 11, 0.26);
  background: rgba(255, 251, 235, 0.7);
}

.metric-label {
  display: block;
  color: #64748b;
  font-size: 0.52rem;
  font-weight: 900;
  letter-spacing: 0.04em;
}

.metric-value {
  display: block;
  margin-top: 4px;
  color: #0f172a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.68rem;
  font-weight: 950;
}

/* ── Quick stats ──────────────────────────────────────────────────────────── */
.quick-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin-bottom: 8px;
}

.stat {
  padding: 8px 10px;
  border: 1px solid rgba(203, 213, 225, 0.48);
  border-radius: 12px;
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.62), rgba(248, 250, 252, 0.42));
}

.stat span {
  display: block;
  color: #64748b;
  font-size: 0.56rem;
  font-weight: 900;
  letter-spacing: 0.06em;
}

.stat strong {
  display: block;
  margin-top: 4px;
  color: #0f172a;
  font-size: 0.74rem;
  font-weight: 900;
}

/* ── Ports ────────────────────────────────────────────────────────────────── */
.ports-section {
  margin-bottom: 8px;
}

.section-title {
  color: #475569;
  font-size: 0.58rem;
  font-weight: 950;
  letter-spacing: 0.08em;
  margin-bottom: 6px;
  text-transform: uppercase;
}

.ports-table {
  border: 1px solid rgba(203, 213, 225, 0.42);
  border-radius: 10px;
  overflow: hidden;
  max-height: 160px;
  overflow-y: auto;
}

.port-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border-bottom: 1px solid rgba(203, 213, 225, 0.24);
  font-size: 0.64rem;
}

.port-row:last-child { border-bottom: 0; }

.port-num {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 800;
  min-width: 36px;
}

.port-peer {
  color: #0f172a;
  font-weight: 700;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.port-state {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
}

.port-state.degraded { background: #f59e0b; box-shadow: 0 0 6px rgba(245, 158, 11, 0.5); }
.port-state.failed { background: #ef4444; box-shadow: 0 0 6px rgba(239, 68, 68, 0.5); }
.port-state.down { background: #ef4444; box-shadow: 0 0 6px rgba(239, 68, 68, 0.5); }

.port-more {
  padding: 4px 8px;
  color: #64748b;
  font-size: 0.58rem;
  font-weight: 800;
  text-align: center;
}

/* ── Detail rows ──────────────────────────────────────────────────────────── */
.detail-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  margin-top: 6px;
  border: 1px solid rgba(203, 213, 225, 0.4);
  border-radius: 10px;
  background: rgba(248, 250, 252, 0.52);
}

.detail-row span {
  color: #64748b;
  font-size: 0.58rem;
  font-weight: 850;
  letter-spacing: 0.05em;
  white-space: nowrap;
}

.detail-row strong {
  color: #0f172a;
  font-size: 0.68rem;
  font-weight: 900;
  text-align: right;
}

/* ── Quick actions ────────────────────────────────────────────────────────── */
.actions-section {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid rgba(203, 213, 225, 0.36);
}

.action-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 10px;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 999px;
  color: #334155;
  background: rgba(255, 255, 255, 0.68);
  font-size: 0.66rem;
  font-weight: 800;
  cursor: pointer;
  transition: all 0.14s ease;
}

.action-btn:hover {
  color: #0369a1;
  border-color: rgba(56, 189, 248, 0.48);
  background: rgba(224, 242, 254, 0.72);
  transform: translateY(-1px);
}

.action-btn.warn:hover {
  color: #b45309;
  border-color: rgba(245, 158, 11, 0.48);
  background: rgba(255, 251, 235, 0.78);
}

.action-btn.critical:hover {
  color: #b91c1c;
  border-color: rgba(239, 68, 68, 0.42);
  background: rgba(254, 242, 242, 0.78);
}

.action-btn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

/* ── Animation ────────────────────────────────────────────────────────────── */
@keyframes detailPop {
  0% {
    opacity: 0;
    transform: translateY(-50%) scale(0.92);
    filter: blur(4px);
  }
  100% {
    opacity: 1;
    transform: translateY(-50%) scale(1);
    filter: blur(0);
  }
}
</style>
