<template>
  <div
    class="link-detail-card"
    :style="cardStyle"
    @pointerdown.stop
  >
      <!-- Close button -->
      <button type="button" class="close-btn" aria-label="关闭链路详情" @click.stop="$emit('close')">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 6l12 12M18 6 6 18"></path>
        </svg>
      </button>

      <!-- Header: link icon + endpoints -->
      <div class="card-header" @pointerdown="onDragStart">
        <span class="link-icon">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
          </svg>
        </span>
        <div class="endpoint-info">
          <p class="type-label">网络链路</p>
          <div class="endpoint-names">
            <strong>{{ sourceName }}</strong>
            <svg viewBox="0 0 24 24" class="arrow-icon" aria-hidden="true">
              <path d="M5 12h14M13 6l6 6-6 6"></path>
            </svg>
            <strong>{{ targetName }}</strong>
          </div>
        </div>
        <span class="status-chip" :class="link.state">{{ stateLabel }}</span>
      </div>

      <!-- Role & health bar -->
      <div class="role-row">
        <span class="role-chip" :class="link.role || 'primary'">{{ roleLabel }}</span>
        <div class="health-bar">
          <div class="health-fill" :class="link.state" :style="{ width: healthPercent + '%' }"></div>
        </div>
        <span class="health-text">{{ healthText }}</span>
      </div>

      <!-- Performance metrics -->
      <div class="metrics-section" v-if="perfCards.length">
        <div class="section-title">性能指标</div>
        <div class="perf-grid">
          <div v-for="p in perfCards" :key="p.key" class="perf-cell" :class="p.status">
            <span class="perf-label">{{ p.label }}</span>
            <strong class="perf-value">{{ p.value }}</strong>
          </div>
        </div>
      </div>

      <!-- Link detail info -->
      <div class="info-grid">
        <div class="info-cell">
          <span>链路 ID</span>
          <code>{{ link.id }}</code>
        </div>
        <div class="info-cell">
          <span>开销</span>
          <code>{{ link.cost ?? 1.0 }}</code>
        </div>
        <div class="info-cell">
          <span>带宽</span>
          <code>{{ link.bandwidth_mbps ? link.bandwidth_mbps + ' Mbps' : '-' }}</code>
        </div>
        <div class="info-cell">
          <span>源端口</span>
          <code>{{ sourcePort }}</code>
        </div>
        <div class="info-cell">
          <span>目的端口</span>
          <code>{{ targetPort }}</code>
        </div>
        <div class="info-cell">
          <span>角色</span>
          <code>{{ roleLabel }}</code>
        </div>
      </div>

      <!-- Path context -->
      <div class="path-context" v-if="pathContexts.length">
        <div class="section-title">所属路径</div>
        <div class="path-list">
          <span v-for="p in pathContexts" :key="p.name" class="path-chip" :class="p.type">
            {{ p.name }}
          </span>
        </div>
      </div>

      <!-- Quick intent actions -->
      <div class="actions-section">
        <div class="section-title">快捷操作</div>
        <div class="action-buttons">
          <button class="action-btn" @click="$emit('quick-intent', { text: `诊断 ${sourceName} → ${targetName} 的链路质量`, priority: 'medium' })">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><path d="M12 16v-4M12 8h.01"></path></svg>
            链路诊断
          </button>
          <button class="action-btn warn" @click="$emit('quick-intent', { text: `将 ${sourceName} → ${targetName} 的流量切换到备份路径`, priority: 'high' })">
            <svg viewBox="0 0 24 24"><path d="M17 1l4 4-4 4"></path><path d="M3 11V9a4 4 0 0 1 4-4h14"></path><path d="M7 23l-4-4 4-4"></path><path d="M21 13v2a4 4 0 0 1-4 4H3"></path></svg>
            切换路径
          </button>
          <button class="action-btn" @click="$emit('quick-intent', { text: `保障 ${sourceName} → ${targetName} 链路带宽不低于 10Gbps`, priority: 'high' })">
            <svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>
            带宽保障
          </button>
        </div>
      </div>
    </div>
</template>

<script setup>
import { computed } from 'vue'
import { useDraggable } from '../../composables/useDraggable'
import { NETWORK_STATE_LABEL, NETWORK_STATE_HEALTH, LINK_ROLE_LABEL } from '../../constants'

const props = defineProps({
  link: { type: Object, required: true },
  sourceNode: { type: Object, default: null },
  targetNode: { type: Object, default: null },
  metrics: { type: Array, default: () => [] },
  style: { type: Object, default: () => ({}) },
})

defineEmits(['close', 'quick-intent'])

const { onDragStart, dragStyle } = useDraggable()

