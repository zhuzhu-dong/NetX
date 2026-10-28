<template>
  <section class="diagnostics-page" data-test="system-diagnostics-page">
    <header>
      <div><span>INTENT-DRIVEN NETWORKING · SYSTEM PLANE</span><h1>System Diagnostics</h1><p>只诊断系统组件与控制循环，不展示或执行业务 Intent。</p></div>
      <div class="actions"><small>{{ updatedAt || 'STALE' }}</small><button @click="refresh">{{ loading ? '读取中…' : '刷新' }}</button><button class="primary" @click="router.push('/dashboard')">返回意图助手</button></div>
    </header>

    <div v-if="error" class="error">{{ error }}</div>
    <main class="component-grid">
      <article v-for="item in components" :key="item.name" :class="tone(item.status)">
        <div><span>{{ item.source }}</span><h2>{{ item.name }}</h2></div>
        <strong>{{ item.status }}</strong>
        <p>{{ item.detail }}</p>
      </article>
    </main>

    <section class="diagnostic-details">
      <article><span>Reconciler</span><strong>{{ reconciliationState }}</strong><p>{{ reconciliationDetail }}</p></article>
      <article><span>FaultController / Recovery</span><strong>{{ faultState }}</strong><p>{{ faultDetail }}</p></article>
      <article><span>Audit</span><strong>{{ auditCount }}</strong><p>最近审计事件仅从 `/audit` 读取。</p></article>
      <article><span>CPU / FD / process</span><strong>UNAVAILABLE</strong><p>当前后端没有暴露进程资源投影，界面不会伪造健康值。</p></article>
      <article><span>A2A</span><strong>{{ workerStatus }}</strong><p>以 Agent worker 就绪与错误计数作为当前可用的系统级运行证据。</p></article>
      <article><span>采集语义</span><strong>READ ONLY</strong><p>此页面不读取 selectedIntentId，也不会触发 Intent execution。</p></article>
    </section>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as api from '../../api/client'

const router = useRouter()
const health = ref({})
const topology = ref({})
const watcher = ref({})
const reconciliation = ref({})
const faults = ref([])
const audit = ref([])
const loading = ref(false)
const error = ref('')
const updatedAt = ref('')
let timer

const components = computed(() => {
  const c = health.value.components || {}
  const mininet = topology.value.mininet || {}
  return [
    entry('Backend', health.value.status, 'health/ready', `flow delivery ${health.value.flow_delivery_mode || 'UNAVAILABLE'}`),
    entry('LLM Provider', c.llm?.status, 'llm/readiness', `${c.llm?.runtime_mode || 'LLM_UNAVAILABLE'} · ${c.llm?.provider || 'NO_PROVIDER'} / ${c.llm?.model || 'NO_MODEL'}`),
    entry('ONOS', c.onos?.status, 'health/ready', `${c.onos?.available_devices ?? 'NO_SAMPLE'} / ${c.onos?.device_count ?? 'NO_SAMPLE'} devices · ${c.onos?.latency_ms ?? 'NO_SAMPLE'} ms`),
    entry('Mininet / BMv2', c.mininet?.status || mininet.status, 'topology/current', `${mininet.switches?.length ?? 'NO_SAMPLE'} switches · ${mininet.hosts?.length ?? 'NO_SAMPLE'} hosts`),
    entry('P4Runtime', c.qos?.status, 'health/ready', `required ${c.qos?.required ?? 'MISSING'} · ${health.value.flow_delivery_mode || 'UNAVAILABLE'}`),
    entry('TC', c.qos?.status, 'health/ready', 'QoS enforcement status; per-Intent readback belongs to Network Evidence.'),
    entry('Agent runtime', c.worker?.status, 'health/ready', `${c.worker?.active_jobs ?? 'NO_SAMPLE'} active · ${c.worker?.error_count ?? 'NO_SAMPLE'} errors · ${c.worker?.restart_count ?? 'NO_SAMPLE'} restarts`),
    entry('Watcher', watcher.value.state, 'watcher/status', `sample ${watcher.value.sample_seq ?? 'NO_SAMPLE'} · errors ${watcher.value.consecutive_errors ?? 'NO_SAMPLE'}`),
    entry('Telemetry', c.telemetry?.status, 'health/ready', 'System telemetry collector readiness.'),
  ]
})
const reconciliationState = computed(() => reconciliation.value.status || ((reconciliation.value.desired || []).length ? 'OBSERVED' : 'NO_SAMPLE'))
const reconciliationDetail = computed(() => `${(reconciliation.value.desired || []).length} desired · ${(reconciliation.value.observed || []).length} observed`)
const faultState = computed(() => faults.value.some((item) => !/RECOVERED|RESOLVED|CLEARED/i.test(item.lifecycle_state || item.status || '')) ? 'ATTENTION' : faults.value.length ? 'STABLE' : 'NO_SAMPLE')
const faultDetail = computed(() => `${faults.value.length} fault records; recovery state is preserved from the backend.`)
const auditCount = computed(() => audit.value.length || 'NO_SAMPLE')
const workerStatus = computed(() => health.value.components?.worker?.ready === true ? 'READY' : health.value.components?.worker?.status || 'UNAVAILABLE')

