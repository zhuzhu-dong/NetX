<template>
  <section class="agent-page">
    <div v-if="!activeIntent" class="empty-state">
      <div class="empty-symbol"><span></span><span></span><span></span></div>
      <h2>暂无智能体执行记录</h2>
      <p>提交网络意图后，可在这里查看协作过程、决策依据与执行结果。</p>
      <button class="empty-btn" type="button" @click="router.push('/dashboard')">返回对话</button>
    </div>

    <template v-else>
      <header class="page-head">
        <div class="head-copy">
          <p>Agent Execution</p>
          <h2>智能体执行详情</h2>
          <span class="intent-summary">{{ activeIntent.text || activeIntent.title }}</span>
          <span class="intent-id">{{ currentIntentId }}</span>
        </div>
        <div class="head-right">
          <label v-if="executionOptions.length > 1" class="execution-switcher">
            <span>本对话意图</span>
            <select :value="currentIntentId" @change="handleExecutionChange">
              <option v-for="(option, index) in executionOptions" :key="option.id" :value="option.id">
                {{ executionOptions.length - index }} · {{ option.label }}
              </option>
            </select>
          </label>
          <span :class="['status-pill', statusTone]">{{ statusLabel }}</span>
          <button v-if="activeIntent.status === 'pending_approval'" class="action-btn approve" type="button" @click="handleApprove">批准</button>
          <button v-if="activeIntent.result?.rollback_available" class="action-btn rollback" type="button" @click="handleRollback">回滚</button>
        </div>
      </header>

      <div v-if="detailLoading" class="sync-strip"><i></i><span>正在同步执行证据与决策报告</span></div>
      <div v-else-if="fetchErrorCount" class="sync-strip warning"><i></i><span>部分技术数据暂不可用，已展示现有执行结果</span></div>

      <section class="overview-grid" aria-label="执行概览">
        <article v-for="metric in overviewMetrics" :key="metric.label" class="overview-metric" :class="metric.tone">
          <span>{{ metric.label }}</span>
          <strong>{{ metric.value }}</strong>
          <small>{{ metric.hint }}</small>
        </article>
      </section>

      <main class="content-grid">
        <section class="main-card card-pipeline full-span">
          <div class="section-title">
            <div>
              <span class="section-kicker">Orchestration</span>
              <h3>智能体执行时间线</h3>
            </div>
            <em>{{ pipelineSummary }}</em>
          </div>
          <AgentPipeline
            :stages="pipelineStore.pipelineStages"
            :selected-stage="pipelineStore.selectedStage"
            @select-stage="pipelineStore.selectStage"
          />

          <div v-if="participatingAgents.length || handoffEdges.length" class="collaboration-band">
            <div class="collaboration-copy">
              <span>Agent Collaboration</span>
              <strong>参与智能体与任务交接</strong>
              <small>{{ participantSummary }}</small>
            </div>
            <div v-if="participatingAgents.length" class="agent-list">
              <AgentCardInline v-for="agent in participatingAgents" :key="agent.agent_id" :agent="agent" :active="isAgentActive(agent.agent_id)" />
            </div>
            <HandoffFlow v-if="handoffEdges.length" :edges="handoffEdges" />
          </div>
        </section>

        <section v-if="decisionReport" class="main-card card-decision full-span">
          <div class="section-title">
            <div>
              <span class="section-kicker">Decision Assurance</span>
              <h3>决策与安全验证</h3>
            </div>
            <em>{{ decisionReport.summary || '报告已生成' }}</em>
          </div>

          <div class="decision-layout">
            <div class="decision-main">
              <div class="kpi-mini-row">
                <div class="kpi-mini"><small>策略冲突</small><b>{{ conflictCount }}</b></div>
                <div class="kpi-mini"><small>候选方案</small><b>{{ candidateSolutions.length }}</b></div>
                <div class="kpi-mini" :class="verdictClass"><small>安全评审</small><b>{{ verdictText }}</b></div>
                <div class="kpi-mini" :class="achievedClass"><small>KPI 结果</small><b>{{ achievedText || '待验证' }}</b></div>
              </div>

              <div v-if="isSkippedIntent" class="result-notice">{{ skippedReason }}</div>
              <div v-else-if="hasHealingComparison" class="chart-block">
                <RadarChart v-if="healingRadarOption" :data="healingRadarOption" title="自愈前后 KPI 对比" :height="284" />
                <div v-else class="result-notice">等待后台返回自愈 KPI 数据</div>
              </div>
              <div v-else-if="verificationRadarOption" class="chart-block">
                <RadarChart :data="verificationRadarOption" title="执行前后 KPI 对比" :height="284" />
              </div>
            </div>

            <aside class="decision-aside">
              <div v-if="intentTags.length || obsTags.length" class="insight-block">
                <h4>意图理解与网络观察</h4>
                <div class="tag-row">
                  <span v-for="tag in [...intentTags, ...obsTags]" :key="`${tag.label}-${tag.value}`" class="info-tag">
                    <small>{{ tag.label }}</small><strong>{{ tag.value }}</strong>
                  </span>
                </div>
              </div>

              <details v-if="showPolicyConflict" class="fold-block" open>
                <summary>策略冲突 <span :class="['mini-pill', conflictStatusClass]">{{ conflictStatusText }}</span></summary>
                <div class="fold-body">
                  <div class="conflict-metrics">
                    <span v-for="metric in conflictSeverityMetrics" :key="metric.key" :class="['conflict-metric', metric.className]">
                      <b>{{ metric.value }}</b><small>{{ metric.label }}</small>
                    </span>
                  </div>
                  <div v-if="conflictMatrix.length" class="conflict-matrix-simple">
                    <div v-for="(row, index) in conflictMatrix.slice(0, 5)" :key="index" class="matrix-row">
                      <span class="matrix-label">{{ row.policy_a || `P${index + 1}` }}</span>
                      <span class="matrix-arrow">vs</span>
                      <span class="matrix-label">{{ row.policy_b || '-' }}</span>
                      <span :class="['matrix-severity', matrixSeverityClass(row.severity)]">{{ row.severity || 'low' }}</span>
                    </div>
                  </div>
                </div>
              </details>

              <details v-if="hasSafetyReport" class="fold-block">
                <summary>安全评审 <span class="mini-pill" :class="safetyPillClass">{{ safetyReport.overall_decision || 'pending' }}</span></summary>
                <div class="fold-body">
                  <dl class="compact-dl">
                    <dt>风险评分</dt><dd>{{ safetyReport.overall_risk_score ?? '-' }} / 100</dd>
                    <dt>风险等级</dt><dd>{{ safetyReport.risk_level || '-' }}</dd>
                    <dt>硬阻断</dt><dd>{{ hardBlockersText }}</dd>
                  </dl>
                </div>
              </details>
            </aside>
          </div>

          <details v-if="candidateSolutions.length" class="fold-block section-fold">
            <summary>候选方案与智能体投票 <span class="mini-pill">{{ candidateSolutions.length }}</span></summary>
            <div class="fold-body">
              <div v-for="solution in candidateSolutions.slice(0, 4)" :key="solution.name || solution.action" class="solution-row">
                <b>{{ solution.name || solution.action }}</b>
                <span :class="riskClass(solution.risk_score)">风险 {{ solution.risk_score ?? '--' }}</span>
                <small>{{ solution.reason || '-' }}</small>
              </div>
              <div v-for="vote in agentVotes.slice(0, 3)" :key="vote.agent_id" class="vote-row">
                <b>{{ vote.agent_id }}</b><em>{{ vote.vote || vote.decision }}</em><small>{{ vote.reason || '-' }}</small>
              </div>
            </div>
          </details>
        </section>

        <section v-if="hasExecutionOutcome" class="main-card card-results full-span">
          <div class="section-title">
            <div>
              <span class="section-kicker">Delivery & Verification</span>
              <h3>变更影响与执行结果</h3>
            </div>
            <em :class="achievedClass">{{ achievedText || statusLabel }}</em>
          </div>

          <div class="result-grid">
            <div v-if="policyName" class="result-panel">
              <h4>策略下发</h4>
              <dl class="compact-dl">
                <dt>策略名称</dt><dd>{{ policyName }}</dd>
                <dt>业务动作</dt><dd>{{ businessAction || '-' }}</dd>
                <dt>影响设备</dt><dd>{{ affectedDevices || '-' }}</dd>
                <dt>表项数量</dt><dd>{{ tableEntryCount || '-' }}</dd>
              </dl>
            </div>
            <div v-if="beforePath || afterPath" class="result-panel">
              <h4>路径变化</h4>
              <dl class="compact-dl">
                <dt>变更前</dt><dd><span class="path-strip">{{ beforePath || '-' }}</span></dd>
                <dt>变更后</dt><dd><span class="path-strip after">{{ afterPath || '-' }}</span></dd>
                <dt>瓶颈链路</dt><dd>{{ bottleneckLink || '-' }}</dd>
                <dt>执行动作</dt><dd>{{ healingActions || '-' }}</dd>
              </dl>
            </div>
            <div v-if="confidenceOption" class="result-panel confidence-panel">
              <GaugeChart :data="confidenceOption" :title="''" :height="150" />
            </div>
          </div>
        </section>

        <details v-if="hasTechnicalDetails" class="technical-drawer full-span">
          <summary>
            <span><b>技术证据</b><small>Worker 作业、A2A 消息、相似案例与运行状态</small></span>
            <em>{{ technicalEvidenceCount }} 项</em>
          </summary>
          <div class="technical-grid">
            <div v-if="intentJobs.length" class="technical-block">
              <h4>Worker 作业</h4>
              <div v-for="job in intentJobs" :key="job.job_id" class="jobs-row">
                <span class="jobs-id">{{ job.job_id }}</span><span class="jobs-stage">{{ job.stage || '-' }}</span><span :class="['jobs-status', job.status]">{{ job.status || '-' }}</span>
              </div>
            </div>
            <div v-if="a2aTimeline.length" class="technical-block">
              <h4>A2A 协作证据链</h4>
              <div v-for="item in a2aTimeline.slice(0, 8)" :key="item.id" class="trace-row">
                <b>{{ item.source_agent || 'orch' }} → {{ item.target_agent || '-' }}</b><span>{{ item.message_type || item.method }}</span><small>{{ item.output_summary || item.input_summary || '-' }}</small>
              </div>
            </div>
            <div v-if="filteredCases.length" class="technical-block">
              <h4>历史相似案例</h4>
              <div v-for="item in filteredCases" :key="item.case_id" class="solution-row">
                <b>{{ item.case_id }}</b><span :class="item.success ? 'risk-low' : 'risk-high'">{{ item.success ? '成功' : '失败' }}</span><small>{{ item.root_cause || item.symptom || '-' }}</small>
              </div>
            </div>
            <div class="technical-block">
              <h4>Watcher 运行状态</h4>
              <dl class="compact-dl">
                <dt>当前状态</dt><dd>{{ watcherState }}</dd>
                <dt>连续异常</dt><dd>{{ telemetryStore.watcherStatus?.consecutive_bad ?? '-' }}</dd>
                <dt>活跃事件</dt><dd>{{ activeIncidentId || '无' }}</dd>
                <dt>冷却剩余</dt><dd>{{ cooldownText }}</dd>
              </dl>
            </div>
          </div>
        </details>
      </main>
    </template>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useIntentStore } from '../../stores/intentStore'
