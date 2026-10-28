<template>
  <section class="run-page">
    <div class="panel run-main">
      <div class="run-head">
        <div>
          <p>Agent Execution</p>
          <h2>{{ intent.activeIntent?.title || '等待意图' }}</h2>
        </div>
        <span :class="intent.activeIntent?.statusClass">{{ intent.activeIntent?.status || 'standby' }}</span>
      </div>

      <AgentPipeline
        :stages="pipeline.pipelineStages"
        :logs="pipeline.agentLogs"
        :selected-stage="pipeline.selectedStage"
        @select-stage="pipeline.selectStage"
      />
    </div>

    <aside class="panel run-side">
      <div class="side-card">
        <span>当前意图</span>
        <p>{{ intent.activeIntent?.content || intent.intentText }}</p>
      </div>
      <div class="side-card">
        <span>执行结果</span>
        <strong>{{ resultText }}</strong>
      </div>

      <div class="side-card report-card">
        <span>Agent Decision Report</span>
        <strong>{{ decisionReport?.summary || '等待智能体生成决策报告。' }}</strong>
        <p v-if="decisionReport?.final_conclusion">{{ decisionReport.final_conclusion }}</p>
        <div v-if="evidenceTags.length" class="evidence-tags">
          <i v-for="tag in evidenceTags" :key="tag">{{ tag }}</i>
        </div>
      </div>

      <div class="side-card a2a-evidence-card">
        <span>A2A Runtime Evidence</span>
        <strong>{{ runtimeEvidence.runtime_trace_ready ? '运行时 A2A 证据链已记录。' : '等待运行时 A2A 消息。' }}</strong>
        <dl>
          <template v-for="row in runtimeRows" :key="row.label">
            <dt>{{ row.label }}</dt>
            <dd>{{ row.value || '-' }}</dd>
          </template>
        </dl>
        <div v-if="runtimeAgents.length" class="evidence-tags">
          <i v-for="agent in runtimeAgents" :key="agent">{{ agent }}</i>
        </div>
      </div>

      <div v-if="decisionReport" class="side-card decision-card">
        <span>智能体决策报告</span>
        <div class="report-block">
          <h3>意图理解</h3>
          <dl>
            <template v-for="row in intentRows" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value || '-' }}</dd>
            </template>
          </dl>
        </div>

        <div class="report-block">
          <h3>网络观察</h3>
          <dl>
            <template v-for="row in observationRows" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value || '-' }}</dd>
            </template>
          </dl>
        </div>

        <div v-if="showPolicyConflict" class="report-block conflict-block">
          <div class="conflict-head">
            <h3>策略冲突可视化</h3>
            <em :class="['conflict-status', conflictStatusClass]">{{ conflictStatusText }}</em>
          </div>
          <p>{{ policyConflict.summary || policyConflict.explanation || '等待策略冲突检查结果。' }}</p>

          <div class="conflict-metrics">
            <span
              v-for="metric in conflictSeverityMetrics"
              :key="metric.key"
              :class="['conflict-metric', metric.className]"
            >
              <b>{{ metric.value }}</b>
              <small>{{ metric.label }}</small>
            </span>
          </div>

          <div v-if="conflictGraphNodes.length" class="conflict-graph">
            <div class="conflict-node-row">
              <span
                v-for="node in conflictGraphNodes"
                :key="node.id"
                :class="['conflict-node', conflictNodeClass(node)]"
              >
                <b>{{ node.label }}</b>
                <small>{{ node.kind }}</small>
              </span>
            </div>
            <div v-if="conflictGraphEdges.length" class="conflict-edge-list">
              <article
                v-for="edge in conflictGraphEdges"
                :key="edge.id"
                :class="['conflict-edge', severityClass(edge.severity)]"
              >
                <div>
                  <b>{{ edge.source_label || edge.source }} -> {{ edge.target_label || edge.target }}</b>
                  <em>{{ edge.label || edge.conflict_type }}</em>
                </div>
                <small>{{ edge.evidence || '无补充证据。' }}</small>
              </article>
            </div>
          </div>

          <div v-if="conflictMatrix.length" class="conflict-matrix">
            <article
              v-for="row in conflictMatrix"
              :key="`${row.kind}-${row.conflict_type}-${row.target}`"
              class="conflict-row"
            >
              <div>
                <b>{{ row.conflict_type }}</b>
                <em :class="severityClass(row.severity)">{{ row.severity }}</em>
              </div>
              <small>对象：{{ row.target_kind || '-' }} / {{ row.target || '-' }}</small>
              <p>{{ row.evidence || '-' }}</p>
              <small v-if="row.resource">资源：{{ row.resource }}</small>
              <small v-if="row.suggestion">建议：{{ row.suggestion }}</small>
            </article>
          </div>

          <div v-if="policyConflict.suggestions?.length" class="evidence-list">
            <i v-for="suggestion in policyConflict.suggestions.slice(0, 3)" :key="suggestion">
              {{ suggestion }}
            </i>
          </div>
        </div>

        <div class="report-block diagnosis-block">
          <h3>问题诊断</h3>
          <strong>{{ diagnosis.root_cause || '未发现明确瓶颈。' }}</strong>
          <p v-if="diagnosis.bottleneck_link">
            瓶颈链路：{{ diagnosis.bottleneck_link }}；瓶颈设备：{{ diagnosis.bottleneck_device || '-' }}
          </p>
          <p v-if="diagnosis.next_probe?.action">
            下一步探针：{{ diagnosis.next_probe.action }}；{{ diagnosis.next_probe.reason }}
          </p>
          <div v-if="diagnosis.evidence?.length" class="evidence-list">
            <i v-for="item in diagnosis.evidence.slice(0, 4)" :key="item">{{ item }}</i>
          </div>
          <div v-if="strongSimilarCases.length" class="evidence-list">
            <i
              v-for="item in strongSimilarCases"
              :key="item.case_id"
            >
              {{ item.case_id }} 相似度 {{ item.similarity }} / success={{ item.success }}
            </i>
          </div>
          <p v-else-if="weakSimilarCaseCount" class="muted-note">
            已过滤 {{ weakSimilarCaseCount }} 个低相似历史案例，未作为强证据使用。
          </p>
        </div>

        <div v-if="candidateSolutions.length" class="report-block">
          <h3>候选方案</h3>
          <article
            v-for="item in candidateSolutions"
            :key="item.id"
            :class="['decision-option', { selected: item.selected, recommended: item.recommended }]"
          >
            <b>{{ item.title }}</b>
            <em>风险 {{ item.risk_score ?? '-' }}</em>
            <p>{{ item.reason }}</p>
          </article>
        </div>

        <div v-if="agentVotes.length" class="report-block">
          <h3>智能体投票</h3>
          <article v-for="vote in agentVotes" :key="vote.agent_id" class="vote-item">
            <b>{{ vote.agent_id }}</b>
            <em :class="riskClass(vote.risk_score)">risk {{ vote.risk_score }}</em>
            <p>{{ vote.vote }}</p>
            <small>{{ vote.reason }}</small>
          </article>
        </div>

        <div class="report-block">
          <h3>最终动作</h3>
          <p>{{ finalAction.rationale || '-' }}</p>
          <div class="evidence-list">
            <i v-for="action in finalAction.selected_actions || []" :key="action">{{ action }}</i>
          </div>
        </div>

        <div v-if="hasSafetyReport" class="report-block safety-block">
          <h3>安全评审委员会</h3>
          <strong>{{ safetyReport.overall_decision || '-' }} · {{ safetyReport.risk_level || '-' }} · risk {{ safetyReport.overall_risk_score ?? '-' }}</strong>
          <p>{{ safetyReport.final_rationale || '-' }}</p>
          <p v-if="safetyWarnings.length" class="warning-summary">
            带警告放行：{{ safetyWarnings.join('；') }}
          </p>
          <article
            v-for="review in safetyReviews"
            :key="review.agent_id"
            class="vote-item"
          >
            <b>{{ review.agent_id }}</b>
            <em :class="riskClass(review.risk_score)">risk {{ review.risk_score }}</em>
            <p>{{ review.decision }}</p>
            <small>{{ (review.evidence || []).slice(0, 2).join(' / ') }}</small>
          </article>
          <div v-if="safetyNegotiation.votes?.length" class="negotiation-box">
            <b>协商结论：{{ safetyNegotiation.final_consensus || '-' }}</b>
            <small>{{ safetyNegotiation.explanation || safetyNegotiation.proposal }}</small>
            <div class="negotiation-votes">
              <i v-for="vote in safetyNegotiation.votes" :key="vote.agent">
                {{ vote.agent }}: {{ vote.vote }}
              </i>
            </div>
          </div>
        </div>

        <div class="report-block">
          <h3>验证结果</h3>
          <dl>
            <template v-for="row in verificationRows" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd>{{ row.value || '-' }}</dd>
            </template>
          </dl>
          <p v-if="fallbackPlan.if_verification_failed">
            失败兜底：{{ fallbackPlan.if_verification_failed }}
          </p>
        </div>
      </div>

      <div class="side-card timeline-card">
        <span>A2A Message Timeline</span>
        <strong v-if="!a2aTimeline.length">暂无协作消息。</strong>
        <div v-else class="timeline-list">
          <article v-for="item in visibleTimeline" :key="item.id" class="timeline-item">
            <div class="timeline-head">
              <b>{{ item.source_agent }} → {{ item.target_agent }}</b>
              <em :class="item.status">{{ item.message_type }}</em>
            </div>
            <p>{{ item.output_summary || item.input_summary }}</p>
            <small>{{ item.method || '-' }} · {{ item.payload_schema || '-' }}</small>
            <small v-if="item.input_summary">输入：{{ item.input_summary }}</small>
            <small v-if="item.confidence !== null && item.confidence !== undefined">
              置信度：{{ Math.round(item.confidence * 100) }}%
            </small>
            <small v-if="item.risk_score !== null && item.risk_score !== undefined">
              风险分：{{ item.risk_score }}
            </small>
            <div v-if="item.evidence_refs?.length" class="timeline-evidence">
              <i v-for="ref in item.evidence_refs.slice(0, 3)" :key="ref">{{ ref }}</i>
            </div>
            <small v-if="item.failure_reason">失败原因：{{ item.failure_reason }}</small>
            <small v-if="item.retry_suggestion">建议：{{ item.retry_suggestion }}</small>
          </article>
        </div>
      </div>

      <form
        v-if="intent.activeClarificationQuestion"
        class="side-card clarification-card"
        @submit.prevent="submitClarification"
      >
        <span>需要澄清</span>
        <strong>{{ intent.activeClarificationQuestion }}</strong>
        <textarea
          v-model="intent.clarificationAnswer"
          placeholder="例如：是，视频业务使用 UDP 协议和 5004 端口，带宽目标为 10000Mbps。"
          :disabled="intent.isAnsweringClarification"
        ></textarea>
        <button
          type="submit"
          :disabled="intent.isAnsweringClarification || !intent.clarificationAnswer.trim()"
        >
          {{ intent.isAnsweringClarification ? '提交中...' : '提交澄清并继续' }}
        </button>
      </form>
    </aside>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useIntentStore } from '../../stores/intentStore'
