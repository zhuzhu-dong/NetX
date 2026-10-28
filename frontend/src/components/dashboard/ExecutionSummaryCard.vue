<template>
  <section class="exec-summary-card">
    <!-- ═══ Tier 1: Header + conclusion + metrics ═══ -->
    <div class="report-head">
      <div>
        <span>执行摘要</span>
        <small>Execution Summary</small>
      </div>
      <strong :class="statusClass">{{ summary.status }}</strong>
    </div>

    <p class="report-summary">{{ summary.conclusion }}</p>

    <div class="report-metrics">
      <span v-for="m in summary.metrics" :key="m.label">
        <small>{{ m.label }}</small>
        <b>{{ m.value }}</b>
      </span>
    </div>

    <!-- ═══ Tier 2: Path change + key actions ═══ -->
    <div v-if="hasTier2" class="report-section-grid">
      <div v-if="summary.pathChange && (summary.pathChange.before || summary.pathChange.after)" class="report-section">
        <div class="section-title">
          <i></i>
          <span>路径变化</span>
        </div>
        <p>
          <template v-if="summary.pathChange.before">执行前：{{ summary.pathChange.before }}<br /></template>
          <template v-if="summary.pathChange.after">执行后：{{ summary.pathChange.after }}</template>
          <template v-if="summary.pathChange.state"> · {{ summary.pathChange.state }}</template>
        </p>
      </div>

      <div v-if="summary.keyActions?.length" class="report-section">
        <div class="section-title">
          <i></i>
          <span>执行动作</span>
        </div>
        <ol class="report-actions">
          <li v-for="action in summary.keyActions" :key="action">{{ action }}</li>
        </ol>
      </div>
    </div>

    <!-- ═══ Tier 2: KPI verification table ═══ -->
    <KpiVerifyCard
      v-if="summary.kpiVerification"
      :verification="summary.kpiVerification"
    />

    <!-- ═══ Tier 3: Collapsible details ═══ -->
    <div v-if="hasTier3" class="detail-fold">
      <button type="button" class="detail-toggle" @click="detailExpanded = !detailExpanded">
        <span>{{ detailExpanded ? '收起详情' : '查看详情' }}</span>
        <em>{{ tier3Count }} 项</em>
      </button>

      <div v-if="detailExpanded" class="detail-panel">
        <!-- Diagnosis -->
        <div v-if="summary.diagnosis?.root_cause || summary.diagnosis?.bottleneck_link" class="report-section">
          <div class="section-title">
            <i></i>
            <span>问题诊断</span>
          </div>
          <p>{{ diagnosisText }}</p>
        </div>

        <!-- Candidate solutions -->
        <div v-if="summary.candidateSolutions?.length" class="report-block">
          <div class="section-title">
            <i></i>
            <span>候选方案评估</span>
          </div>
          <div class="plan-list">
            <article
              v-for="plan in summary.candidateSolutions"
              :key="plan.id || plan.title"
              class="plan-item"
              :class="{ selected: plan.selected }"
            >
              <strong>{{ plan.title }}</strong>
              <span>风险 {{ plan.risk }}</span>
              <p>{{ plan.reason }}</p>
            </article>
          </div>
        </div>

        <!-- Agent votes -->
        <div v-if="summary.agentVotes?.length" class="report-block">
          <div class="section-title">
            <i></i>
            <span>智能体共识</span>
          </div>
          <div class="vote-list">
            <span v-for="vote in summary.agentVotes" :key="vote.agent">
              <b>{{ vote.agent }}</b>
              <em>{{ vote.vote }}</em>
            </span>
          </div>
        </div>

        <!-- Evidence chips -->
        <div v-if="summary.evidence?.length" class="report-changes">
          <i v-for="item in summary.evidence" :key="item">{{ item }}</i>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import KpiVerifyCard from './KpiVerifyCard.vue'

const props = defineProps({
  summary: { type: Object, required: true },
})

