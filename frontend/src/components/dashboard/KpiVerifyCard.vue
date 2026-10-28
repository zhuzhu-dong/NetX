<template>
  <section v-if="rows.length" class="kpi-verify-card">
    <div class="kpi-verify-head">
      <div>
        <span>KPI 闭环验证</span>
        <small>Verification</small>
      </div>
      <strong :class="passedClass">{{ passedLabel }}</strong>
    </div>

    <div class="kpi-verify-table">
      <div class="kpi-row kpi-header" :class="{ 'kpi-header--has-before': hasBefore }">
        <span>指标</span>
        <span v-if="hasBefore">执行前</span>
        <span>执行后</span>
        <span>状态</span>
      </div>
      <div
        v-for="row in rows"
        :key="row.key"
        class="kpi-row"
        :class="{ changed: row.changed, alert: row.alert, 'kpi-row--has-before': hasBefore }"
      >
        <span class="kpi-label">{{ row.label }}</span>
        <span v-if="hasBefore" class="kpi-value mono">{{ row.before }}</span>
        <span class="kpi-value mono">{{ row.after }}</span>
        <span class="kpi-status" :class="row.status">{{ row.statusText }}</span>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { formatMbps, formatMs, normalizePercent } from '../../composables/useFormatters'

const props = defineProps({
  verification: { type: Object, default: null },
})

const hasBefore = computed(() => {
  const before = props.verification?.before_kpi
  return before && Object.values(before).some(v => v != null)
})

const passed = computed(() => props.verification?.passed ?? props.verification?.intent_achieved ?? null)
const passedLabel = computed(() => {
  if (passed.value === true) return '验证通过'
  if (passed.value === false) return '未通过'
  return '待验证'
})
const passedClass = computed(() => {
  if (passed.value === true) return 'passed'
  if (passed.value === false) return 'failed'
  return 'pending'
})

const rows = computed(() => {
  const after = props.verification?.after_kpi || {}
  const before = props.verification?.before_kpi || {}
  if (!Object.keys(after).length && !Object.keys(before).length) return []

  const defs = [
    { key: 'latency_ms',      label: '核心时延',  fmt: v => formatMs(v) },
    { key: 'throughput_mbps', label: '吞吐量',    fmt: v => formatMbps(v) },
    { key: 'packet_loss_rate',label: '丢包率',    fmt: v => normalizePercent(v) },
    { key: 'queue_depth',     label: '队列深度',  fmt: v => (v != null && v !== '') ? `${v} pkt` : '--' },
  ]

  return defs.map(d => {
    const b = before[d.key]
    const a = after[d.key]
    const bNum = Number(b)
    const aNum = Number(a)
    const changed = hasBefore.value && Number.isFinite(bNum) && Number.isFinite(aNum) && Math.abs(aNum - bNum) > 0.001
    let alert = false
    if (d.key === 'latency_ms' && Number.isFinite(aNum)) alert = aNum > 10
    if (d.key === 'packet_loss_rate' && Number.isFinite(aNum)) alert = aNum > 0.01
    const status = a == null ? 'idle' : alert ? 'warn' : 'ok'

    return {
      ...d,
      before: hasBefore.value ? d.fmt(b) : undefined,
      after: d.fmt(a),
      changed,
      alert,
      status,
      statusText: status === 'ok' ? '✓ 正常' : status === 'warn' ? '⚠ 异常' : '—',
    }
  })
})
</script>

<style scoped>
.kpi-verify-card {
  margin-top: 12px;
  padding: 15px;
  border: 1px solid rgba(186, 230, 253, 0.52);
  border-radius: 16px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.74), rgba(248, 250, 252, 0.52)),
    radial-gradient(circle at 18% 0%, rgba(56, 189, 248, 0.08), transparent 54%);
  box-shadow: 0 14px 32px rgba(15, 23, 42, 0.06), inset 0 1px 0 rgba(255, 255, 255, 0.82);
  backdrop-filter: blur(12px);
}

.kpi-verify-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.kpi-verify-head span {
  display: block;
  color: #0f172a;
  font-size: 0.88rem;
  font-weight: 900;
}

.kpi-verify-head small {
  display: block;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.62rem;
  font-weight: 800;
  letter-spacing: 1px;
  text-transform: uppercase;
}

.kpi-verify-head strong {
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 0.68rem;
  font-weight: 900;
}

.kpi-verify-head strong.passed {
  color: #047857;
  background: rgba(209, 250, 229, 0.82);
}

.kpi-verify-head strong.failed {
  color: #b91c1c;
  background: rgba(254, 226, 226, 0.82);
}

.kpi-verify-head strong.pending {
  color: #64748b;
  background: rgba(241, 245, 249, 0.82);
}

/* ── Table ─────────────────────────────────────────────────────────────── */
.kpi-row {
  display: grid;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  border-radius: 10px;
  transition: background 0.14s ease;
}

.kpi-row:not(.kpi-header) {
  grid-template-columns: 90px 1fr 72px;
}

.kpi-row--has-before:not(.kpi-header) {
  grid-template-columns: 90px 80px 1fr 72px;
}

.kpi-header {
  grid-template-columns: 90px 1fr 72px;
  padding-bottom: 6px;
  margin-bottom: 2px;
  border-bottom: 1px solid rgba(203, 213, 225, 0.48);
}

.kpi-header--has-before {
  grid-template-columns: 90px 80px 1fr 72px;
}

.kpi-header span {
  color: #64748b;
  font-size: 0.6rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.kpi-label {
  color: #475569;
  font-size: 0.72rem;
  font-weight: 800;
}

.kpi-value {
  color: #0f172a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.76rem;
  font-weight: 750;
}

.kpi-status {
  text-align: right;
  font-size: 0.64rem;
  font-weight: 850;
}

.kpi-status.ok { color: #047857; }
.kpi-status.warn { color: #b45309; }
.kpi-status.idle { color: #94a3b8; }

/* ── Row states ───────────────────────────────────────────────────────── */
.kpi-row.changed {
  background: rgba(254, 243, 199, 0.40);
}

.kpi-row.alert {
  background: rgba(254, 226, 226, 0.40);
}

.kpi-row.changed.alert {
  background: rgba(254, 226, 226, 0.50);
}
</style>