import { usePipelineStore } from '../../stores/pipelineStore'
import AgentPipeline from '../../components/dashboard/AgentPipeline.vue'
import { formatMbps, formatMs, formatPercent, formatCounter, formatPlannerCritic, asArray, similarityScore, belongsToCurrentIntent, normalizeSafetyReview } from '../../composables/useFormatters'

const route = useRoute()
const intent = useIntentStore()
const pipeline = usePipelineStore()

const resultText = computed(() => {
  const s = intent.activeIntent?.statusClass
  if (s === 'success') return '策略已生效，KPI 验证通过。'
  if (s === 'failed') return '执行失败，请查看日志。'
  if (s === 'clarification') return '智能体正在等待你补充澄清信息。'
  return '智能体正在执行 STAV 闭环：Sense → Parse → Check → Plan → Act → Verify。'
})

const decisionReport = computed(() => intent.activeIntent?.result?.decision_report || null)
const currentIntentId = computed(() => intent.activeIntent?.id || '')
const runtimeEvidence = computed(() => (
  intent.activeIntent?.result?.a2a_runtime_evidence
  || decisionReport.value?.a2a_runtime_evidence
  || {}
))
const runtimeAgents = computed(() => (runtimeEvidence.value?.agents || []).slice(0, 10))
const rawA2ATimeline = computed(() => {
  const runtimeMessages = runtimeEvidence.value?.messages || []
  return runtimeMessages.length ? runtimeMessages : (intent.activeIntent?.result?.a2a_timeline || [])
})
const a2aTimeline = computed(() => rawA2ATimeline.value
  .filter((item) => belongsToCurrentIntent(item, currentIntentId.value))
  .filter((item) => String(item.message_type || '').toLowerCase() !== 'heartbeat'))