import { usePipelineStore } from '../../stores/pipelineStore'
import { useTelemetryStore } from '../../stores/telemetryStore'
import * as api from '../../api/client'
import AgentPipeline from '../../components/dashboard/AgentPipeline.vue'
import RadarChart from '../../components/charts/RadarChart.vue'
import GaugeChart from '../../components/charts/GaugeChart.vue'
import AgentCardInline from '../../components/dashboard/AgentCardInline.vue'
import HandoffFlow from '../../components/dashboard/HandoffFlow.vue'
import { toRadarOption, toGaugeOption, scaleKpiForRadar, RADAR_KPI_DIMENSIONS } from '../../composables/useChartData'
import { formatMbps, formatMs, formatPercent, formatSeconds, asArray, belongsToCurrentIntent, normalizeSafetyReview } from '../../composables/useFormatters'
import { firstText, firstKpi, formatQueue, formatActionList } from '../../composables/useDataExtractors'

const router = useRouter()
const route = useRoute()
const intentStore = useIntentStore()
const pipelineStore = usePipelineStore()
const telemetryStore = useTelemetryStore()

/* ── Active execution ── */
const requestedIntentId = computed(() => String(route.query.intent_id || ''))

const contextRecord = computed(() => {
  const requested = requestedIntentId.value
  if (requested) {
    const matched = intentStore.intentHistory.find((record) => (
      String(record.id || '') === requested
      || String(record.activeRunId || '') === requested
      || (record.runIds || []).some((id) => String(id) === requested)
      || Object.prototype.hasOwnProperty.call(record.intentResults || {}, requested)
    ))
    if (matched) return matched
  }
  return intentStore.activeIntent
})

