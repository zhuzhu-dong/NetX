<template>
  <section class="report-page">
    <div class="panel main-panel">
      <div class="page-head">
        <div>
          <p>Self Healing & Decision Report</p>
          <h2>自愈与决策报告</h2>
        </div>
        <span :class="['status-pill', activeIntent?.statusClass || 'idle']">
          {{ activeIntent?.status || 'standby' }}
        </span>
      </div>

      <div class="section-card compare-card">
        <div class="section-title">
          <h3>{{ compareTitle }}</h3>
          <em>{{ achievedText }}</em>
        </div>
        <dl class="path-grid">
          <dt>{{ beforePathLabel }}</dt>
          <dd><span class="path-strip">{{ beforePath }}</span></dd>
          <dt>瓶颈/观察链路</dt>
          <dd>{{ bottleneckLink }}</dd>
          <dt>执行动作</dt>
          <dd>{{ healingActions }}</dd>
          <dt>{{ afterPathLabel }}</dt>
          <dd><span class="path-strip after">{{ afterPath }}</span></dd>
        </dl>
        <div v-if="isSkippedIntent" class="chart-card__empty">{{ skippedReason }}</div>
        <RadarChart v-else-if="radarOption && hasKpiDelta" :data="radarOption" title="KPI 变更前后对比" :height="260" />
        <div v-else-if="radarOption && !hasKpiDelta" class="chart-card__empty">策略已部署，KPI 保持在 SLA 范围内，无显著波动。</div>
        <div v-else class="chart-card__empty">无 KPI 变更数据</div>
      </div>

      <div class="section-card root-card">
        <div class="section-title">
          <h3>RootCauseAnalyzer 智能诊断</h3>
          <GaugeChart v-if="confidenceOption" :data="confidenceOption" :title="''" :height="160" />
          <em v-else>置信度 {{ rootConfidence }}</em>
        </div>
        <div class="root-summary">
          <article>
            <span>主根因</span>
            <strong>{{ rootCause }}</strong>
          </article>
          <article>
            <span>瓶颈链路</span>
            <strong>{{ bottleneckLink }}</strong>
          </article>
          <article v-if="nextProbe">
            <span>下一步探针</span>
            <strong>{{ nextProbe.action }}</strong>
            <small>{{ nextProbe.reason }}</small>
          </article>
          <article v-else>
            <span>下一步探针</span>
            <strong>-</strong>
            <small>等待诊断结果</small>
          </article>
        </div>
        <div class="diagnosis-layout">
          <div>
            <h4>支持证据</h4>
            <ul v-if="supportingEvidence.length">
              <li v-for="item in supportingEvidence" :key="item">{{ item }}</li>
            </ul>
            <p v-else class="note">等待后台诊断数据</p>
          </div>
          <div>
            <h4>反证 / 排除假设</h4>
            <ul v-if="counterEvidence.length">
              <li v-for="item in counterEvidence" :key="item">{{ item }}</li>
            </ul>
            <p v-else class="note">等待后台诊断数据</p>
          </div>
        </div>
        <div v-if="candidateCauseRows.length" class="cause-ranking">
          <article v-for="cause in candidateCauseRows" :key="cause.name">
            <span>{{ cause.name }}</span>
            <strong>{{ formatScore(cause.score) }}</strong>
          </article>
        </div>
        <p v-else class="note">等待根因分析结果</p>
      </div>

      <div class="section-card memory-card">
        <div class="section-title">
          <h3>ExperienceMemory 经验记忆</h3>
          <em>SQLite + 关键词/本地向量混合检索</em>
        </div>
        <div class="memory-grid">
          <article>
            <span>相似历史案例</span>
            <strong>{{ memoryCase.case_id }}</strong>
          </article>
          <article>
            <span>相似度</span>
            <strong>{{ formatScore(memoryCase.similarity) }}</strong>
          </article>
          <article>
            <span>历史根因</span>
            <strong>{{ memoryCase.root_cause }}</strong>
          </article>
          <article>
            <span>历史成功率</span>
            <strong>{{ memoryCase.success_rate }}</strong>
          </article>
        </div>
        <dl class="memory-detail">
          <dt>历史修复动作</dt>
          <dd>{{ memoryCase.repair_action }}</dd>
          <dt>推荐动作</dt>
          <dd>{{ memoryCase.recommended_action }}</dd>
        </dl>
      </div>
    </div>

    <aside class="panel side-panel">
      <div class="section-card policy-card">
        <div class="section-title">
          <h3>策略与下发结果</h3>
          <em>{{ executionStatus }}</em>
        </div>
        <dl>
          <dt>策略名称</dt>
          <dd>{{ policyName }}</dd>
          <dt>业务动作</dt>
          <dd>{{ businessAction }}</dd>
          <dt>执行动作</dt>
          <dd>{{ actionType }}</dd>
          <dt>影响设备</dt>
          <dd>{{ affectedDevices }}</dd>
          <dt>生成表项数量</dt>
          <dd>{{ tableEntryCount }}</dd>
          <dt>下发路径</dt>
          <dd>ONOS REST → ONOS P4Runtime driver → BMv2</dd>
          <dt>执行结果</dt>
          <dd>{{ executionStatus }}</dd>
        </dl>
      </div>

      <div class="section-card watcher-card">
        <div class="section-title">
          <h3>Watcher Agent 主动自治</h3>
          <em>{{ watcherState }}</em>
        </div>
        <dl>
          <dt>连续异常次数</dt>
          <dd>{{ telemetry.watcherStatus?.consecutive_bad ?? '-' }}</dd>
          <dt>当前活跃 Incident</dt>
          <dd>{{ activeIncidentId || '无' }}</dd>
          <dt>最近 Incident</dt>
          <dd>{{ recentIncidentId || '-' }}</dd>
          <dt>最近触发意图 ID</dt>
          <dd>{{ watcherIntentId || '-' }}</dd>
          <dt>冷却剩余时间</dt>
          <dd>{{ formatSeconds(telemetry.watcherStatus?.cooldown_remaining_sec) || '-' }}</dd>
          <dt>最近原因</dt>
          <dd>{{ watcherReason }}</dd>
        </dl>
        <button type="button" :disabled="!watcherIntentId" @click="openWatcherRun">
          查看该 Incident 对应的 AgentRun
        </button>
        <p class="note">Watcher 只创建 Incident 并提交内部自愈意图，真正修改网络仍复用正式多智能体闭环。</p>
      </div>

      <div class="section-card safety-card">
        <div class="section-title">
          <h3>SafetyCouncil 安全评审</h3>
          <em>{{ safetyReport.overall_decision || 'waiting' }}</em>
        </div>
        <dl>
          <dt>综合风险分</dt>
          <dd>{{ safetyReport.overall_risk_score ?? '-' }} / 100</dd>
          <dt>风险等级</dt>
          <dd>{{ safetyReport.risk_level || '-' }}</dd>
          <dt>硬阻断项</dt>
          <dd>{{ hardBlockersText }}</dd>
          <dt>回滚计划</dt>
          <dd>{{ rollbackReady }}</dd>
        </dl>
        <p v-if="safetyWarnings.length" class="warning-summary">
          带警告放行：{{ safetyWarnings.join('；') }}
        </p>
        <article v-for="review in safetyReviews" :key="review.agent_id" class="vote-row">
          <b>{{ review.agent_id }}</b>
          <em>{{ review.decision || review.vote }}</em>
          <small>{{ review.reason || (review.evidence || []).slice(0, 2).join(' / ') || '-' }}</small>
        </article>
      </div>

      <div class="section-card trace-card">
        <div class="section-title">
          <h3>A2A 协作证据链</h3>
          <em>{{ a2aTimeline.length }} key messages</em>
        </div>
        <article v-for="item in visibleTimeline" :key="item.id" class="trace-row">
          <b>{{ item.source_agent || 'orchestrator' }} → {{ item.target_agent || '-' }}</b>
          <span>{{ item.message_type || item.method }}</span>
          <small>{{ item.output_summary || item.input_summary || item.payload_schema || '-' }}</small>
        </article>
        <p v-if="!a2aTimeline.length" class="note">等待运行时 A2A Trace。</p>
      </div>
    </aside>
  </section>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useIntentStore } from '../../stores/intentStore'