const visibleTimeline = computed(() => a2aTimeline.value.slice(0, 8))
const evidenceTags = computed(() => (decisionReport.value?.evidence_sources || []).slice(0, 6))
const understanding = computed(() => decisionReport.value?.intent_understanding || {})
const observation = computed(() => decisionReport.value?.network_observation || {})
const policyConflict = computed(() => normalizePolicyConflict(
  decisionReport.value?.policy_conflict
  || intent.activeIntent?.result?.conflict_report
  || {}
))
const showPolicyConflict = computed(() => decisionReport.value && Object.keys(policyConflict.value).length > 0)
const conflictGraphNodes = computed(() => asArray(policyConflict.value.conflict_graph?.nodes))
const conflictGraphEdges = computed(() => asArray(policyConflict.value.conflict_graph?.edges))
const conflictMatrix = computed(() => asArray(policyConflict.value.matrix))
const conflictSeverityMetrics = computed(() => {
  const counts = policyConflict.value.severity_counts || {}
  return [
    { key: 'HIGH', label: '高危', value: counts.HIGH || 0, className: 'severity-high' },
    { key: 'MEDIUM', label: '中危', value: counts.MEDIUM || 0, className: 'severity-medium' },
    { key: 'LOW', label: '低危', value: counts.LOW || 0, className: 'severity-low' },
    { key: 'INFO', label: '提示', value: counts.INFO || 0, className: 'severity-info' },
  ]
})
const conflictStatusText = computed(() => {
  if (policyConflict.value.status === 'blocked') return '阻断'
  if (policyConflict.value.status === 'warning') return '告警'
  if (policyConflict.value.status === 'clear') return '通过'
  return '待检查'
})
const conflictStatusClass = computed(() => {
  if (policyConflict.value.status === 'blocked') return 'status-blocked'
  if (policyConflict.value.status === 'warning') return 'status-warning'
  if (policyConflict.value.status === 'clear') return 'status-clear'
  return 'status-idle'
})
const diagnosis = computed(() => decisionReport.value?.problem_diagnosis || {})
const candidateSolutions = computed(() => decisionReport.value?.candidate_solutions || [])
const agentVotes = computed(() => decisionReport.value?.agent_votes || [])
const finalAction = computed(() => decisionReport.value?.final_action || {})
const safetyReport = computed(() => (
  decisionReport.value?.safety_assurance
  || intent.activeIntent?.result?.safety_council_report
  || {}
))
const hasSafetyReport = computed(() => Object.keys(safetyReport.value || {}).length > 0)
const safetyReviews = computed(() => (safetyReport.value?.agent_reviews || safetyReport.value?.votes || [])
  .map(normalizeSafetyReview)
  .slice(0, 5))