function entry(name, status, source, detail) { return { name, status: String(status || 'UNAVAILABLE').toUpperCase(), source, detail } }
function tone(status) {
  if (/OK|HEALTHY|READY|LIVE|ENFORCEMENT/.test(status)) return 'ok'
  if (/FAILED|ERROR|DOWN|UNAVAILABLE|ATTENTION/.test(status)) return 'bad'
  return 'warn'
}
async function refresh() {
  loading.value = true
  error.value = ''
  const requests = await Promise.allSettled([
    api.getHealthReady(), api.getTopology(), api.getWatcherStatus(), api.getReconciliationStatus(), api.getFaults(), api.getAuditEvents({ limit: 50 }),
  ])
  const targets = [health, topology, watcher, reconciliation]
  requests.slice(0, 4).forEach((result, index) => { if (result.status === 'fulfilled') targets[index].value = result.value || {} })
  if (requests[4].status === 'fulfilled') faults.value = requests[4].value?.faults || []
  if (requests[5].status === 'fulfilled') audit.value = requests[5].value?.items || requests[5].value || []
  const failures = requests.filter((item) => item.status === 'rejected')
  if (failures.length) error.value = `${failures.length} 个系统诊断投影不可用；未用默认健康值覆盖。`
  updatedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  loading.value = false
}
onMounted(() => { refresh(); timer = window.setInterval(refresh, 10000) })
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<style scoped>
.diagnostics-page{box-sizing:border-box;width:100%;height:100%;padding:28px 34px;overflow:auto;color:#172038;background:radial-gradient(circle at 75% 0,#e1f5ff,transparent 35%),linear-gradient(140deg,#f8fcff,#f5f3ff);user-select:text}.diagnostics-page>header{display:flex;align-items:flex-end;justify-content:space-between;gap:22px;max-width:1680px;margin:0 auto 18px}.diagnostics-page header span{color:#138bc9;font:850 10px ui-monospace;letter-spacing:.17em}.diagnostics-page h1{margin:7px 0 4px;font-size:32px}.diagnostics-page header p{margin:0;color:#6c7b90}.actions{display:flex;align-items:center;gap:8px}.actions small{color:#708196}.actions button{border:1px solid #c8dfed;border-radius:10px;padding:9px 12px;background:white;color:#247aa9;font-weight:800;cursor:pointer}.actions .primary{border-color:transparent;background:linear-gradient(135deg,#1598e4,#6c62ee);color:white}.error{max-width:1680px;margin:0 auto 12px;padding:11px;border-radius:10px;background:#ffe5e5;color:#bf3939}.component-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;max-width:1680px;margin:auto}.component-grid article,.diagnostic-details article{display:grid;padding:16px;border:1px solid #d6e4ee;border-top:3px solid #94a3b8;border-radius:14px;background:rgba(255,255,255,.8)}.component-grid article.ok{border-top-color:#10b981}.component-grid article.bad{border-top-color:#ef4444}.component-grid article.warn{border-top-color:#f59e0b}.component-grid article div span,.diagnostic-details span{color:#2c91ca;font:800 9px ui-monospace;letter-spacing:.1em}.component-grid h2{margin:4px 0 13px;font-size:15px}.component-grid strong,.diagnostic-details strong{font:850 12px ui-monospace}.component-grid p,.diagnostic-details p{margin:8px 0 0;color:#718196;font-size:10px;line-height:1.5}.diagnostic-details{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;max-width:1680px;margin:12px auto 0}.diagnostic-details strong{margin-top:8px}@media(max-width:1100px){.component-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:760px){.diagnostics-page>header{align-items:flex-start;flex-direction:column}.actions{flex-wrap:wrap}.component-grid,.diagnostic-details{grid-template-columns:1fr}}
</style>