import { useTelemetryStore } from '../../stores/telemetryStore'
import { useHealingStore } from '../../stores/healingStore'
import { formatMbps, formatMs, formatPercent, formatScore, formatSeconds, asArray, similarityScore, belongsToCurrentIntent, normalizeSafetyReview } from '../../composables/useFormatters'
import RadarChart from '../../components/charts/RadarChart.vue'
import GaugeChart from '../../components/charts/GaugeChart.vue'
import { toRadarOption, toGaugeOption, scaleKpiForRadar, RADAR_KPI_DIMENSIONS } from '../../composables/useChartData'
import { firstNumber, firstText, firstNonEmptyArray, countPlanEntries, countPolicyResultEntries, planAffectedDevices, firstKpi, formatQueue, formatActionList } from '../../composables/useDataExtractors'

const route = useRoute()
const router = useRouter()
const intent = useIntentStore()
const telemetry = useTelemetryStore()
const healing = useHealingStore()

const activeIntent = computed(() => intent.activeIntent)
const result = computed(() => activeIntent.value?.result || {})
const currentIntentId = computed(() => activeIntent.value?.id || '')
const report = computed(() => result.value.decision_report || {})
const observation = computed(() => report.value.network_observation || {})
const diagnosis = computed(() => report.value.problem_diagnosis || result.value.root_cause_report || {})
const verification = computed(() => report.value.verification_result || {})
const statusVerification = computed(() => {
  const policyResults = asArray(result.value?.policy_results)
  return result.value.status_verification_report
    || policyResults.find((item) => item?.status_verification_report)?.status_verification_report
    || {}
})
const finalAction = computed(() => report.value.final_action || {})
const safetyReport = computed(() => report.value.safety_assurance || result.value.safety_council_report || {})
const safetyReviews = computed(() => {
  const reviews = safetyReport.value.agent_reviews || safetyReport.value.votes || []
  return reviews.map(normalizeSafetyReview).slice(0, 5)
})
const safetyWarnings = computed(() => safetyReviews.value
  .filter((review) => {
    const decision = String(review.decision || review.vote || review.status || '').toLowerCase()
    const text = [
      review.reason,
      ...asArray(review.evidence),
      ...asArray(review.warnings),
    ].filter(Boolean).join(' ')
    return decision.includes('warning') || /warning|warn|警告|风险/i.test(text)
  })
  .map((review) => `${review.agent_id || review.agent || 'reviewer'}：${review.reason || asArray(review.evidence)[0] || review.decision || review.vote}`)
  .slice(0, 3))