const executionOptions = computed(() => {
  const context = contextRecord.value
  if (!context) return []

  const sessionId = String(context.sessionId || context.session_id || '')
  const candidates = sessionId
    ? intentStore.intentHistory.filter((record) => (
        String(record.sessionId || record.session_id || '') === sessionId
        || String(record.id || '') === sessionId
      ))
    : [context]
  const byId = new Map()
  const order = []

  const addExecution = (id, runtime = {}, source = context) => {
    const normalizedId = String(id || '')
    if (!/^INT-/i.test(normalizedId)) return
    if (!byId.has(normalizedId)) order.push(normalizedId)
    byId.set(normalizedId, {
      ...(byId.get(normalizedId) || {}),
      ...source,
      ...runtime,
      id: normalizedId,
      sessionId: source.sessionId || source.session_id || sessionId,
      activeRunId: normalizedId,
      result: runtime.result ?? source.result ?? null,
      milestones: runtime.milestones || source.milestones || [],
      snapshot: runtime.snapshot || source.snapshot || null,
    })
  }

  for (const record of candidates) {
    if (/^INT-/i.test(String(record.id || ''))) addExecution(record.id, record, record)
    for (const id of record.runIds || []) addExecution(id, record.intentResults?.[id] || {}, record)
    for (const [id, runtime] of Object.entries(record.intentResults || {})) addExecution(id, runtime, record)
  }

  return order.map((id) => {
    const execution = byId.get(id)
    const text = execution.text || execution.content || `意图 ${id.slice(-8)}`
    return {
      id,
      execution,
      label: text.length > 28 ? `${text.slice(0, 28)}…` : text,
    }
  }).reverse()
})

const selectedExecutionId = computed(() => {
  const availableIds = executionOptions.value.map((option) => option.id)
  if (availableIds.includes(requestedIntentId.value)) return requestedIntentId.value
  return availableIds[0] || ''
})

const activeIntent = computed(() => (
  executionOptions.value.find((option) => option.id === selectedExecutionId.value)?.execution
  || (/^INT-/i.test(String(contextRecord.value?.id || '')) ? contextRecord.value : null)
))
const result = computed(() => activeIntent.value?.result || {})
const currentIntentId = computed(() => selectedExecutionId.value || activeIntent.value?.id || '')

function handleExecutionChange(event) {
  const intentId = String(event.target.value || '')
  if (!intentId || intentId === currentIntentId.value) return
  router.replace({ path: route.path, query: { ...route.query, intent_id: intentId } })
}

watch([executionOptions, requestedIntentId], () => {
  const intentId = selectedExecutionId.value
  if (!intentId || requestedIntentId.value === intentId) return
  router.replace({ path: route.path, query: { ...route.query, intent_id: intentId } })
}, { immediate: true })

/* ── Fetched data (API endpoints) ── */
const fetchedReport = ref(null)
const fetchedTrace = ref(null)
const agentsList = ref([])
const experienceCases = ref([])
const intentJobs = ref([])
const detailLoading = ref(false)
const fetchErrorCount = ref(0)

let detailRequestSequence = 0

async function fetchAllData() {
  const id = currentIntentId.value
  if (!id) return

  const requestSequence = ++detailRequestSequence
  detailLoading.value = true
  fetchErrorCount.value = 0
  fetchedReport.value = null
  fetchedTrace.value = null
  intentJobs.value = []
  experienceCases.value = []

  pipelineStore.resetPipeline()
  if (activeIntent.value?.milestones?.length) {
    pipelineStore._updatePipelineFromMilestones(activeIntent.value.milestones)
  } else {
    pipelineStore.setPipelineFromStatus(activeIntent.value?.statusClass || activeIntent.value?.status)
  }

  const [report, trace, agents, cases, jobs] = await Promise.allSettled([
    api.getDecisionReport(id),
    api.getA2ATrace(id),
    api.getAgents(),
    api.getExperienceCases({ limit: 5 }),
    api.getIntentJobs(id),
  ])
  if (requestSequence !== detailRequestSequence || id !== currentIntentId.value) return

  fetchErrorCount.value = [report, trace, agents, cases, jobs]
    .filter((request) => request.status === 'rejected').length

  if (report.status === 'fulfilled') fetchedReport.value = report.value?.decision_report || report.value
  if (trace.status === 'fulfilled') {
    fetchedTrace.value = trace.value
    if (trace.value?.messages) pipelineStore.enrichStagesFromTrace(id, trace.value.messages)
  }
  if (agents.status === 'fulfilled') agentsList.value = agents.value?.items || []
  if (cases.status === 'fulfilled') experienceCases.value = cases.value?.items || []
  if (jobs.status === 'fulfilled') intentJobs.value = jobs.value?.jobs || []

  // Fallback: enrich stages from result's A2A timeline if API didn't deliver
  if (!fetchedTrace.value) {
    const msgs = activeIntent.value?.result?.a2a_runtime_evidence?.messages
      || activeIntent.value?.result?.a2a_timeline
    if (msgs?.length) pipelineStore.enrichStagesFromTrace(id, msgs)
  }

  detailLoading.value = false
}

watch(currentIntentId, (id) => { if (id) fetchAllData() }, { immediate: true })

/* ── Merged data: prefer fetched endpoint, fallback to result ── */
const decisionReport = computed(() => fetchedReport.value || result.value?.decision_report || null)

