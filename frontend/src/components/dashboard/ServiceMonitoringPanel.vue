<template>
  <section class="service-monitor" data-test="service-monitoring-panel">
    <article class="monitor-card network-health" data-test="network-health-region">
      <header><span>网络健康</span><small>Network Health Probe</small></header>
      <dl>
        <template v-for="row in healthRows" :key="row.key">
          <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
        </template>
      </dl>
    </article>

    <article class="monitor-card business-monitor" data-test="business-monitor-region">
      <header>
        <span>业务监控</span>
        <select :value="selectedServiceId" data-test="service-selector" @change="$emit('select', $event.target.value)">
          <option v-for="item in profiles" :key="item.service_id" :value="item.service_id">
            {{ item.service_name }} · {{ item.protocol }}{{ item.dst_port ? `/${item.dst_port}` : '' }}
          </option>
        </select>
      </header>
      <div class="selector-summary">
        <b>{{ selectedProfile?.service_name || selectedServiceId }}</b>
        <span>{{ selectedProfile?.src_host }} → {{ selectedProfile?.dst_host }}</span>
        <span>{{ selectedProfile?.protocol }}{{ selectedProfile?.dst_port ? `/${selectedProfile.dst_port}` : '' }}</span>
        <span>DSCP {{ selectedProfile?.dscp ?? 'N/A' }}</span>
        <span>状态 {{ selectedSample?.validation_result?.verification_state || selectedSample?.status || 'NO_SAMPLE' }}</span>
      </div>
      <dl data-test="protocol-metrics">
        <template v-for="row in metricRows" :key="row.key">
          <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
        </template>
      </dl>
      <div class="validation-status" data-test="validation-statuses">
        <div v-for="row in statusRows" :key="row.key">
          <span>{{ row.label }}</span><b>{{ row.value }}</b>
        </div>
      </div>
      <button type="button" :disabled="validationRunning" @click="$emit('validate')">
        {{ validationRunning ? '验证中…' : '运行该业务验证' }}
      </button>
    </article>

    <article class="monitor-card network-resource" data-test="network-resource-region">
      <header><span>网络资源</span><small>Exact service evidence</small></header>
      <dl>
        <template v-for="row in resources" :key="row.key">
          <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
        </template>
      </dl>
      <details v-if="evidenceReason" class="evidence-reason">
        <summary>展开证据异常详情</summary>
        <pre>{{ evidenceReason }}</pre>
      </details>
      <p>overlimits 为整形触发次数，不等同于 dropped 或 UDP lost_packets。</p>
    </article>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import {
  networkHealthRows,
  protocolMetricRows,
  resourceRows,
  validationStatusRows,
} from '../../utils/servicePresentation'

const props = defineProps({
  profiles: { type: Array, default: () => [] },
  services: { type: Object, default: () => ({}) },
  networkHealth: { type: Object, default: () => ({}) },
  infrastructure: { type: Object, default: () => ({}) },
  selectedServiceId: { type: String, default: '' },
  validationRunning: { type: Boolean, default: false },
})
defineEmits(['select', 'validate'])

const selectedProfile = computed(() => props.profiles.find((item) => item.service_id === props.selectedServiceId))
const selectedSample = computed(() => props.services[props.selectedServiceId] || { status: 'NO_SAMPLE' })
const healthRows = computed(() => networkHealthRows(props.networkHealth))
const metricRows = computed(() => protocolMetricRows(selectedProfile.value, selectedSample.value))
const statusRows = computed(() => validationStatusRows(selectedSample.value))
const resources = computed(() => resourceRows(selectedSample.value, props.infrastructure))
const evidenceReason = computed(() => {
  const counter = selectedSample.value?.p4_counter_evidence || {}
  if (!counter.reason_code && !counter.reason_detail && !counter.exception) return ''
  return JSON.stringify({
    status: counter.status,
    reason_code: counter.reason_code,
    reason_detail: counter.reason_detail,
    exception: counter.exception,
    read_request: counter.read_request,
    read_response: counter.read_response,
  }, null, 2)
})
</script>

<style scoped>
.service-monitor{display:grid;grid-template-columns:.9fr 1.2fr .9fr;gap:14px;margin-top:16px}.monitor-card{padding:16px;border:1px solid rgba(74,169,229,.24);border-radius:20px;background:rgba(255,255,255,.72);min-width:0}.monitor-card header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.monitor-card header span{font-weight:800;color:#18324e}.monitor-card header small{color:#7a8da3}.monitor-card select{max-width:58%;border:1px solid #b7d9ee;border-radius:10px;padding:6px;background:#fff}.monitor-card dl{display:grid;grid-template-columns:1fr auto;gap:8px 12px;margin:0}.monitor-card dt{color:#71849a}.monitor-card dd{margin:0;text-align:right;font-weight:700;color:#233d58;overflow-wrap:anywhere}.selector-summary{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}.selector-summary span,.selector-summary b{padding:4px 7px;border-radius:8px;background:#edf8ff;font-size:12px}.validation-status{display:grid;gap:5px;margin-top:12px;padding:10px;border-radius:12px;background:rgba(237,248,255,.72)}.validation-status div{display:flex;justify-content:space-between;gap:10px;font-size:12px}.validation-status span{color:#71849a}.validation-status b{text-align:right;color:#233d58}.monitor-card button{margin-top:12px;border:0;border-radius:10px;padding:8px 12px;background:#168bd2;color:#fff;font-weight:700}.monitor-card button:disabled{opacity:.55}.network-resource p{margin:12px 0 0;color:#7b6b51;font-size:12px}.evidence-reason{margin-top:10px;color:#7b6b51;font-size:12px}.evidence-reason pre{overflow:auto;max-height:180px;padding:8px;border-radius:8px;background:#f5f8fb;color:#334155;white-space:pre-wrap}@media(max-width:1100px){.service-monitor{grid-template-columns:1fr}}
</style>