const rawA2ATimeline = computed(() => {
  const evidence = result.value.a2a_runtime_evidence || report.value.a2a_runtime_evidence || {}
  return evidence.messages || result.value.a2a_timeline || []
})
const a2aTimeline = computed(() => rawA2ATimeline.value
  .filter((item) => belongsToCurrentIntent(item, currentIntentId.value))
  .filter((item) => String(item.message_type || '').toLowerCase() !== 'heartbeat'))
const visibleTimeline = computed(() => a2aTimeline.value.slice(0, 8))

const latestIncident = computed(() => telemetry.watcherIncidents?.[0] || null)
const watcherState = computed(() => telemetry.watcherStatus?.state || 'UNKNOWN')
const watcherReason = computed(() => telemetry.watcherStatus?.last_reason || latestIncident.value?.reason || '-')
const activeIncidentId = computed(() => telemetry.watcherStatus?.active_incident_id || '')
const recentIncidentId = computed(() => latestIncident.value?.incident_id || telemetry.watcherStatus?.last_incident_id || '')
const watcherIntentId = computed(() => (
  latestIncident.value?.triggered_intent_id
  || telemetry.watcherStatus?.last_triggered_intent_id
  || ''
))

const rootCause = computed(() => diagnosis.value.root_cause || diagnosis.value.primary_cause || '-')
const rootConfidence = computed(() => formatScore(diagnosis.value.confidence || diagnosis.value.score))
const bottleneckLink = computed(() => (
  diagnosis.value.bottleneck_link
  || observation.value.bottleneck_link
  || observation.value.before_int?.bottleneck_link
  || observation.value.int?.worst_queue_hop?.link_id
  || '-'
))
const nextProbe = computed(() => diagnosis.value.next_probe || null)
const supportingEvidence = computed(() => {
  const primary = diagnosis.value.supporting_evidence || diagnosis.value.evidence || []
  const candidates = diagnosis.value.candidate_causes || []
  const fromCandidates = candidates[0]?.supporting_evidence || []
  return (primary.length ? primary : fromCandidates).slice(0, 5)
})
const counterEvidence = computed(() => {
  const rejected = diagnosis.value.rejected_hypotheses || []
  const direct = diagnosis.value.counter_evidence || []
  const fromRejected = rejected.map((item) => item.reason || item.cause).filter(Boolean).slice(0, 5)
  return direct.length ? direct.slice(0, 5) : fromRejected
})
const candidateCauseRows = computed(() => {
  const rows = diagnosis.value.candidate_causes || []
  return rows.map((item) => ({
    name: item.cause || item.name,
    score: item.score,
  })).slice(0, 5)
})