const safetyWarnings = computed(() => safetyReviews.value
  .filter((review) => {
    const status = String(review.decision || review.vote || review.status || '').toLowerCase()
    const text = [
      review.reason,
      ...asArray(review.evidence),
      ...asArray(review.warnings),
    ].filter(Boolean).join(' ')
    return status.includes('warning') || /warning|warn|警告|风险|warning/i.test(text)
  })
  .map((review) => `${review.agent_id || review.agent || 'reviewer'}：${review.reason || (review.evidence || [])[0] || review.decision || review.vote}`)
  .slice(0, 3))
const safetyNegotiation = computed(() => safetyReport.value?.negotiation || {})
const verificationResult = computed(() => decisionReport.value?.verification_result || {})
const fallbackPlan = computed(() => decisionReport.value?.fallback_plan || {})
const strongSimilarCases = computed(() => asArray(diagnosis.value?.experience_memory?.similar_cases)
  .filter((item) => similarityScore(item) >= 0.75)
  .slice(0, 3))
const weakSimilarCaseCount = computed(() => asArray(diagnosis.value?.experience_memory?.similar_cases)
  .filter((item) => similarityScore(item) < 0.75).length)

const runtimeRows = computed(() => [
  { label: '协议版本', value: runtimeEvidence.value.protocol_version },
  { label: 'Trace ID', value: runtimeEvidence.value.trace_id },
  { label: '关键消息', value: `${a2aTimeline.value.length} / ${runtimeEvidence.value.message_count || rawA2ATimeline.value.length || 0}` },
  { label: '消息类型', value: [...new Set(a2aTimeline.value.map((item) => item.message_type).filter(Boolean))].join(' / ') },
  { label: '协作边数', value: (runtimeEvidence.value.handoff_edges || []).length },
])

const intentRows = computed(() => [
  { label: '业务对象', value: understanding.value.business_object },
  { label: '源地址', value: understanding.value.source_ip },
  { label: '目的地址', value: understanding.value.destination_ip },
  { label: '协议/端口', value: [understanding.value.protocol, understanding.value.destination_port].filter(Boolean).join(' / ') },
  { label: '动作', value: understanding.value.action_type },
  { label: '带宽目标', value: formatMbps(understanding.value.bandwidth_target_mbps) },
  { label: '置信度', value: formatPercent(understanding.value.confidence_score) },
  { label: '交叉验证', value: formatPlannerCritic(understanding.value.planner_critic) },
])

