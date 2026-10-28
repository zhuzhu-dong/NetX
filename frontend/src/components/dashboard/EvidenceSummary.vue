<template>
  <section class="evidence-summary" data-test="evidence-summary">
    <div class="summary-head">
      <strong>策略生效证据</strong>
      <span>{{ consensus.reasonText }}</span>
    </div>
    <p data-test="policy-preserved">{{ consensus.policyPreservedText }}</p>
    <p data-test="qos-mode">{{ qosModeLabel(plane.qos_mode) }}</p>
    <p data-test="int-quality">{{ intQualityLabel(observation.int) }}</p>
    <p data-test="path-change">{{ path.changeText }}；{{ path.currentText }}；{{ path.migrationText }}</p>

    <dl class="evidence-grid">
      <template v-for="item in evidence" :key="item.id">
        <dt>{{ item.label }}</dt>
        <dd :class="item.trusted ? 'trusted' : 'pending'">{{ item.value }}</dd>
      </template>
    </dl>

    <div v-if="capacity.physicalCount || capacity.directedCount" class="capacity-box">
      <p>容量证据：{{ capacity.physicalCount }} 条物理链路 / {{ capacity.directedCount }} 条有向链路</p>
      <article v-for="item in capacity.items" :key="item.physical_link_key || item.direction_id">
        {{ capacityLine(item) }}
      </article>
      <p v-if="!capacity.expanded && capacity.otherPhysicalLinkCount">其余 {{ capacity.otherPhysicalLinkCount }} 条物理链路已折叠</p>
      <button type="button" data-test="capacity-toggle" @click="expanded = !expanded">
        {{ capacity.toggleText }}
      </button>
    </div>

    <ul v-if="consensus.repairActions.length">
      <li v-for="action in consensus.repairActions" :key="action">修复建议：{{ action }}</li>
    </ul>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import {
  capacityViews,
  consensusPresentation,
  intQualityLabel,
  pathChangePresentation,
  policyEvidenceRows,
  qosModeLabel,
} from '../../utils/evidencePresentation'

const props = defineProps({ report: { type: Object, default: () => ({}) } })
const expanded = ref(false)
const report = computed(() => props.report || {})
const plane = computed(() => report.value.execution_plane || {})
const observation = computed(() => report.value.network_observation || {})
const conflict = computed(() => report.value.policy_conflict || {})
const consensus = computed(() => consensusPresentation(report.value.consensus_decision || {}))
const path = computed(() => pathChangePresentation(plane.value.path || {}))
const evidence = computed(() => policyEvidenceRows(plane.value, report.value.verification_result || {}))
const capacity = computed(() => capacityViews(
  conflict.value.capacity_evidence || report.value.consensus_decision?.reason_detail?.capacity_evidence || {},
  expanded.value,
))

function capacityLine(item) {
  if (item.direction_id) {
    return `${item.src_switch || item.src_device} → ${item.dst_switch || item.dst_device}：可用 ${item.available_mbps} Mbps`
  }
  return `${item.switch_a || item.device_a} ↔ ${item.switch_b || item.device_b}：容量 ${item.configured_capacity_mbps} Mbps${item.is_bottleneck ? '（瓶颈）' : ''}`
}
</script>

<style scoped>
.evidence-summary { margin-top: 12px; padding: 12px; border: 1px solid rgba(14,165,233,.3); border-radius: 12px; background: rgba(240,249,255,.7); }
.summary-head { display: flex; justify-content: space-between; gap: 8px; }
.summary-head span, p, li, dt, dd, article, button { font-size: .72rem; }
.evidence-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; }
.evidence-grid dd { margin: 0; font-weight: 850; }
.trusted { color: #047857; }
.pending { color: #92400e; }
.capacity-box article { margin-top: 5px; padding: 6px; border-radius: 7px; background: rgba(255,255,255,.72); }
button { margin-top: 8px; border: 0; border-radius: 999px; padding: 7px 10px; color: #0369a1; background: #e0f2fe; font-weight: 850; cursor: pointer; }
ul { padding-left: 18px; }
</style>