const cardStyle = computed(() => ({
  ...props.style,
  ...dragStyle.value,
}))

const sourceName = computed(() => props.sourceNode?.name || props.link.from || '?')
const targetName = computed(() => props.targetNode?.name || props.link.to || '?')

const stateLabel = computed(() => NETWORK_STATE_LABEL[props.link.state] || props.link.state || '未知')

const roleLabel = computed(() => LINK_ROLE_LABEL[props.link.role] || props.link.role || '主链路')

const healthPercent = computed(() => NETWORK_STATE_HEALTH[props.link.state] || 100)

const healthText = computed(() => NETWORK_STATE_LABEL[props.link.state] || '未知')

const sourcePort = computed(() => {
  if (props.link.fromPort != null) return String(props.link.fromPort)
  const node = props.sourceNode
  if (!node || !Array.isArray(node.ports)) return '-'
  const match = node.ports.find(p => p.peer === props.link.to)
  return match ? `#${match.port ?? match.name}` : '-'
})

const targetPort = computed(() => {
  if (props.link.toPort != null) return String(props.link.toPort)
  const node = props.targetNode
  if (!node || !Array.isArray(node.ports)) return '-'
  const match = node.ports.find(p => p.peer === props.link.from)
  return match ? `#${match.port ?? match.name}` : '-'
})

const perfCards = computed(() => {
  const preferred = ['latency_ms', 'packet_loss_rate', 'throughput_mbps', 'queue_depth']
  const fallbackLabel = {
    latency_ms: '时延',
    packet_loss_rate: '丢包率',
    throughput_mbps: '吞吐量',
    queue_depth: '队列深度',
  }
  const byId = new Map(props.metrics.map(m => [m.id || m.key || m.label, m]))
  return preferred
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
})

const pathContexts = computed(() => {
  const contexts = []
  // Infer path membership from link endpoints
  if (sourceName.value.includes('h_') && targetName.value.includes('s_')) {
    contexts.push({ name: '终端接入', type: 'access' })
  } else if (sourceName.value.includes('s_') && targetName.value.includes('s_')) {
    if (props.link.role === 'backup') {
      contexts.push({ name: '备份路径', type: 'backup' })
    } else {
      contexts.push({ name: '主转发路径', type: 'primary' })
    }
  }
  // Add explicit path context if link has known path membership
  if (props.link.pathRole) {
    contexts.push({ name: props.link.pathRole, type: 'primary' })
  }
  return contexts.length ? contexts : [{ name: '通用转发', type: 'primary' }]
})
</script>

<style scoped>
.link-detail-card {
  width: 380px;
  max-height: 90vh;
  overflow-y: auto;
  padding: 18px;
  border: 1px solid rgba(148, 163, 184, 0.42);
  z-index: 25;
  border-radius: 22px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.95), rgba(248, 250, 252, 0.78)),
    radial-gradient(circle at 10% 0%, rgba(99, 102, 241, 0.14), transparent 44%),
    radial-gradient(circle at 90% 100%, rgba(56, 189, 248, 0.1), transparent 52%);
  box-shadow:
    0 28px 60px rgba(15, 23, 42, 0.18),
    0 0 0 1px rgba(255, 255, 255, 0.76) inset;
  animation: cardPopIn 0.24s cubic-bezier(0.34, 1.56, 0.64, 1);
}

.close-btn {
  position: absolute;
  right: 12px;
  top: 12px;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(203, 213, 225, 0.68);
  border-radius: 999px;
  color: #64748b;
  background: rgba(255, 255, 255, 0.68);
  cursor: pointer;
  z-index: 1;
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
  gap: 12px;
  padding-right: 32px;
  margin-bottom: 14px;
  cursor: grab;
  user-select: none;
}
.card-header:active { cursor: grabbing; }

.link-icon {
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: #6366f1;
  background: rgba(238, 242, 255, 0.82);
}