const observationRows = computed(() => [
  { label: '当前路径', value: observation.value.current_path_text },
  { label: '自愈前路径', value: observation.value.pre_heal_path_text },
  { label: '自愈后路径', value: observation.value.post_heal_path_text },
  { label: '路径状态', value: observation.value.path_state },
  { label: '吞吐', value: formatMbps(observation.value.kpi?.throughput_mbps) },
  { label: '时延', value: formatMs(observation.value.kpi?.latency_ms) },
  { label: '丢包', value: formatPercent(observation.value.kpi?.packet_loss_rate) },
  { label: 'qdisc 队列', value: observation.value.qdisc?.queue_depth },
  { label: 'INT 最大队列', value: observation.value.int?.max_queue_occupancy },
  { label: 'P4 direct counter', value: formatCounter(observation.value.p4_direct_counter) },
])

const verificationRows = computed(() => {
  const before = verificationResult.value.before_kpi || {}
  const after = verificationResult.value.after_kpi || {}
  return [
    { label: '意图达成', value: verificationResult.value.intent_achieved === true ? '是' : verificationResult.value.intent_achieved === false ? '否' : '-' },
    { label: '验证通过', value: verificationResult.value.passed === true ? '通过' : verificationResult.value.passed === false ? '未通过' : '-' },
    { label: '切换备用路径', value: verificationResult.value.switched_to_backup === true ? '是' : verificationResult.value.switched_to_backup === false ? '否' : '-' },
    { label: '吞吐 (前→后)', value: `${formatMbps(before.throughput_mbps)} → ${formatMbps(after.throughput_mbps)}` },
    { label: '时延 (前→后)', value: `${formatMs(before.latency_ms)} → ${formatMs(after.latency_ms)}` },
    { label: '丢包 (前→后)', value: `${formatPercent(before.packet_loss_rate)} → ${formatPercent(after.packet_loss_rate)}` },
    { label: '队列 (前→后)', value: `${formatQueue(before)} → ${formatQueue(after)}` },
  ]
})

function normalizePolicyConflict(report) {
  if (!report || typeof report !== 'object') return {}
  const items = asArray(report.items || report.conflict_items)
  const warningItems = asArray(report.warning_items)
  const warningTexts = asArray(report.warnings)
  const status = report.status || (
    report.has_conflict ? 'blocked' : (warningItems.length || warningTexts.length ? 'warning' : 'clear')
  )
  const matrix = asArray(report.matrix)
  return {
    ...report,
    status,
    items,
    warning_items: warningItems,
    suggestions: asArray(report.suggestions),
    severity_counts: report.severity_counts || countConflictSeverities(items, warningItems),
    matrix: matrix.length ? matrix : buildLegacyConflictMatrix(items, warningItems),
    conflict_graph: report.conflict_graph || buildLegacyConflictGraph(report, items, warningItems, status),
  }
}

function buildLegacyConflictMatrix(items, warningItems) {
  return [
    ...items.map((item) => conflictRow(item, 'conflict')),
    ...warningItems.map((item) => conflictRow(item, 'warning')),
  ]
}

function conflictRow(item, kind) {
  return {
    kind,
    conflict_type: item.conflict_type || item.type || 'UNKNOWN',
    severity: String(item.severity || (kind === 'warning' ? 'LOW' : 'INFO')).toUpperCase(),
    target: conflictTarget(item),
    target_kind: conflictTargetKind(item),
    evidence: item.explanation || item.description || '',
    resource: conflictResource(item),
    suggestion: item.suggestion || asArray(item.suggestions)[0] || '',
  }
}

function buildLegacyConflictGraph(report, items, warningItems, status) {
  const current = {
    id: 'policy:current',
    label: report.policy_name || '当前策略',
    kind: 'current_policy',
    status,
    severity: report.severity,
  }
  const nodes = [current]
  const edges = []
  const seen = new Set([current.id])
  ;[
    ...items.map((item) => ({ item, kind: 'conflict' })),
    ...warningItems.map((item) => ({ item, kind: 'warning' })),
  ].forEach(({ item, kind }, index) => {
    const targetLabel = conflictTarget(item)
    const targetKind = conflictTargetKind(item)
    const targetId = `${targetKind}:${targetLabel}:${index}`
    if (!seen.has(targetId)) {
      seen.add(targetId)
      nodes.push({ id: targetId, label: targetLabel, kind: targetKind, severity: item.severity })
    }
    const type = item.conflict_type || item.type || 'UNKNOWN'
    const severity = String(item.severity || (kind === 'warning' ? 'LOW' : 'INFO')).toUpperCase()
    edges.push({
      id: `${current.id}->${targetId}`,
      source: current.id,
      target: targetId,
      source_label: current.label,
      target_label: targetLabel,
      kind,
      conflict_type: type,
      severity,
      label: `${type} / ${severity}`,
      evidence: item.explanation || item.description || '',
    })
  })
  return { nodes, edges }
}