const detailExpanded = ref(false)

const statusClass = computed(() => {
  const st = props.summary?.status || ''
  if (st.includes('通过')) return 'passed'
  if (st.includes('未通过') || st.includes('复核')) return 'failed'
  return 'pending'
})

const hasTier2 = computed(() => {
  const s = props.summary
  return Boolean(
    (s?.pathChange && (s.pathChange.before || s.pathChange.after))
    || s?.keyActions?.length
  )
})

const hasTier3 = computed(() => {
  const s = props.summary
  return Boolean(
    (s?.diagnosis?.root_cause || s?.diagnosis?.bottleneck_link)
    || s?.candidateSolutions?.length
    || s?.agentVotes?.length
    || s?.evidence?.length
  )
})

const tier3Count = computed(() => {
  const s = props.summary || {}
  let n = 0
  if (s.diagnosis?.root_cause || s.diagnosis?.bottleneck_link) n++
  if (s.candidateSolutions?.length) n++
  if (s.agentVotes?.length) n++
  if (s.evidence?.length) n++
  return n
})

const diagnosisText = computed(() => {
  const d = props.summary?.diagnosis || {}
  return [
    d.root_cause,
    d.bottleneck_link ? `瓶颈链路：${d.bottleneck_link}` : '',
    d.bottleneck_device ? `瓶颈设备：${d.bottleneck_device}` : '',
  ].filter(Boolean).join('；') || '未发现阻断性冲突，策略可继续执行。'
})
</script>

<style scoped>
/* ── Card shell ── */
.exec-summary-card {
  position: relative;
  width: 100%;
  max-width: 600px;
  margin: 0 auto;
  padding: 14px 16px;
  border: 1px solid rgba(14, 165, 233, 0.22);
  border-radius: 16px;
  background:
    radial-gradient(circle at 8% 0, rgba(14, 165, 233, 0.12), transparent 34%),
    linear-gradient(145deg, rgba(255, 255, 255, 0.95), rgba(241, 245, 249, 0.85));
  box-shadow:
    0 4px 16px rgba(15, 23, 42, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.9);
  text-align: left;
  font-size: 0.78rem;
}

.exec-summary-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 3px;
  background: linear-gradient(90deg, #38bdf8, #818cf8, #22c55e);
  opacity: 0.82;
}

/* ── Header ── */
.report-head {
  position: relative;
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 12px;
}

.report-head > div {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.report-head span {
  color: #0f172a;
  font-size: 1rem;
  font-weight: 1000;
  letter-spacing: -0.02em;
}

.report-head small {
  color: #64748b;
  font-size: 0.66rem;
  font-weight: 900;
}

.report-head strong {
  flex: 0 0 auto;
  padding: 5px 11px;
  border-radius: 999px;
  font-size: 0.66rem;
  font-weight: 1000;
}

.report-head strong.passed {
  border: 1px solid rgba(34, 197, 94, 0.24);
  color: #047857;
  background: rgba(220, 252, 231, 0.72);
  box-shadow: 0 8px 22px rgba(34, 197, 94, 0.12);
}

.report-head strong.failed {
  border: 1px solid rgba(239, 68, 68, 0.24);
  color: #b91c1c;
  background: rgba(254, 226, 226, 0.82);
}

.report-head strong.pending {
  border: 1px solid rgba(203, 213, 225, 0.3);
  color: #64748b;
  background: rgba(241, 245, 249, 0.82);
}

/* ── Summary ── */
.report-summary {
  margin: 0 0 14px;
  color: #334155;
  font-size: 0.86rem;
  line-height: 1.75;
  font-weight: 760;
}

/* ── Metrics ── */
.report-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 14px;
}

.report-metrics span {
  min-width: 0;
  padding: 10px 9px;
  border: 1px solid rgba(125, 211, 252, 0.28);
  border-radius: 15px;
  background: rgba(255, 255, 255, 0.64);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.78);
}