.link-icon svg {
  width: 22px;
  height: 22px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.endpoint-info {
  flex: 1;
  min-width: 0;
}

.type-label {
  margin: 0;
  color: #6366f1;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.6rem;
  font-weight: 950;
  letter-spacing: 1.2px;
  text-transform: uppercase;
}

.endpoint-names {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  min-width: 0;
}

.endpoint-names strong {
  color: #0f172a;
  font-size: 0.9rem;
  font-weight: 900;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.arrow-icon {
  width: 16px;
  height: 16px;
  flex: 0 0 16px;
  fill: none;
  stroke: #94a3b8;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
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

/* ── Role & Health ────────────────────────────────────────────────────────── */
.role-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 8px 10px;
  border: 1px solid rgba(203, 213, 225, 0.42);
  border-radius: 12px;
  background: rgba(248, 250, 252, 0.56);
}

.role-chip {
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 0.62rem;
  font-weight: 950;
  color: #2563eb;
  background: rgba(224, 242, 254, 0.72);
}

.role-chip.backup {
  color: #b45309;
  background: rgba(255, 251, 235, 0.72);
}

.health-bar {
  flex: 1;
  height: 6px;
  border-radius: 999px;
  background: rgba(203, 213, 225, 0.42);
  overflow: hidden;
}

.health-fill {
  height: 100%;
  border-radius: 999px;
  background: #22c55e;
  transition: width 0.4s ease, background 0.3s ease;
}

.health-fill.degraded { background: #f59e0b; }
.health-fill.failed, .health-fill.attack { background: #ef4444; }

.health-text {
  font-size: 0.62rem;
  font-weight: 900;
  color: #047857;
}

.health-text:has(+ .health-fill.degraded) ~ .health-text,
.role-row:has(.health-fill.degraded) .health-text { color: #b45309; }
.role-row:has(.health-fill.failed) .health-text,
.role-row:has(.health-fill.attack) .health-text { color: #b91c1c; }

/* ── Performance metrics ──────────────────────────────────────────────────── */
.metrics-section {
  margin-bottom: 10px;
}

.section-title {
  color: #475569;
  font-size: 0.58rem;
  font-weight: 950;
  letter-spacing: 0.08em;
  margin-bottom: 6px;
  text-transform: uppercase;
}

.perf-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  padding: 8px;
  border: 1px solid rgba(186, 230, 253, 0.52);
  border-radius: 14px;
  background:
    linear-gradient(135deg, rgba(239, 246, 255, 0.56), rgba(255, 255, 255, 0.42)),
    radial-gradient(circle at 18% 0%, rgba(99, 102, 241, 0.1), transparent 54%);
}

.perf-cell {
  padding: 7px 6px;
  border: 1px solid rgba(203, 213, 225, 0.46);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.62);
  text-align: center;
}

.perf-cell.ok {
  border-color: rgba(16, 185, 129, 0.24);
  background: rgba(240, 253, 250, 0.66);
}

.perf-cell.warn {
  border-color: rgba(245, 158, 11, 0.26);
  background: rgba(255, 251, 235, 0.7);
}

.perf-label {
  display: block;
  color: #64748b;
  font-size: 0.52rem;
  font-weight: 900;
}

.perf-value {
  display: block;
  margin-top: 4px;
  color: #0f172a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.66rem;
  font-weight: 950;
}

/* ── Info grid ────────────────────────────────────────────────────────────── */
.info-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin-bottom: 8px;
}

.info-cell {
  padding: 8px 10px;
  border: 1px solid rgba(203, 213, 225, 0.44);
  border-radius: 10px;
  background: rgba(248, 250, 252, 0.52);
}

.info-cell span {
  display: block;
  color: #64748b;
  font-size: 0.54rem;
  font-weight: 850;
  letter-spacing: 0.05em;
  margin-bottom: 3px;
}

.info-cell code {
  color: #0f172a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.64rem;
  font-weight: 700;
  word-break: break-all;
}

/* ── Path context ─────────────────────────────────────────────────────────── */
.path-context {
  margin-bottom: 8px;
  padding: 8px 10px;
  border: 1px solid rgba(203, 213, 225, 0.4);
  border-radius: 12px;
  background: rgba(248, 250, 252, 0.52);
}

.path-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.path-chip {
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 0.62rem;
  font-weight: 800;
  color: #2563eb;
  background: rgba(224, 242, 254, 0.72);
  border: 1px solid rgba(56, 189, 248, 0.18);
}

.path-chip.backup {
  color: #b45309;
  background: rgba(255, 251, 235, 0.72);
  border-color: rgba(245, 158, 11, 0.18);
}

/* ── Quick actions ────────────────────────────────────────────────────────── */
.actions-section {
  margin-top: 8px;
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
  color: #4f46e5;
  border-color: rgba(99, 102, 241, 0.48);
  background: rgba(238, 242, 255, 0.72);
  transform: translateY(-1px);
}

.action-btn.warn:hover {
  color: #b45309;
  border-color: rgba(245, 158, 11, 0.48);
  background: rgba(255, 251, 235, 0.78);
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

/* ── Animations ───────────────────────────────────────────────────────────── */
@keyframes overlayFadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes cardPopIn {
  0% {
    opacity: 0;
    transform: scale(0.9);
    filter: blur(4px);
  }
  100% {
    opacity: 1;
    transform: scale(1);
    filter: blur(0);
  }
}
</style>