function countConflictSeverities(items, warningItems) {
  const counts = { HIGH: 0, MEDIUM: 0, LOW: 0, INFO: 0 }
  ;[...items, ...warningItems].forEach((item) => {
    const severity = String(item.severity || 'INFO').toUpperCase()
    counts[severity in counts ? severity : 'INFO'] += 1
  })
  return counts
}

function conflictTarget(item) {
  if (item.existing_policy_name || item.existing_policy_id) return item.existing_policy_name || item.existing_policy_id
  if (item.link_id) return item.link_id
  if (Array.isArray(item.unavailable_devices) && item.unavailable_devices.length) return item.unavailable_devices.join(', ')
  if (item.resource_check_scope) return item.resource_check_scope
  return item.conflict_type || item.type || '检查项'
}

function conflictTargetKind(item) {
  if (item.existing_policy_name || item.existing_policy_id) return 'existing_policy'
  if (item.link_id) return 'link'
  if (Array.isArray(item.unavailable_devices) && item.unavailable_devices.length) return 'device'
  if (item.resource_check_scope) return 'resource'
  return 'check'
}

function conflictResource(item) {
  const parts = [
    ['requested_bandwidth', 'requested'],
    ['reserved_bandwidth', 'reserved'],
    ['available_bandwidth', 'available'],
    ['required_bandwidth', 'required'],
  ]
    .filter(([key]) => item[key] !== null && item[key] !== undefined)
    .map(([key, label]) => `${label}=${item[key]}`)
  return parts.join(', ')
}

function severityClass(value) {
  const severity = String(value || '').toUpperCase()
  if (severity === 'HIGH') return 'severity-high'
  if (severity === 'MEDIUM') return 'severity-medium'
  if (severity === 'LOW') return 'severity-low'
  return 'severity-info'
}

function conflictNodeClass(node) {
  if (node.kind === 'current_policy') return 'node-current'
  return severityClass(node.severity)
}

function riskClass(score) {
  if (score >= 60) return 'risk-high'
  if (score >= 35) return 'risk-mid'
  return 'risk-low'
}

// SSE-first progress tracking (polling as fallback via store internals)
let _cleanupSse = null

onMounted(() => {
  const routeIntentId = String(route.query.intent_id || '')
  if (routeIntentId && routeIntentId !== intent.selectedIntentId) {
    intent.selectIntent({ id: routeIntentId, title: '网络意图编排', status: 'running', statusClass: 'running' })
  }
  const intentId = intent.selectedIntentId
  if (!intentId) return

  // Use SSE as primary progress channel (falls back to polling internally)
  intent._subscribePipeline(intentId)
})

async function submitClarification() {
  const intentId = intent.activeIntent?.id
  await intent.answerClarification(intentId, intent.clarificationAnswer)
  // _subscribePipeline is called inside answerClarification
}

onUnmounted(() => {
  // Cleanup handled by intentStore._sseSubs lifecycle
})
</script>

<style scoped>
.run-page {
  display: grid;
  grid-template-columns: minmax(560px, 1fr) minmax(330px, 0.52fr);
  gap: 18px;
  height: 100%;
}

.panel {
  min-width: 0;
  min-height: 0;
  padding: 18px;
  border: 1px solid rgba(226, 232, 240, 0.88);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.66);
  box-shadow: 0 26px 60px rgba(15, 23, 42, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(14px);
}

.run-main {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}

.run-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

p,
h2 {
  margin: 0;
}

.run-head p {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.72rem;
  font-weight: 850;
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

h2 {
  margin-top: 4px;
  color: #0f172a;
  font-size: 1.18rem;
}

.run-head > span {
  min-width: 90px;
  height: 30px;
  display: inline-grid;
  place-items: center;
  border-radius: 999px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.86);
  font-size: 0.72rem;
  font-weight: 950;
}

.run-head > span.success {
  color: #047857;
  background: rgba(209, 250, 229, 0.84);
}

.run-head > span.clarification {
  color: #b45309;
  background: rgba(254, 243, 199, 0.9);
}

.run-side {
  display: grid;
  gap: 14px;
  align-content: start;
  overflow: auto;
}

.side-card {
  padding: 16px;
  border: 1px solid rgba(203, 213, 225, 0.78);
  border-radius: 15px;
  background: rgba(248, 250, 252, 0.68);
}

.side-card span {
  display: block;
  color: #64748b;
  font-size: 0.76rem;
  font-weight: 850;
}

.side-card p,
.side-card strong {
  display: block;
  margin-top: 10px;
  color: #0f172a;
  font-size: 0.9rem;
  line-height: 1.65;
}