/* ── Page summary ── */
const normalizedStatus = computed(() => String(
  activeIntent.value?.statusClass
  || activeIntent.value?.status
  || result.value?.status
  || 'running'
).toLowerCase())

const statusLabel = computed(() => ({
  success: '执行完成',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已取消',
  pending_approval: '等待审批',
  pending_clarification: '等待补充',
  clarification: '等待补充',
  running: '执行中',
  processing: '执行中',
}[normalizedStatus.value] || '执行中'))

const statusTone = computed(() => {
  if (['success', 'completed'].includes(normalizedStatus.value)) return 'success'
  if (['failed', 'cancelled'].includes(normalizedStatus.value)) return 'failed'
  if (['pending_clarification', 'clarification', 'pending_approval'].includes(normalizedStatus.value)) return 'clarification'
  return 'running'
})

/* ── Pipeline ── */
const pipelineSummary = computed(() => {
  const stages = pipelineStore.pipelineStages
  const done = stages.filter(s => s.state === 'complete').length
  const running = stages.find(s => s.state === 'running')
  if (running) return `${done}/${stages.length} · ${running.name} 进行中`
  if (done === stages.length) return '全部完成'
  return `${done}/${stages.length} 已完成`
})

/* ── Agents ── */
const traceAgents = computed(() => fetchedTrace.value?.agents || [])
const participatingAgents = computed(() => agentsList.value.filter(a => traceAgents.value.includes(a.agent_id)))
function isAgentActive(id) { return !!fetchedTrace.value?.messages?.some(m => m.source_agent === id || m.target_agent === id) }
const handoffEdges = computed(() => (fetchedTrace.value?.handoff_edges || [])
  .map((edge) => ({
    ...edge,
    source: edge.source || edge.source_agent,
    target: edge.target || edge.target_agent,
  }))
  .filter((edge) => edge.source && edge.target && edge.source !== edge.target))

/* ── Experience cases ── */
const filteredCases = computed(() =>
  experienceCases.value.filter(c => {
    const currRoot = (decisionReport.value?.problem_diagnosis?.root_cause || '').toLowerCase()
    const caseRoot = (c.root_cause || '').toLowerCase()
    return !currRoot || caseRoot.includes(currRoot) || currRoot.includes(caseRoot)
  }).slice(0, 4)
)

/* ── Actions ── */
async function handleApprove() {
  const id = currentIntentId.value; if (!id) return
  try { await api.approveIntent(id); await intentStore.refreshIntent(id) } catch (_) {}
}
async function handleRollback() {
  const id = currentIntentId.value; if (!id) return
  if (!confirm('确认回滚此意图？')) return
  try { await api.rollbackIntent(id); await intentStore.refreshIntent(id) } catch (_) {}
}

/* ── KPI mini cards ── */
const conflictCount = computed(() => {
  const c = decisionReport.value?.policy_conflict; if (!c) return '—'
  const sev = c.severity_counts || {}
  return (sev.critical || 0) + (sev.high || 0) + (sev.medium || 0) + (sev.low || 0) || '—'
})
const verdictText = computed(() => {
  const s = safetyReport.value; if (!Object.keys(s).length) return '—'
  const d = String(s.overall_decision || '').toLowerCase()
  if (d === 'approved' || d === 'passed') return '✓'; if (d === 'blocked' || d === 'rejected') return '✗'; return '…'
})
const verdictClass = computed(() => {
  const d = String(safetyReport.value?.overall_decision || '').toLowerCase()
  if (d === 'approved' || d === 'passed') return 'good'; if (d === 'blocked' || d === 'rejected') return 'bad'; return ''
})
const achievedText = computed(() => {
  const v = decisionReport.value?.verification_result || {}
  if (v.intent_achieved === true || v.passed === true) return '已达成'; if (v.intent_achieved === false) return '未达成'; return ''
})
const achievedClass = computed(() => { const t = achievedText.value; if (t === '已达成') return 'good'; if (t === '未达成') return 'bad'; return '' })

/* ── Tags ── */
const intentTags = computed(() => {
  const u = decisionReport.value?.intent_understanding || {}
  return [{ label: '业务对象', value: u.business_object || u.intent_type || '-' },{ label: '源', value: u.source_ip || '-' },{ label: '目的', value: u.destination_ip || '-' },{ label: '动作', value: u.action_type || '-' }].filter(t => t.value !== '-')
})
const obsTags = computed(() => {
  const o = decisionReport.value?.network_observation || {}
  return [{ label: '吞吐', value: formatMbps(o.throughput_mbps) },{ label: '时延', value: formatMs(o.latency_ms) },{ label: '丢包', value: formatPercent(o.packet_loss_rate) },{ label: '队列', value: formatQueue({ queue_depth: o.queue_depth }) }].filter(t => t.value !== '-' && t.value !== '—')
})

/* ── Policy conflict ── */
const policyConflict = computed(() => decisionReport.value?.policy_conflict || {})
const showPolicyConflict = computed(() => !!Object.keys(policyConflict.value).length)
const conflictStatusText = computed(() => ({ blocked: '阻断', warning: '告警', clear: '通过' }[policyConflict.value.status] || '待检查'))
const conflictStatusClass = computed(() => policyConflict.value.status === 'blocked' ? 'bad' : policyConflict.value.status === 'warning' ? 'warn' : '')
const conflictSeverityMetrics = computed(() => {
  const c = policyConflict.value.severity_counts || {}
  return [{ key:'critical',label:'严重',value:c.critical??0,className:'critical'},{ key:'high',label:'高',value:c.high??0,className:'high'},{ key:'medium',label:'中',value:c.medium??0,className:'medium'},{ key:'low',label:'低',value:c.low??0,className:'low' }]
})
const conflictMatrix = computed(() => asArray(policyConflict.value.matrix || policyConflict.value.conflicts))
function matrixSeverityClass(s) { return { critical:'critical', high:'critical', medium:'medium' }[String(s||'').toLowerCase()] || 'low' }