.report-metrics small,
.report-metrics b {
  display: block;
}

.report-metrics small {
  color: #64748b;
  font-size: 0.56rem;
  font-weight: 950;
}

.report-metrics b {
  margin-top: 5px;
  color: #0f172a;
  font-size: 0.82rem;
  font-weight: 1000;
}

/* ── Tier 2 section grid ── */
.report-section-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.report-section,
.report-block {
  min-width: 0;
  padding: 12px;
  border: 1px solid rgba(203, 213, 225, 0.44);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.54);
}

.section-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 7px;
}

.section-title i {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: #38bdf8;
  box-shadow: 0 0 14px rgba(56, 189, 248, 0.72);
}

.section-title span {
  color: #0f172a;
  font-size: 0.72rem;
  font-weight: 1000;
}

.report-section p,
.plan-item p {
  margin: 0;
  color: #475569;
  font-size: 0.74rem;
  line-height: 1.65;
  font-weight: 720;
}

.report-block {
  margin-top: 10px;
}

/* ── Actions ── */
.report-actions {
  display: grid;
  gap: 6px;
  margin: 0;
  padding-left: 18px;
  color: #334155;
  font-size: 0.75rem;
  line-height: 1.55;
  font-weight: 800;
}

/* ── Plan list ── */
.plan-list {
  display: grid;
  gap: 8px;
}

.plan-item {
  padding: 10px;
  border: 1px solid rgba(203, 213, 225, 0.48);
  border-radius: 13px;
  background: rgba(248, 250, 252, 0.7);
}

.plan-item.selected {
  border-color: rgba(34, 197, 94, 0.34);
  background: rgba(240, 253, 244, 0.68);
}

.plan-item strong,
.plan-item span {
  display: inline-flex;
  margin-right: 8px;
}

.plan-item strong {
  color: #0f172a;
  font-size: 0.76rem;
  font-weight: 1000;
}

.plan-item span {
  padding: 2px 7px;
  border-radius: 999px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.72);
  font-size: 0.6rem;
  font-weight: 950;
}

.plan-item p {
  margin-top: 6px;
}

/* ── Vote list ── */
.vote-list {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.vote-list span {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 9px;
  border: 1px solid rgba(203, 213, 225, 0.42);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.68);
}

.vote-list b {
  color: #334155;
  font-size: 0.64rem;
  font-weight: 1000;
}

.vote-list em {
  color: #047857;
  font-size: 0.58rem;
  font-style: normal;
  font-weight: 1000;
}

/* ── Evidence chips ── */
.report-changes {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 12px;
}

.report-changes i {
  max-width: 100%;
  padding: 5px 9px;
  border-radius: 999px;
  color: #334155;
  background: rgba(241, 245, 249, 0.78);
  font-size: 0.62rem;
  font-style: normal;
  font-weight: 900;
}

/* ── Collapsible detail toggle ── */
.detail-fold {
  margin-top: 12px;
}

.detail-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 0 11px;
  border: 1px solid rgba(203, 213, 225, 0.56);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.78);
  color: #334155;
  font: inherit;
  font-size: 0.72rem;
  font-weight: 950;
  cursor: pointer;
  transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease;
}

.detail-toggle:hover {
  transform: translateY(-1px);
  border-color: rgba(14, 165, 233, 0.38);
  background: rgba(240, 249, 255, 0.86);
}

.detail-toggle em {
  color: #0284c7;
  font-size: 0.62rem;
  font-style: normal;
  font-weight: 950;
}

.detail-panel {
  display: grid;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid rgba(203, 213, 225, 0.5);
  border-radius: 16px;
  background: rgba(248, 250, 252, 0.76);
}

/* ── Responsive: collapse metric grid on narrow screens ── */
@media (max-width: 640px) {
  .report-metrics {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .report-section-grid {
    grid-template-columns: 1fr;
  }
}
</style>