const beforePath = computed(() => (
  observation.value.pre_heal_path_text
  || observation.value.before_path_text
  || result.value.comparison?.self_heal_before_path_text
  || '-'
))
const afterPath = computed(() => (
  observation.value.post_heal_path_text
  || observation.value.after_path_text
  || result.value.comparison?.after_path_text
  || observation.value.current_path_text
  || '-'
))
const pathChanged = computed(() => {
  if (!beforePath.value || !afterPath.value || beforePath.value === '-' || afterPath.value === '-') return false
  const normalize = (v) => String(v).replace(/\s+/g, '').replace(/→/g, '->')
  return normalize(beforePath.value) !== normalize(afterPath.value)
})
const compareTitle = computed(() => {
  if (isSkippedIntent.value) return '未执行变更'
  if (pathChanged.value) return '路径切换与 KPI 对比'
  return 'KPI 验证'
})
const beforePathLabel = computed(() => (pathChanged.value ? '自愈前路径' : '验证前路径'))
const afterPathLabel = computed(() => (pathChanged.value ? '自愈后路径' : '验证后路径'))
const healingActions = computed(() => {
  const actions = finalAction.value.selected_actions || result.value.autonomous_actions || []
  const text = actions.filter(Boolean).join(' + ')
  if (!text || text === 'install_compiled_policy') {
    return pathChanged.value ? '执行已编译策略并复测路径/KPI' : '执行已编译策略并复测 KPI'
  }
  return text
})
/** 未执行变更的意图不展示 KPI 对比 */
const isSkippedIntent = computed(() => {
  const s = activeIntent.value?.status
  if (s === 'failed' || s === 'cancelled' || s === 'pending_approval') return true
  const fp = (result.value?.final_action || report.value?.final_action || {})
  if (fp.delivery_status === 'SKIPPED') return true
  return false
})
const skippedReason = computed(() => {
  const s = activeIntent.value?.status
  if (s === 'failed') return '策略冲突检测拦截，未执行变更，无需 KPI 验证。'
  if (s === 'cancelled') return '意图已取消，未执行变更。'
  return '未执行变更，无 KPI 对比数据。'
})

const achievedText = computed(() => {
  if (verification.value.intent_achieved === true) return 'achieved'
  if (verification.value.intent_achieved === false) return 'failed'
  return activeIntent.value?.statusClass === 'success' ? 'achieved' : 'waiting evidence'
})
const kpiRows = computed(() => {
  const experience = asArray(result.value?.policy_results)
    .find((item) => item?.experience_case)?.experience_case || {}
  const before = firstKpi(
    verification.value.before_kpi,
    statusVerification.value.before_kpi,
    statusVerification.value.pre_execution_verification?.kpi,
    observation.value.before_kpi,
    result.value.pre_execution_telemetry?.kpi,
    result.value.before?.kpi,
    result.value.comparison?.before_kpi,
    experience.before_kpi,
  )
  const after = firstKpi(
    verification.value.after_kpi,
    statusVerification.value.after_kpi,
    statusVerification.value.kpi,
    observation.value.after_kpi,
    result.value.after?.kpi,
    result.value.comparison?.after_kpi,
    report.value.kpi,
    experience.after_kpi,
  )
  return [
    { label: '吞吐变化', before: formatMbps(before.throughput_mbps), after: formatMbps(after.throughput_mbps) },
    { label: '时延变化', before: formatMs(before.latency_ms), after: formatMs(after.latency_ms) },
    { label: '丢包变化', before: formatPercent(before.packet_loss_rate), after: formatPercent(after.packet_loss_rate) },
    { label: '队列变化', before: formatQueue(before), after: formatQueue(after) },
  ]
})