/* ── Solutions ── */
const candidateSolutions = computed(() => decisionReport.value?.candidate_solutions || [])
const agentVotes = computed(() => decisionReport.value?.agent_votes || [])
function riskClass(score) { const s = Number(score); return s >= 70 ? 'risk-high' : s >= 40 ? 'risk-mid' : 'risk-low' }

/* ── Safety ── */
const safetyReport = computed(() => decisionReport.value?.safety_assurance || result.value?.safety_council_report || {})
const hasSafetyReport = computed(() => Object.keys(safetyReport.value).length > 0)
const safetyPillClass = computed(() => { const d = String(safetyReport.value?.overall_decision||'').toLowerCase(); if (d==='approved'||d==='passed') return 'good'; if (d==='blocked') return 'bad'; return '' })
const safetyReviews = computed(() => { const r = safetyReport.value.agent_reviews || safetyReport.value.votes || []; return r.map(normalizeSafetyReview).slice(0,5) })
const hardBlockersText = computed(() => { const b = safetyReport.value.hard_blockers; return Array.isArray(b) && b.length ? b.join('；') : '无' })

/* ── Verification ── */
/** 未执行任何变更的意图（冲突拦截/审批超时等）—— 无需展示 KPI 对比 */
const isSkippedIntent = computed(() => {
  const s = activeIntent.value?.status
  if (s === 'failed' || s === 'cancelled' || s === 'pending_approval') return true
  const fp = (result.value?.final_action || decisionReport.value?.final_action || {})
  if (fp.delivery_status === 'SKIPPED') return true
  return false
})
const skippedReason = computed(() => {
  const s = activeIntent.value?.status
  if (s === 'failed') return '策略冲突检测拦截，未执行变更，无需 KPI 验证。'
  if (s === 'cancelled') return '意图已取消，未执行变更。'
  if (s === 'pending_approval') return '等待审批中，尚未执行变更。'
  return '未执行变更，无 KPI 对比数据。'
})

const verificationRadarOption = computed(() => {
  const v = decisionReport.value?.verification_result || {}
  const before = v.before_kpi || v.pre_execution_verification?.kpi || {}
  const after = v.after_kpi || v.kpi || {}
  if (!Object.keys(before).length && !Object.keys(after).length) return null
  const beforeScaled = scaleKpiForRadar(before)
  const afterScaled = scaleKpiForRadar(after)
  return toRadarOption(beforeScaled, afterScaled, RADAR_KPI_DIMENSIONS)
})

/* ── Self-Healing ── */
const beforePath = computed(() => firstText(result.value?.comparison?.self_heal_before_path_text, result.value?.comparison?.before_path_text) || '')
const afterPath = computed(() => firstText(result.value?.comparison?.after_path_text, result.value?.comparison?.self_heal_after_path_text) || '')
const bottleneckLink = computed(() => firstText(result.value?.problem_diagnosis?.bottleneck_link, decisionReport.value?.network_observation?.bottleneck_link) || '')
const healingActions = computed(() => formatActionList(result.value?.final_action?.selected_actions) || result.value?.final_action?.action || '')
/** 自愈雷达图：仅当存在 comparison 数据（含路径切换）时才渲染。
 *  与 verificationRadarOption 互斥 — 非自愈意图不展示此图表。 */
const hasHealingComparison = computed(() => {
  const comp = result.value?.comparison || {}
  return comp.switched_to_backup === true
      || (typeof comp.before_kpi?.latency_ms === 'number'
          && typeof comp.after_kpi?.latency_ms === 'number'
          && Math.abs((comp.before_kpi.latency_ms || 0) - (comp.after_kpi.latency_ms || 0)) > 10)
})
const healingRadarOption = computed(() => {
  if (!hasHealingComparison.value) return null
  const r = result.value
  const comp = r.comparison || {}
  const before = comp.before_kpi || {}
  const after = comp.after_kpi || {}
  if (!Object.keys(before).length && !Object.keys(after).length) return null
  const beforeScaled = scaleKpiForRadar(before)
  const afterScaled = scaleKpiForRadar(after)
  return toRadarOption(beforeScaled, afterScaled, RADAR_KPI_DIMENSIONS)
})
const confidenceOption = computed(() => {
  const diag = decisionReport.value?.problem_diagnosis || result.value?.root_cause_report || {}
  const val = diag.confidence ?? diag.score; if (val == null) return null
  return toGaugeOption({ label:'诊断置信度', value:val, rawValue:Number(val)||0 })
})

/* ── Policy results ── */
const pp = computed(() => asArray(result.value?.policy_results)[0] || {})
const policyName = computed(() => firstText(pp.value.policy_name, result.value?.final_action?.policy_name))
const businessAction = computed(() => firstText(decisionReport.value?.intent_understanding?.action_type, result.value?.intent_type))
const affectedDevices = computed(() => firstText(pp.value.affected_devices, result.value?.final_action?.affected_devices))
const tableEntryCount = computed(() => firstText(pp.value.flow_entry_count, pp.value.entry_count))

/* ── Watcher ── */
const watcherState = computed(() => telemetryStore.watcherStatus?.state || 'IDLE')
const activeIncidentId = computed(() => telemetryStore.watcherStatus?.active_incident_id || '')
const cooldownText = computed(() => formatSeconds(telemetryStore.watcherStatus?.cooldown_remaining_sec) || '-')

/* ── A2A ── */
const a2aTimeline = computed(() => {
  const evidence = result.value?.a2a_runtime_evidence || {}
  return (evidence.messages || result.value?.a2a_timeline || []).filter(m => belongsToCurrentIntent(m) && m.message_type !== 'heartbeat')
})

const participantSummary = computed(() => {
  const agentCount = participatingAgents.value.length || traceAgents.value.length
  const handoffCount = handoffEdges.value.length
  if (!agentCount && !handoffCount) return '等待智能体协作证据'
  return `${agentCount} 个智能体参与，完成 ${handoffCount} 次任务交接`
})

const totalStageDuration = computed(() => pipelineStore.pipelineStages.reduce((total, stage) => {
  const duration = Number(stage.duration_ms ?? stage.duration ?? 0)
  return total + (Number.isFinite(duration) ? duration : 0)
}, 0))

