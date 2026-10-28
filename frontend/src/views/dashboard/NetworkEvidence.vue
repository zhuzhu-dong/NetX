<template>
  <section class="evidence-page" data-test="network-evidence-page">
    <header class="evidence-head">
      <div>
        <span>INTENT-DRIVEN NETWORKING · READ-ONLY EVIDENCE</span>
        <h1>Network Evidence</h1>
        <p>按 selectedIntentId 汇集计划、执行回读、P4/ONOS、TC、Telemetry、INT 与 Assurance 证据。</p>
      </div>
      <div class="head-actions">
        <select :value="selectedIntentId || ''" aria-label="选择 Intent" @change="selectEvidenceIntent($event.target.value)">
          <option value="">选择历史 Intent</option>
          <option v-for="item in intentHistory" :key="item.id" :value="item.id">{{ item.status }} · {{ item.id }}</option>
        </select>
        <button type="button" @click="returnToAssistant">返回意图助手</button>
      </div>
    </header>

    <div v-if="!selectedIntentId" class="empty-state">
      <strong>未选择 Intent</strong>
      <span>Network Evidence 不会用“最新 Intent”或固定业务冒充当前上下文。</span>
    </div>

    <template v-else>
      <section class="identity-strip">
        <article><span>Intent ID</span><strong>{{ selectedIntentId }}</strong></article>
        <article><span>Plan ID</span><strong>{{ planId }}</strong></article>
        <article><span>Execution ID</span><strong>{{ executionId }}</strong></article>
        <article><span>Freshness</span><strong>{{ freshness }}</strong></article>
      </section>

      <div v-if="loading" class="notice">正在读取真实 Intent 证据…</div>
      <div v-if="projectionErrors.length" class="notice warning">不可用投影：{{ projectionErrors.join(' / ') }}；其余区域不会用 0 填充。</div>

      <main class="evidence-grid">
        <EvidenceBlock title="Path" source="Intent Execution" :rows="pathRows" />
        <EvidenceBlock title="ONOS / P4" source="device receipts + flow readback" :rows="onosRows" />
        <EvidenceBlock title="P4 Direct Counters" source="ONOS flow projection" :rows="counterRows" />
        <EvidenceBlock title="TC" source="execution device-config readback" :rows="tcRows" />
        <EvidenceBlock title="Telemetry" source="Intent Evidence" :rows="telemetryRows" />
        <EvidenceBlock title="INT" source="Intent Evidence" :rows="intRows" />
        <EvidenceBlock title="Assurance" source="Intent Assurance" :rows="assuranceRows" />
        <EvidenceBlock title="Evidence Ledger" source="Intent Evidence API" :rows="ledgerRows" />
      </main>
    </template>
  </section>
</template>