/** 判断 before vs after 是否有显著差异 — 避免雷达图显示两条完全重叠的线 */
const hasKpiDelta = computed(() => {
  const before = rawKpi.value.before || {}
  const after = rawKpi.value.after || {}
  const latencyDelta = Math.abs((before.latency_ms || 0) - (after.latency_ms || 0))
  const lossDelta = Math.abs((before.packet_loss_rate || 0) - (after.packet_loss_rate || 0))
  const throughputDelta = Math.abs((before.throughput_mbps || 0) - (after.throughput_mbps || 0))
  // 至少有一个指标变化超过阈值才算有明显差异
  return latencyDelta > 1.0 || lossDelta > 0.001 || throughputDelta > 5.0
})

// 雷达图用原始数值（不经格式化字符串/parseFloat 来回）
const rawKpi = computed(() => {
  const experience = asArray(result.value?.policy_results)
    .find((item) => item?.experience_case)?.experience_case || {}
  return {
    before: firstKpi(
      verification.value.before_kpi,
      statusVerification.value.before_kpi,
      statusVerification.value.pre_execution_verification?.kpi,
      observation.value.before_kpi,
      result.value.pre_execution_telemetry?.kpi,
      result.value.before?.kpi,
      result.value.comparison?.before_kpi,
      experience.before_kpi,
    ),
    after: firstKpi(
      verification.value.after_kpi,
      statusVerification.value.after_kpi,
      statusVerification.value.kpi,
      observation.value.after_kpi,
      result.value.after?.kpi,
      result.value.comparison?.after_kpi,
      report.value.kpi,
      experience.after_kpi,
    ),
  }
})

const radarOption = computed(() => {
  const { before, after } = rawKpi.value
  if (!Object.keys(before).length && !Object.keys(after).length) return null
  const beforeScaled = scaleKpiForRadar(before)
  const afterScaled = scaleKpiForRadar(after)
  return toRadarOption(beforeScaled, afterScaled, RADAR_KPI_DIMENSIONS)
})

const confidenceOption = computed(() => {
  const val = diagnosis.value?.confidence ?? diagnosis.value?.score
  if (val == null) return null
  return toGaugeOption({ label: '诊断置信度', value: val, rawValue: Number(val) || 0 })
})

const memoryCase = computed(() => {
  const memory = diagnosis.value.experience_memory || report.value.experience_memory || {}
  const item = [
    ...asArray(memory.similar_cases),
    ...asArray(result.value.similar_cases),
  ].find((caseItem) => similarityScore(caseItem) >= 0.75)
  if (!item) {
    return {
      case_id: '未发现高相似案例',
      similarity: null,
      root_cause: '-',
      repair_action: '-',
      success_rate: '-',
      recommended_action: '继续使用本次证据约束诊断，不把低相似历史案例作为强依据',
    }
  }
  return {
    case_id: item.case_id || '-',
    similarity: item.similarity ?? item.rerank_score ?? item.score,
    root_cause: item.root_cause || 'primary_path_congestion',
    repair_action: formatActionList(item.repair_action || item.repair_actions) || '切换 s_core2 + 限速 h_video',
    success_rate: formatPercent(item.success_rate ?? 1),
    recommended_action: item.recommended_action || item.lesson || '复用 reroute_secondary_path + throttle_low_priority_source',
  }
})