const overviewMetrics = computed(() => {
  const stages = pipelineStore.pipelineStages
  const completed = stages.filter((stage) => stage.state === 'complete').length
  const participants = participatingAgents.value.length || traceAgents.value.length
  const duration = totalStageDuration.value
  return [
    { label: '流水线进度', value: `${completed}/${stages.length || 6}`, hint: pipelineSummary.value, tone: 'blue' },
    { label: '参与智能体', value: participants || '—', hint: participants ? '协同执行单元' : '等待协作证据', tone: 'violet' },
    { label: '任务交接', value: handoffEdges.value.length || '—', hint: 'A2A handoff', tone: 'cyan' },
    { label: '累计耗时', value: duration ? formatMs(duration) : '—', hint: '已返回阶段耗时', tone: 'neutral' },
    { label: '策略冲突', value: conflictCount.value, hint: showPolicyConflict.value ? conflictStatusText.value : '暂无冲突报告', tone: Number(conflictCount.value) > 0 ? 'red' : 'green' },
    { label: '闭环验证', value: achievedText.value || '待验证', hint: statusLabel.value, tone: achievedText.value === '已达成' ? 'green' : achievedText.value === '未达成' ? 'red' : 'amber' },
  ]
})

const hasExecutionOutcome = computed(() => {
  const terminal = ['success', 'completed', 'failed', 'cancelled'].includes(normalizedStatus.value)
  return terminal
    || Boolean(policyName.value || beforePath.value || afterPath.value || confidenceOption.value || achievedText.value)
})

const hasTechnicalDetails = computed(() => Boolean(
  intentJobs.value.length
  || a2aTimeline.value.length
  || filteredCases.value.length
  || Object.keys(telemetryStore.watcherStatus || {}).length
))

const technicalEvidenceCount = computed(() => (
  intentJobs.value.length + a2aTimeline.value.length + filteredCases.value.length
))
</script>

<style scoped>
.agent-page {
  --ink: #142033;
  --muted: #68778d;
  --line: rgba(122, 153, 184, .22);
  --line-strong: rgba(71, 154, 211, .32);
  --surface: rgba(255, 255, 255, .78);
  --surface-soft: rgba(246, 250, 253, .72);
  --blue: #1689ca;
  --cyan: #32b6d8;
  --green: #159b75;
  --amber: #c88a21;
  --red: #cf5362;
  width: 100%;
  height: 100%;
  min-height: 0;
  box-sizing: border-box;
  padding: 24px clamp(18px, 2.2vw, 36px) 52px;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  color: var(--ink);
}