<script setup>
import { computed, defineComponent, h, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { useIntentStore } from '../../stores/intentStore'
import { useIntentRuntimeData } from '../../composables/useIntentRuntimeData'
import { missingSemantic } from '../../utils/intentPresentation'
import * as api from '../../api/client'

const EvidenceBlock = defineComponent({
  props: { title: String, source: String, rows: Array },
  setup(props) {
    return () => h('section', { class: 'evidence-block' }, [
      h('header', [h('div', [h('span', props.source), h('h2', props.title)]), h('b', `${props.rows?.length || 0} records`)]),
      props.rows?.length
        ? h('div', { class: 'evidence-rows' }, props.rows.map((row, index) => h('article', { key: `${props.title}-${index}` }, Object.entries(row).map(([key, value]) => h('div', [h('small', key), h('strong', present(value))])))))
        : h('div', { class: 'missing' }, 'NO_SAMPLE'),
    ])
  },
})

const route = useRoute()
const router = useRouter()
const store = useIntentStore()
const { selectedIntentId, intentHistory } = storeToRefs(store)
const { data, loading, errors, load } = useIntentRuntimeData(selectedIntentId)
const systemEvidence = ref({ flows: [], telemetry: [] })
const systemErrors = ref({})

const runtime = computed(() => data.value.runtimeResponse?.runtime || data.value.record?.intent_runtime || {})
const execution = computed(() => data.value.execution?.current || {})
const assurance = computed(() => data.value.assurance?.current || {})
const evidenceItems = computed(() => data.value.evidence?.items || [])
const decisionReport = computed(() => data.value.record?.result?.decision_report || data.value.record?.decision_report || {})
const admission = computed(() => decisionReport.value.admission || {})
const preExecutionRejected = computed(() => admission.value.southbound_execution === 'NOT_EXECUTED' && admission.value.stop_category === 'CAPACITY_ADMISSION_REJECTED')
const notApplicableReason = computed(() => `Rejected during capacity admission: ${admission.value.final_reason_code || 'CAPACITY_ADMISSION_REJECTED'}`)
const planId = computed(() => preExecutionRejected.value ? 'NOT_CREATED' : (execution.value.plan_id || data.value.plan?.active?.plan_id || runtime.value.active_plan_id || 'MISSING'))
const executionId = computed(() => preExecutionRejected.value ? 'NOT_CREATED' : (execution.value.execution_id || runtime.value.active_execution_id || 'MISSING'))
const freshness = computed(() => preExecutionRejected.value ? 'NOT_APPLICABLE' : (assurance.value.observed_at || latestSample.value?.sampled_at || 'STALE'))
const latestSample = computed(() => [...evidenceItems.value].sort((a, b) => String(b.sampled_at).localeCompare(String(a.sampled_at)))[0])
const projectionErrors = computed(() => [...Object.keys(errors.value), ...Object.keys(systemErrors.value)])

const pathRows = computed(() => preExecutionRejected.value ? [{
  status: 'NOT_APPLICABLE', reason: notApplicableReason.value, path: 'NOT_APPLICABLE', config_verified: 'NOT_APPLICABLE',
}] : [{
  status: execution.value.status || 'UNAVAILABLE',
  path: execution.value.selected_path?.length ? execution.value.selected_path.join(' → ') : 'NO_SAMPLE',
  previous_execution_id: execution.value.previous_execution_id || 'NOT_APPLICABLE',
  config_verified: execution.value.config_verified ?? 'MISSING',
}])

const receiptFlowIds = computed(() => new Set((execution.value.device_receipts || []).flatMap((item) => item.flow_ids || []).map(String)))
const onosRows = computed(() => preExecutionRejected.value ? [{ state: 'NOT_APPLICABLE', reason: 'Southbound execution was not started; no ONOS/P4 classifier exists.' }] : (execution.value.device_receipts || []).map((item) => ({
  device: item.device_id,
  state: item.verified === true ? 'VERIFIED' : item.verified === false ? 'FAILED' : 'MISSING',
  requested_flows: missingSemantic(item.requested_flows),
  installed_flows: missingSemantic(item.installed_flows),
  flow_ids: item.flow_ids?.join(', ') || 'NO_SAMPLE',
})))
const counterRows = computed(() => preExecutionRejected.value ? [{ state: 'NOT_APPLICABLE', reason: 'No classifier was installed, so no direct-counter sample is applicable.' }] : systemEvidence.value.flows
  .filter((item) => receiptFlowIds.value.has(String(item.id)))
  .map((item) => ({
    device: item.deviceId || 'MISSING', table: item.tableId || 'MISSING', entry: item.id || 'MISSING',
    selector: item.selector || item.criteria || 'UNAVAILABLE', action: item.treatment || item.action || 'UNAVAILABLE',
    packets: item.packets ?? 'NO_SAMPLE', bytes: item.bytes ?? 'NO_SAMPLE', delta: item.delta ?? 'UNAVAILABLE',
})))
const tcRows = computed(() => preExecutionRejected.value ? [{ state: 'NOT_APPLICABLE', reason: 'No QoS scheduler configuration was applied.' }] : (execution.value.device_config_receipts || []).flatMap((receipt) => {
  const readback = receipt.tc_readback || receipt.readback || {}
  const resources = readback.resources || []
  if (resources.length) return resources.map(tcRow)
  return receipt.resource === 'TC_QOS' ? [tcRow(readback)] : []
}))
const telemetryRows = computed(() => preExecutionRejected.value ? [{ state: 'NOT_APPLICABLE', reason: 'Traffic verification was not started.' }] : evidenceItems.value.filter((item) => /(throughput|latency|loss|jitter)/i.test(`${item.metric} ${item.source}`)).map(evidenceRow))
const intRows = computed(() => preExecutionRejected.value ? [{ state: 'NOT_APPLICABLE', reason: 'INT verification was not started.' }] : evidenceItems.value.filter((item) => /(^|[^a-z])int([^a-z]|$)|hop|queue/i.test(`${item.source} ${item.metric}`)).map((item) => ({
  path: item.raw?.path || item.correlation?.path || 'NO_SAMPLE', hops: item.raw?.hops || 'NO_SAMPLE',
  latency: metricValue(item), queue: item.raw?.queue || 'NO_SAMPLE', quality: item.quality || 'MISSING', sampled_at: item.sampled_at || 'STALE',
})))
const assuranceRows = computed(() => preExecutionRejected.value ? [{ state: 'NOT_APPLICABLE', reason: 'Intent was rejected before service execution.' }] : (assurance.value.assessments || []).map((item) => ({
  target: `${item.objective_type || item.objective_id || 'MISSING'} ${item.expected ?? 'MISSING'} ${item.unit || ''}`.trim(),
  measured: item.observed ?? 'NO_SAMPLE', status: item.status || 'MISSING', evidence_source: item.evidence_ids?.join(', ') || 'NO_SAMPLE',
  quality: assurance.value.evidence_quality || 'MISSING', freshness: assurance.value.observed_at || 'STALE',
})))
const ledgerRows = computed(() => preExecutionRejected.value ? [{ state: 'PRE_EXECUTION_REJECTED', reason: notApplicableReason.value }] : evidenceItems.value.map(evidenceRow))

function tcRow(item = {}) {
  const stats = item.stats || {}
  return {
    interface: item.interface || item.interface_name || 'MISSING', class: item.class_id || 'MISSING', filter: item.filter_id ?? 'MISSING',
    rate: item.rate_mbps == null ? 'MISSING' : `${item.rate_mbps} Mbps`, ceil: item.ceil_mbps == null ? 'MISSING' : `${item.ceil_mbps} Mbps`,
    packets: stats.sent_packets ?? 'NO_SAMPLE', bytes: stats.sent_bytes ?? 'NO_SAMPLE', overlimits: stats.overlimits ?? 'NO_SAMPLE', drops: stats.dropped_packets ?? 'NO_SAMPLE',
  }
}
function evidenceRow(item = {}) { return { metric: item.metric || 'MISSING', value: metricValue(item), source: item.source || 'MISSING', quality: item.quality || 'MISSING', sampled_at: item.sampled_at || 'STALE' } }
function metricValue(item = {}) { return item.value == null ? 'NO_SAMPLE' : `${present(item.value)}${item.unit ? ` ${item.unit}` : ''}` }
function present(value) {
  const normalized = missingSemantic(value, 'MISSING')
  if (typeof normalized === 'object') return JSON.stringify(normalized)
  return String(normalized)
}

async function loadSystemEvidence() {
  systemErrors.value = {}
  const results = await Promise.allSettled([api.getFlows(), api.getTelemetryHistory({ limit: 200 })])
  const next = { flows: [], telemetry: [] }
  if (results[0].status === 'fulfilled') next.flows = results[0].value?.flows || []
  else systemErrors.value.flows = results[0].reason?.message || 'UNAVAILABLE'
  if (results[1].status === 'fulfilled') next.telemetry = results[1].value?.samples || results[1].value?.items || []
  else systemErrors.value.telemetry = results[1].reason?.message || 'UNAVAILABLE'
  systemEvidence.value = next
}

async function loadSelected(id) {
  if (!id) return
  selectedIntentId.value = id
  await Promise.all([store.selectIntentById(id), load(id)])
  if (!preExecutionRejected.value) await loadSystemEvidence()
}
function selectEvidenceIntent(id) {
  if (!id) return
  selectedIntentId.value = id
  router.push({ name: 'network-evidence-intent', params: { intentId: id } })
}
function returnToAssistant() {
  router.push(selectedIntentId.value ? { name: 'intent-assistant-context', params: { intentId: selectedIntentId.value } } : { name: 'intent-assistant' })
}

watch(() => route.params.intentId, (id) => loadSelected(String(id || selectedIntentId.value || '')))
onMounted(async () => {
  await store.loadIntents()
  const id = String(route.params.intentId || selectedIntentId.value || '')
  if (id) await loadSelected(id)
})
</script>

<style>
.evidence-page{box-sizing:border-box;width:100%;height:100%;padding:28px 34px;overflow:auto;color:#162038;background:radial-gradient(circle at 80% 0,#e0f4ff,transparent 34%),linear-gradient(140deg,#f8fcff,#f6f4ff);user-select:text}.evidence-head{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;max-width:1680px;margin:0 auto 18px}.evidence-head span{color:#148bc9;font:850 10px ui-monospace;letter-spacing:.17em}.evidence-head h1{margin:6px 0 4px;font-size:32px}.evidence-head p{margin:0;color:#6b7b90}.head-actions{display:flex;gap:8px}.head-actions select,.head-actions button{max-width:360px;border:1px solid #c8dfed;border-radius:10px;padding:10px 12px;background:rgba(255,255,255,.9);color:#297ba8;font-weight:750}.head-actions button{cursor:pointer}.identity-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;max-width:1680px;margin:0 auto 12px}.identity-strip article{display:grid;padding:13px 15px;border:1px solid #d7e5ef;border-radius:13px;background:rgba(255,255,255,.76)}.identity-strip span{color:#8090a3;font-size:10px}.identity-strip strong{margin-top:5px;font:800 11px ui-monospace;overflow-wrap:anywhere}.notice,.empty-state{max-width:1680px;margin:0 auto 12px;padding:13px 16px;border-radius:11px;background:#e5f4ff;color:#2479a6;font-size:11px}.notice.warning{background:#fff2d9;color:#95630d}.empty-state{display:grid;place-items:center;align-content:center;min-height:430px;background:rgba(255,255,255,.7);color:#77889c}.empty-state strong{font-size:22px;color:#283951;margin-bottom:8px}.evidence-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;max-width:1680px;margin:auto}.evidence-block{min-width:0;border:1px solid #d6e4ee;border-radius:17px;background:rgba(255,255,255,.8);overflow:hidden}.evidence-block>header{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid #e3ebf1}.evidence-block>header div{display:grid}.evidence-block>header span{color:#2b91cc;font:800 9px ui-monospace;letter-spacing:.12em}.evidence-block h2{margin:4px 0 0;font-size:16px}.evidence-block>header b{color:#8b9aad;font:750 9px ui-monospace}.evidence-rows{max-height:310px;overflow:auto}.evidence-rows article{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;padding:13px 17px;border-top:1px solid #eef3f6}.evidence-rows article:first-child{border-top:0}.evidence-rows div{display:grid;min-width:0}.evidence-rows small{color:#8796a8;font-size:9px}.evidence-rows strong{margin-top:4px;color:#3f5269;font:700 10px/1.5 ui-monospace;overflow-wrap:anywhere}.missing{display:grid;place-items:center;min-height:90px;color:#9a7b40;font:850 11px ui-monospace;background:#fffaf0}@media(max-width:950px){.evidence-head{align-items:flex-start;flex-direction:column}.head-actions{width:100%;flex-wrap:wrap}.identity-strip{grid-template-columns:1fr 1fr}.evidence-grid{grid-template-columns:1fr}}
</style>