.report-card p {
  color: #334155;
  font-size: 0.82rem;
}

.muted-note,
.warning-summary {
  margin: 0;
  color: #64748b;
  font-size: 0.72rem;
  line-height: 1.5;
}

.warning-summary {
  padding: 8px 10px;
  border-radius: 10px;
  color: #92400e;
  background: rgba(254, 243, 199, 0.82);
}

.a2a-evidence-card dl {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 6px 10px;
  margin: 12px 0 0;
}

.a2a-evidence-card dt {
  color: #64748b;
  font-size: 0.7rem;
  font-weight: 850;
}

.a2a-evidence-card dd {
  min-width: 0;
  margin: 0;
  color: #0f172a;
  font-size: 0.74rem;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.evidence-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.evidence-tags i {
  max-width: 100%;
  padding: 5px 8px;
  border-radius: 999px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.88);
  font-style: normal;
  font-size: 0.68rem;
  font-weight: 850;
  overflow-wrap: anywhere;
}

.timeline-card {
  min-height: 0;
}

.decision-card {
  display: grid;
  gap: 14px;
}

.report-block {
  display: grid;
  gap: 9px;
  padding-top: 12px;
  border-top: 1px solid rgba(203, 213, 225, 0.74);
}

.report-block h3 {
  margin: 0;
  color: #0f172a;
  font-size: 0.82rem;
}

.report-block dl {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 6px 10px;
  margin: 0;
}

.report-block dt {
  color: #64748b;
  font-size: 0.7rem;
  font-weight: 850;
}