.agent-page > template,
.agent-page > .empty-state { width:100%; }
.empty-state { display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:70vh; gap:10px; color:var(--muted); text-align:center; }
.empty-symbol { display:flex; align-items:center; gap:6px; height:38px; }
.empty-symbol span { width:7px; height:7px; border-radius:50%; background:var(--cyan); box-shadow:0 0 12px rgba(50,182,216,.35); animation:emptyPulse 1.2s ease-in-out infinite alternate; }
.empty-symbol span:nth-child(2) { animation-delay:.18s; background:#64748b; }
.empty-symbol span:nth-child(3) { animation-delay:.36s; background:var(--green); }
.empty-state h2 { margin:0; font-size:1.15rem; color:var(--ink); }
.empty-state p { margin:0; font-size:.8rem; }
.empty-btn,.action-btn { border:0; cursor:pointer; font-weight:800; }
.empty-btn { margin-top:8px; padding:9px 20px; border-radius:6px; color:#fff; background:var(--blue); box-shadow:0 8px 20px rgba(22,137,202,.15); }

.page-head { display:flex; align-items:flex-end; justify-content:space-between; gap:28px; max-width:1680px; margin:0 auto 16px; padding:0 2px 15px; border-bottom:1px solid var(--line); }
.head-copy { min-width:0; }
.head-copy p,.section-kicker { margin:0; color:#75869c; font:800 .63rem/1.2 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; letter-spacing:1.45px; text-transform:uppercase; }
.head-copy h2 { margin:6px 0 0; color:#101b2d; font-size:clamp(1.3rem,1.7vw,1.65rem); line-height:1.15; }
.intent-summary { display:block; max-width:min(820px,65vw); margin-top:9px; overflow:hidden; color:#425269; font-size:.82rem; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
.intent-id { display:block; margin-top:4px; color:#94a3b8; font:700 .65rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
.head-right { display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-shrink:0; }
.execution-switcher { display:flex; align-items:center; gap:9px; height:38px; padding:0 5px 0 12px; border:1px solid var(--line); border-radius:6px; background:rgba(255,255,255,.82); box-shadow:0 6px 18px rgba(38,65,91,.04); }
.execution-switcher > span { color:#64748b; font-size:.68rem; font-weight:800; white-space:nowrap; }
.execution-switcher select { width:min(300px,28vw); height:30px; padding:0 26px 0 10px; border:0; border-left:1px solid var(--line); outline:0; background:transparent; color:var(--ink); font-size:.72rem; font-weight:750; cursor:pointer; text-overflow:ellipsis; }
.status-pill,.action-btn { padding:8px 12px; border-radius:6px; font-size:.7rem; white-space:nowrap; }
.status-pill { color:#075985; background:#e0f2fe; font-weight:850; }
.status-pill.success { color:#047857; background:#dcfce7; }
.status-pill.failed { color:#b42318; background:#fee4e2; }
.status-pill.clarification { color:#a15c07; background:#fff3d6; }
.action-btn { color:#fff; background:var(--blue); }
.action-btn.rollback { color:#7c2d12; background:#ffedd5; }
.action-btn:hover { filter:brightness(.97); }

.sync-strip { display:flex; align-items:center; gap:9px; max-width:1680px; min-height:34px; margin:0 auto 12px; padding:0 12px; border-left:2px solid var(--cyan); color:#475569; background:rgba(240,249,255,.68); font-size:.72rem; font-weight:700; }
.sync-strip i { width:7px; height:7px; border-radius:50%; background:var(--cyan); animation:syncPulse .9s ease-in-out infinite alternate; }
.sync-strip.warning { border-color:#d49a2a; background:rgba(255,251,235,.78); }
.sync-strip.warning i { background:#d49a2a; animation:none; }

.overview-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); max-width:1680px; margin:0 auto 16px; border-block:1px solid var(--line); background:rgba(255,255,255,.5); }
.overview-metric { position:relative; min-width:0; padding:14px 16px 13px; border-right:1px solid var(--line); }
.overview-metric:last-child { border-right:0; }
.overview-metric::before { content:''; position:absolute; top:-1px; left:16px; width:24px; height:2px; background:#94a3b8; }
.overview-metric.blue::before { background:var(--cyan); }
.overview-metric.violet::before { background:#818cf8; }
.overview-metric.cyan::before { background:#22b8cf; }
.overview-metric.green::before { background:var(--green); }
.overview-metric.red::before { background:var(--red); }
.overview-metric.amber::before { background:var(--amber); }
.overview-metric span { display:block; color:#718096; font-size:.65rem; font-weight:800; }
.overview-metric strong { display:block; margin-top:6px; overflow:hidden; color:#152238; font-size:1.12rem; line-height:1.1; text-overflow:ellipsis; white-space:nowrap; }
.overview-metric small { display:block; margin-top:5px; overflow:hidden; color:#94a3b8; font-size:.61rem; font-weight:650; text-overflow:ellipsis; white-space:nowrap; }

.content-grid { display:grid; grid-template-columns:repeat(12,minmax(0,1fr)); gap:16px; max-width:1680px; margin:0 auto; padding-bottom:20px; }
.full-span { grid-column:1 / -1; }
.main-card,.technical-drawer { border:1px solid var(--line); border-radius:7px; background:var(--surface); box-shadow:0 16px 38px rgba(30,64,96,.055); backdrop-filter:blur(10px); }
.main-card { position:relative; min-width:0; padding:20px; overflow:hidden; }
.main-card::before { content:''; position:absolute; top:0; left:20px; width:42px; height:2px; background:#94a3b8; }
.card-pipeline::before { background:linear-gradient(90deg,var(--cyan),#6f8df5); }
.card-decision::before { background:var(--green); }
.card-results::before { background:var(--amber); }
.section-title { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:14px; }
.section-title h3 { margin:4px 0 0; color:#152238; font-size:.96rem; }
.section-title em { max-width:48%; overflow:hidden; color:#64748b; font-style:normal; font-size:.68rem; font-weight:750; text-overflow:ellipsis; white-space:nowrap; }
.section-title em.good { color:#047857; }
.section-title em.bad { color:#b42318; }

.collaboration-band { margin-top:18px; padding:16px 2px 0; border-top:1px solid var(--line); }
.collaboration-copy { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:end; gap:3px 12px; min-width:0; }
.collaboration-copy span { grid-column:1 / -1; color:#1689ca; font:800 .62rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; letter-spacing:1px; text-transform:uppercase; }
.collaboration-copy strong { min-width:0; font-size:.8rem; }
.collaboration-copy small { justify-self:end; min-width:0; color:#718096; font-size:.66rem; text-align:right; }
.agent-list { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:7px; min-width:0; margin-top:12px; }

.decision-layout { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(320px,.72fr); gap:20px; }
.decision-main,.decision-aside { min-width:0; }
.decision-aside { padding-left:20px; border-left:1px solid var(--line); }
.kpi-mini-row { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border:1px solid #e3ebf3; border-radius:7px; overflow:hidden; }
.kpi-mini { min-width:0; padding:11px 12px; border-right:1px solid #e3ebf3; }
.kpi-mini:last-child { border-right:0; }
.kpi-mini small { display:block; color:#718096; font-size:.62rem; font-weight:750; }
.kpi-mini b { display:block; margin-top:5px; color:#172033; font-size:1rem; }
.kpi-mini.good b { color:#047857; }
.kpi-mini.bad b { color:#b42318; }
.chart-block { margin-top:12px; }
.result-notice { margin-top:12px; padding:12px; border-left:2px solid #d49a2a; color:#7c4a03; background:#fffbeb; font-size:.72rem; font-weight:700; }
.insight-block h4,.result-panel h4,.technical-block h4 { margin:0 0 10px; color:#334155; font-size:.75rem; }
.tag-row { display:flex; flex-wrap:wrap; gap:6px; }
.info-tag { display:flex; align-items:center; gap:7px; padding:6px 8px; border-bottom:1px solid #e5edf5; }
.info-tag small { color:#718096; font-size:.61rem; font-weight:750; }
.info-tag strong { color:#24324a; font-size:.68rem; }

.fold-block { margin-top:10px; border-top:1px solid #e5edf5; }
.fold-block summary,.technical-drawer summary { list-style:none; cursor:pointer; user-select:none; }
.fold-block summary::-webkit-details-marker,.technical-drawer summary::-webkit-details-marker { display:none; }
.fold-block summary { display:flex; align-items:center; gap:8px; padding:11px 0 6px; color:#334155; font-size:.72rem; font-weight:800; }
.fold-block summary::before { content:'+'; width:16px; color:#1689ca; font-size:.9rem; }
.fold-block[open] summary::before { content:'−'; }
.fold-body { padding:6px 0 4px; }
.section-fold { margin-top:14px; }
.mini-pill { margin-left:auto; padding:3px 7px; border-radius:6px; color:#075985; background:#e0f2fe; font-size:.6rem; font-weight:850; }
.mini-pill.good { color:#047857; background:#dcfce7; }
.mini-pill.bad { color:#b42318; background:#fee4e2; }
.mini-pill.warn { color:#a15c07; background:#fff3d6; }

.conflict-metrics { display:grid; grid-template-columns:repeat(4,1fr); margin-bottom:8px; border:1px solid #e5edf5; border-radius:6px; overflow:hidden; }
.conflict-metric { padding:7px; border-right:1px solid #e5edf5; text-align:center; }
.conflict-metric:last-child { border-right:0; }
.conflict-metric b,.conflict-metric small { display:block; }
.conflict-metric b { font-size:.88rem; }
.conflict-metric small { margin-top:2px; color:#718096; font-size:.58rem; }
.conflict-metric.critical b { color:#b42318; }
.conflict-metric.high b { color:#c2410c; }
.conflict-metric.medium b { color:#a15c07; }
.conflict-matrix-simple { display:flex; flex-direction:column; }
.matrix-row { display:grid; grid-template-columns:minmax(0,1fr) 24px minmax(0,1fr) auto; align-items:center; gap:6px; padding:7px 4px; border-bottom:1px solid #edf2f7; }
.matrix-label { overflow:hidden; font-size:.65rem; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
.matrix-arrow { color:#94a3b8; font-size:.58rem; text-align:center; }
.matrix-severity,.solution-row span { padding:2px 6px; border-radius:5px; font-size:.58rem; font-weight:800; }
.matrix-severity.critical,.risk-high { color:#b42318; background:#fee4e2; }
.matrix-severity.medium,.risk-mid { color:#a15c07; background:#fff3d6; }
.matrix-severity.low,.risk-low { color:#047857; background:#dcfce7; }

.solution-row,.vote-row,.trace-row,.jobs-row { display:grid; align-items:center; gap:8px; padding:7px 2px; border-bottom:1px solid #edf2f7; }
.solution-row { grid-template-columns:150px auto minmax(0,1fr); }
.vote-row { grid-template-columns:150px auto minmax(0,1fr); }
.trace-row { grid-template-columns:170px auto minmax(0,1fr); }
.solution-row b,.vote-row b,.trace-row b { overflow:hidden; font-size:.67rem; text-overflow:ellipsis; white-space:nowrap; }
.solution-row small,.vote-row small,.trace-row small { overflow:hidden; color:#718096; font-size:.63rem; text-overflow:ellipsis; white-space:nowrap; }
.vote-row em,.trace-row span { color:#075985; font-size:.59rem; font-style:normal; font-weight:800; }

.result-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-block:1px solid var(--line); }
.result-panel { min-width:0; min-height:150px; padding:14px; border-right:1px solid #e3ebf3; }
.result-panel:last-child { border-right:0; }
.confidence-panel { display:flex; align-items:center; justify-content:center; }
.compact-dl { display:grid; grid-template-columns:82px minmax(0,1fr); gap:7px 9px; margin:0; }
.compact-dl dt { color:#718096; font-size:.64rem; font-weight:750; }
.compact-dl dd { min-width:0; margin:0; overflow:hidden; color:#24324a; font-size:.68rem; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
.path-strip { display:block; overflow:hidden; padding:2px 6px; color:#475569; background:#f1f5f9; font:650 .62rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
.path-strip.after { color:#047857; background:#ecfdf3; }

.technical-drawer { overflow:hidden; background:rgba(255,255,255,.62); }
.technical-drawer > summary { display:flex; align-items:center; justify-content:space-between; padding:13px 16px; }
.technical-drawer > summary span { display:flex; align-items:center; gap:10px; }
.technical-drawer > summary b { font-size:.76rem; }
.technical-drawer > summary small { color:#718096; font-size:.62rem; }
.technical-drawer > summary em { color:#1689ca; font-size:.62rem; font-style:normal; font-weight:800; }
.technical-drawer[open] > summary { border-bottom:1px solid #e5edf5; }
.technical-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; padding:16px; }
.technical-block { min-width:0; }
.jobs-row { grid-template-columns:160px minmax(0,1fr) auto; }
.jobs-id { overflow:hidden; font:700 .62rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
.jobs-stage { color:#718096; font-size:.64rem; }
.jobs-status { padding:2px 6px; border-radius:5px; color:#075985; background:#e0f2fe; font-size:.58rem; font-weight:800; }
.jobs-status.success,.jobs-status.completed { color:#047857; background:#dcfce7; }
.jobs-status.failed { color:#b42318; background:#fee4e2; }

@keyframes emptyPulse { to { transform:translateY(-7px); opacity:.45; } }
@keyframes syncPulse { to { transform:scale(1.45); opacity:.4; } }

@media (max-width:1280px) {
  .overview-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
  .overview-metric:nth-child(3) { border-right:0; }
  .overview-metric:nth-child(-n+3) { border-bottom:1px solid #e4ebf3; }
  .decision-layout { grid-template-columns:1fr; }
  .decision-aside { padding:14px 0 0; border-top:1px solid #e5edf5; border-left:0; }
}

@media (max-width:880px) {
  .agent-page { padding:14px 10px 32px; }
  .page-head { flex-direction:column; }
  .head-right { width:100%; flex-wrap:wrap; justify-content:flex-start; }
  .execution-switcher { max-width:100%; }
  .execution-switcher select { width:55vw; }
  .overview-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .overview-metric:nth-child(2n) { border-right:0; }
  .overview-metric:nth-child(3) { border-right:1px solid #e4ebf3; }
  .overview-metric:nth-child(-n+4) { border-bottom:1px solid #e4ebf3; }
  .collaboration-band { grid-template-columns:1fr; }
  .collaboration-copy { grid-template-columns:1fr; align-items:start; }
  .collaboration-copy span { grid-column:auto; }
  .collaboration-copy small { justify-self:start; text-align:left; }
  .kpi-mini-row { grid-template-columns:repeat(2,1fr); }
  .kpi-mini:nth-child(2) { border-right:0; }
  .kpi-mini:nth-child(-n+2) { border-bottom:1px solid #e3ebf3; }
  .result-grid,.technical-grid { grid-template-columns:1fr; }
  .result-panel { border-right:0; border-bottom:1px solid #e3ebf3; }
  .result-panel:last-child { border-bottom:0; }
  .section-title { align-items:flex-start; }
  .section-title em { max-width:42%; }
}

@media (max-width:560px) {
  .head-right { display:grid; grid-template-columns:1fr auto; }
  .execution-switcher { grid-column:1 / -1; width:100%; box-sizing:border-box; }
  .execution-switcher select { width:100%; min-width:0; }
  .overview-grid { grid-template-columns:1fr 1fr; }
  .main-card { padding:16px 12px; }
  .section-title { flex-direction:column; gap:5px; }
  .section-title em { max-width:100%; }
  .solution-row,.vote-row,.trace-row,.jobs-row { grid-template-columns:1fr; gap:3px; }
  .compact-dl { grid-template-columns:76px minmax(0,1fr); }
  .technical-grid { padding:12px; }
}
</style>