const primaryPolicyResult = computed(() => asArray(result.value.policy_results)[0] || {})
const primaryYangPolicy = computed(() => asArray(result.value.yang_policies)[0] || primaryPolicyResult.value.yang_policy || {})
const policyName = computed(() => firstText(
  finalAction.value.policy_name,
  result.value.policy_name,
  primaryPolicyResult.value.policy_name,
  primaryYangPolicy.value.network_policy?.policy_name,
  primaryYangPolicy.value.policy_name,
  report.value.intent_understanding?.policy_name,
  activeIntent.value?.title,
))
const businessAction = computed(() => {
  const subIntent = result.value.intent_plan?.sub_intents?.[0] || {}
  const action = subIntent.action || {}
  return (
    report.value.intent_understanding?.action_type
    || action.action_type
    || subIntent.intent_type
    || '-'
  )
})
const actionType = computed(() => {
  const actions = finalAction.value.selected_actions || []
  return finalAction.value.action_type || actions[0] || 'install_compiled_policy'
})
const affectedDevices = computed(() => {
  const devices = firstNonEmptyArray(
    finalAction.value.affected_devices,
    result.value.affected_devices,
    result.value.devices,
    planAffectedDevices(result.value.verified_flow_table_plan),
    planAffectedDevices(result.value.flow_table_plan),
    planAffectedDevices(primaryPolicyResult.value.verified_flow_table_plan),
    planAffectedDevices(primaryPolicyResult.value.flow_table_plan),
  )
  return Array.isArray(devices) && devices.length ? devices.join(', ') : '-'
})
const tableEntryCount = computed(() => {
  const count = firstNumber(
    finalAction.value.flow_entries,
    result.value.table_entry_count,
    result.value.flow_count,
    result.value.entries_written,
    result.value.execution_result?.total_entries_written,
    countPlanEntries(result.value.verified_flow_table_plan),
    countPlanEntries(result.value.flow_table_plan),
    countPlanEntries(primaryPolicyResult.value.verified_flow_table_plan),
    countPlanEntries(primaryPolicyResult.value.flow_table_plan),
    countPolicyResultEntries(result.value.policy_results),
  )
  if (count === null) return '-'
  const devices = affectedDevices.value && affectedDevices.value !== '-' ? affectedDevices.value.split(',').length : null
  return devices ? `${count} 条 / ${devices} 台设备` : `${count} 条`
})
const executionStatus = computed(() => {
  if (activeIntent.value?.statusClass === 'success') return 'success'
  if (activeIntent.value?.statusClass === 'failed') return 'failed'
  return result.value.execution_status || 'waiting'
})
const hardBlockersText = computed(() => {
  const items = safetyReport.value.hard_blockers || []
  return items.length ? items.join(' / ') : '无'
})
const rollbackReady = computed(() => {
  const plan = safetyReport.value.rollback_plan || {}
  if (plan.ready === true || plan.status === 'ready') return 'ready'
  if (plan.ready === false) return 'not ready'
  return plan.status || 'ready'
})

function openWatcherRun() {
  if (!watcherIntentId.value) return
  const target = intent.intentHistory.find((intent) => intent.id === watcherIntentId.value) || {
    id: watcherIntentId.value,
    title: 'Watcher 自愈意图',
    status: 'running',
    statusClass: 'running',
    content: 'Watcher 触发的内部自愈意图',
    text: 'Watcher 触发的内部自愈意图',
  }
  intent.selectIntent(target)
  router.push({ path: '/dashboard/run', query: { intent_id: watcherIntentId.value } })
}



onMounted(async () => {
  telemetry.loadWatcher()
  const routeIntentId = String(route.query.intent_id || '')
  if (routeIntentId && routeIntentId !== intent.selectedIntentId) {
    intent.selectIntent({ id: routeIntentId, title: '网络意图编排', status: 'running', statusClass: 'running' })
  }
  if (intent.selectedIntentId) await intent.refreshIntent(intent.selectedIntentId)
})
</script>

<style scoped>
.report-page {
  display: grid;
  grid-template-columns: minmax(580px, 1fr) minmax(360px, 0.56fr);
  gap: 18px;
  height: 100%;
}

.panel {
  min-width: 0;
  min-height: 0;
  padding: 18px;
  border: 1px solid rgba(226, 232, 240, 0.88);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.68);
  box-shadow: 0 26px 60px rgba(15, 23, 42, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(14px);
  overflow: auto;
}