.report-block dd {
  min-width: 0;
  margin: 0;
  color: #0f172a;
  font-size: 0.76rem;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.diagnosis-block > strong {
  margin-top: 0;
  font-size: 0.82rem;
}

.safety-block > strong {
  margin-top: 0;
  color: #0f172a;
  font-size: 0.82rem;
}

.evidence-list {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.evidence-list i {
  max-width: 100%;
  padding: 5px 8px;
  border-radius: 999px;
  color: #334155;
  background: rgba(226, 232, 240, 0.88);
  font-style: normal;
  font-size: 0.67rem;
  font-weight: 850;
  overflow-wrap: anywhere;
}

.conflict-block > p {
  color: #334155;
  font-size: 0.74rem;
  line-height: 1.55;
}

.conflict-head,
.conflict-edge > div,
.conflict-row > div {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.conflict-status,
.conflict-row em,
.conflict-edge em {
  flex: 0 0 auto;
  width: fit-content;
  padding: 3px 7px;
  border-radius: 999px;
  background: rgba(226, 232, 240, 0.88);
  color: #475569;
  font-style: normal;
  font-size: 0.62rem;
  font-weight: 900;
}

.conflict-status.status-blocked,
.severity-high {
  color: #b91c1c;
  background: rgba(254, 226, 226, 0.9);
}

.conflict-status.status-warning,
.severity-medium {
  color: #b45309;
  background: rgba(254, 243, 199, 0.9);
}

.severity-low {
  color: #0369a1;
  background: rgba(224, 242, 254, 0.88);
}

.conflict-status.status-clear,
.severity-info {
  color: #047857;
  background: rgba(209, 250, 229, 0.88);
}

.conflict-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 7px;
}

.conflict-metric {
  display: grid;
  gap: 2px;
  min-width: 0;
  padding: 8px;
  border-radius: 10px;
}

.conflict-metric b {
  font-size: 0.95rem;
}

.conflict-metric small,
.conflict-node small,
.conflict-edge small,
.conflict-row small {
  color: #64748b;
  font-size: 0.67rem;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.conflict-graph,
.conflict-edge-list,
.conflict-matrix {
  display: grid;
  gap: 8px;
}

.conflict-node-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.conflict-node {
  display: grid;
  gap: 2px;
  max-width: 100%;
  padding: 8px 10px;
  border: 1px solid rgba(203, 213, 225, 0.7);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.68);
}

.conflict-node.node-current {
  border-color: rgba(14, 165, 233, 0.72);
  background: rgba(224, 242, 254, 0.72);
}

.conflict-node.severity-high,
.conflict-edge.severity-high {
  border-color: rgba(248, 113, 113, 0.7);
}

.conflict-node.severity-medium,
.conflict-edge.severity-medium {
  border-color: rgba(251, 191, 36, 0.7);
}

.conflict-node.severity-low,
.conflict-edge.severity-low {
  border-color: rgba(14, 165, 233, 0.55);
}

.conflict-node b,
.conflict-edge b,
.conflict-row b {
  color: #0f172a;
  font-size: 0.72rem;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.conflict-edge,
.conflict-row {
  display: grid;
  gap: 5px;
  padding: 9px;
  border: 1px solid rgba(203, 213, 225, 0.72);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.58);
}

.conflict-row p {
  margin: 0;
  color: #334155;
  font-size: 0.72rem;
  line-height: 1.45;
}

.decision-option,
.vote-item {
  display: grid;
  gap: 5px;
  padding: 10px;
  border: 1px solid rgba(203, 213, 225, 0.74);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.58);
}

.decision-option.selected {
  border-color: rgba(14, 165, 233, 0.72);
  background: rgba(224, 242, 254, 0.72);
}

.decision-option.recommended:not(.selected) {
  border-color: rgba(34, 197, 94, 0.46);
}

.decision-option b,
.vote-item b {
  color: #0f172a;
  font-size: 0.76rem;
}

.decision-option em,
.vote-item em {
  width: fit-content;
  padding: 3px 7px;
  border-radius: 999px;
  color: #475569;
  background: rgba(226, 232, 240, 0.88);
  font-style: normal;
  font-size: 0.62rem;
  font-weight: 900;
}

.vote-item em.risk-high {
  color: #b91c1c;
  background: rgba(254, 226, 226, 0.9);
}

.vote-item em.risk-mid {
  color: #b45309;
  background: rgba(254, 243, 199, 0.9);
}

.vote-item em.risk-low {
  color: #047857;
  background: rgba(209, 250, 229, 0.88);
}

.negotiation-box {
  display: grid;
  gap: 7px;
  padding: 10px;
  border: 1px solid rgba(14, 165, 233, 0.34);
  border-radius: 12px;
  background: rgba(224, 242, 254, 0.52);
}

.negotiation-box b {
  color: #0f172a;
  font-size: 0.76rem;
}

.negotiation-box small {
  color: #475569;
  font-size: 0.7rem;
  line-height: 1.45;
}

.negotiation-votes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.negotiation-votes i {
  max-width: 100%;
  padding: 4px 7px;
  border-radius: 999px;
  color: #0369a1;
  background: rgba(255, 255, 255, 0.72);
  font-style: normal;
  font-size: 0.64rem;
  font-weight: 850;
  overflow-wrap: anywhere;
}

.decision-option p,
.vote-item p,
.vote-item small {
  margin: 0;
  color: #475569;
  font-size: 0.72rem;
  line-height: 1.45;
}

.timeline-list {
  display: grid;
  gap: 12px;
  margin-top: 12px;
}

.timeline-item {
  padding-left: 12px;
  border-left: 3px solid rgba(14, 165, 233, 0.34);
}

.timeline-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.timeline-head b {
  color: #0f172a;
  font-size: 0.78rem;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.timeline-head em {
  flex: 0 0 auto;
  padding: 4px 7px;
  border-radius: 999px;
  color: #334155;
  background: rgba(226, 232, 240, 0.9);
  font-style: normal;
  font-size: 0.62rem;
  font-weight: 900;
}

.timeline-head em.success {
  color: #047857;
  background: rgba(209, 250, 229, 0.88);
}

.timeline-head em.failed {
  color: #b91c1c;
  background: rgba(254, 226, 226, 0.9);
}

.timeline-item p {
  margin-top: 7px;
  color: #334155;
  font-size: 0.78rem;
  line-height: 1.5;
}

.timeline-item small {
  display: block;
  margin-top: 5px;
  color: #64748b;
  font-size: 0.7rem;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.timeline-evidence {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
}

.timeline-evidence i {
  max-width: 100%;
  padding: 4px 7px;
  border-radius: 999px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.88);
  font-style: normal;
  font-size: 0.64rem;
  font-weight: 850;
  overflow-wrap: anywhere;
}

.clarification-card {
  display: grid;
  gap: 12px;
}

.clarification-card textarea {
  min-height: 110px;
  resize: vertical;
  padding: 12px;
  border: 1px solid rgba(148, 163, 184, 0.72);
  border-radius: 12px;
  color: #0f172a;
  background: rgba(255, 255, 255, 0.78);
  font: inherit;
  line-height: 1.55;
  outline: none;
}

.clarification-card textarea:focus {
  border-color: rgba(14, 165, 233, 0.82);
  box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.12);
}

.clarification-card button {
  height: 38px;
  border: 0;
  border-radius: 999px;
  color: #ffffff;
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  font-weight: 900;
  cursor: pointer;
}

.clarification-card button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

@media (max-width: 980px) {
  .run-page {
    grid-template-columns: 1fr;
    height: auto;
  }
}
</style>