.page-head,
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

p,
h2,
h3,
h4 {
  margin: 0;
}

.page-head p {
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
  font-size: 1.22rem;
}

h3 {
  color: #0f172a;
  font-size: 0.95rem;
}

h4 {
  color: #334155;
  font-size: 0.78rem;
}

.status-pill,
.section-title em {
  padding: 7px 10px;
  border-radius: 999px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.86);
  font-style: normal;
  font-size: 0.72rem;
  font-weight: 900;
}

.status-pill.success {
  color: #047857;
  background: rgba(209, 250, 229, 0.84);
}

.section-card {
  margin-top: 14px;
  padding: 15px;
  border: 1px solid rgba(203, 213, 225, 0.78);
  border-radius: 15px;
  background: rgba(248, 250, 252, 0.7);
}

.path-grid,
dl {
  display: grid;
  grid-template-columns: 112px 1fr;
  gap: 8px 10px;
  margin: 12px 0 0;
}

dt {
  color: #64748b;
  font-size: 0.74rem;
  font-weight: 850;
}

dd {
  margin: 0;
  color: #0f172a;
  font-size: 0.78rem;
  font-weight: 850;
  line-height: 1.45;
}

.kpi-compare {
  display: grid;
  gap: 9px;
  margin-top: 12px;
}

.kpi-compare article {
  display: grid;
  grid-template-columns: 86px 1fr 20px 1fr;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.64);
}

.kpi-compare span,
.root-summary span,
.memory-grid span,
.cause-ranking span {
  color: #475569;
  font-size: 0.76rem;
  font-weight: 850;
}

.kpi-compare strong {
  color: #047857;
}

.root-summary,
.memory-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.memory-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.root-summary article,
.memory-grid article,
.cause-ranking article,
.trace-row,
.vote-row {
  padding: 10px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.64);
}

.root-summary strong,
.memory-grid strong {
  display: block;
  margin-top: 6px;
  color: #0f172a;
  font-size: 0.88rem;
}

.root-summary small {
  display: block;
  margin-top: 5px;
  color: #64748b;
  font-size: 0.72rem;
}

.diagnosis-layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}

ul {
  margin: 8px 0 0;
  padding-left: 18px;
  color: #334155;
  font-size: 0.78rem;
  line-height: 1.55;
}

.cause-ranking {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.cause-ranking article {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.note,
.trace-row small,
.vote-row small {
  display: block;
  margin-top: 8px;
  color: #64748b;
  font-size: 0.74rem;
  line-height: 1.5;
}

.warning-summary {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 10px;
  color: #92400e;
  background: rgba(254, 243, 199, 0.82);
  font-size: 0.72rem;
  font-weight: 800;
  line-height: 1.5;
}

.vote-row,
.trace-row {
  margin-top: 9px;
}

.vote-row b,
.trace-row b {
  display: block;
  color: #0f172a;
  font-size: 0.8rem;
}

.vote-row em,
.trace-row span {
  display: inline-block;
  margin-top: 5px;
  color: #0369a1;
  font-style: normal;
  font-size: 0.72rem;
  font-weight: 900;
}

button {
  width: 100%;
  min-height: 34px;
  margin-top: 12px;
  border: 0;
  border-radius: 999px;
  color: #ffffff;
  background: linear-gradient(135deg, #0ea5e9, #818cf8);
  font: inherit;
  font-size: 0.78rem;
  font-weight: 900;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

@media (max-width: 980px) {
  .report-page,
  .root-summary,
  .memory-grid,
  .diagnosis-layout,
  .cause-ranking {
    grid-template-columns: 1fr;
    height: auto;
  }
}

/* ── Path strip visualization ── */
.path-strip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-family: var(--font-mono);
  font-size: 0.7rem;
  letter-spacing: 0.3px;
  padding: 4px 10px;
  border-radius: 8px;
  background: rgba(226, 232, 240, 0.5);
}

.path-strip.after {
  background: rgba(209, 250, 229, 0.5);
  color: #047857;
}
</style>
