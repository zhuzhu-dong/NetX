<template>
  <section class="agent-panel">
    <div class="pipeline-overview" aria-label="执行流水线概览">
      <div class="pipeline-stat">
        <span>当前阶段</span>
        <strong>{{ activeStage?.name || '等待调度' }}</strong>
        <small>{{ activeStage?.agent || '尚无执行智能体' }}</small>
      </div>
      <div class="pipeline-stat">
        <span>阶段进度</span>
        <strong>{{ completedCount }} / {{ enrichedStages.length }}</strong>
        <small>{{ failedCount ? `${failedCount} 个阶段异常` : '执行链路状态正常' }}</small>
      </div>
      <div class="pipeline-stat">
        <span>累计耗时</span>
        <strong>{{ totalDurationText }}</strong>
        <small>已返回阶段耗时汇总</small>
      </div>
      <div class="pipeline-stat">
        <span>证据引用</span>
        <strong>{{ totalEvidence }} 项</strong>
        <small>输入快照、策略与验证记录</small>
      </div>
    </div>

    <div class="pipeline-body">
      <div class="stage-rail" role="list" aria-label="智能体执行阶段">
      <button
        v-for="(stage, index) in enrichedStages"
        :key="stage.key"
        type="button"
        class="stage-step"
        :class="[stage.state, { active: stage.key === activeStage?.key }]"
        :aria-current="stage.key === activeStage?.key ? 'step' : undefined"
        @click="$emit('select-stage', stage.key)"
      >
        <span class="step-marker">
          <span v-if="isComplete(stage)" class="step-check">✓</span>
          <span v-else>{{ String(index + 1).padStart(2, '0') }}</span>
        </span>
        <span class="step-copy">
          <strong>{{ stage.name }}</strong>
          <small>{{ stage.agent }}</small>
        </span>
        <span class="step-state">{{ stateLabel(stage.state) }}</span>
      </button>
      </div>

      <div v-if="activeStage" class="stage-inspector">
      <div class="inspector-head">
        <div>
          <span class="inspector-kicker">{{ activeStage.key }} · {{ activeStage.agent }}</span>
          <h4>{{ activeStage.name }}</h4>
        </div>
        <span class="inspector-status" :class="activeStage.state">
          <i></i>{{ stateLabel(activeStage.state) }}
        </span>
      </div>

      <div class="inspector-grid">
        <div class="detail-cell detail-wide">
          <span>阶段输出</span>
          <p>{{ activeStage.output || activeStage.input || waitingText(activeStage) }}</p>
        </div>
        <div class="detail-cell">
          <span>处理耗时</span>
          <strong>{{ durationText(activeStage) }}</strong>
        </div>
        <div class="detail-cell">
          <span>证据引用</span>
          <strong>{{ evidenceCount(activeStage) }}</strong>
        </div>
      </div>

      <details v-if="activeStage.input || activeStage.evidence" class="evidence-fold">
        <summary>查看输入与技术证据</summary>
        <dl>
          <template v-if="activeStage.input">
            <dt>输入</dt>
            <dd>{{ activeStage.input }}</dd>
          </template>
          <template v-if="activeStage.evidence">
            <dt>证据</dt>
            <dd>{{ activeStage.evidence }}</dd>
          </template>
        </dl>
      </details>
      </div>
    </div>

  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  stages: { type: Array, default: () => [] },
  logs: { type: Array, default: () => [] },
  selectedStage: { type: String, default: '' },
})

defineEmits(['select-stage'])

const enrichedStages = computed(() => props.stages.map((stage) => ({ ...stage })))
const activeStage = computed(() => (
  enrichedStages.value.find((stage) => stage.key === props.selectedStage)
  || enrichedStages.value.find((stage) => stage.state === 'running')
  || [...enrichedStages.value].reverse().find((stage) => stage.state === 'complete' || stage.state === 'success' || stage.state === 'failed')
  || enrichedStages.value[0]
  || null
))
const completedCount = computed(() => enrichedStages.value.filter(isComplete).length)
const failedCount = computed(() => enrichedStages.value.filter((stage) => String(stage.state).toLowerCase() === 'failed').length)
const totalEvidence = computed(() => enrichedStages.value.reduce((total, stage) => {
  if (!stage.evidence) return total
  if (Array.isArray(stage.evidence)) return total + stage.evidence.length
  return total + (String(stage.evidence).split(',').filter(Boolean).length || 1)
}, 0))
const totalDurationMs = computed(() => enrichedStages.value.reduce((total, stage) => {
  if (stage.duration_ms != null && Number.isFinite(Number(stage.duration_ms))) return total + Number(stage.duration_ms)
  const parsed = Number.parseFloat(stage.duration)
  return total + (Number.isFinite(parsed) ? parsed : 0)
}, 0))
const totalDurationText = computed(() => {
  if (!totalDurationMs.value) return '待返回'
  if (totalDurationMs.value >= 1000) return `${(totalDurationMs.value / 1000).toFixed(2)} s`
  return `${totalDurationMs.value.toFixed(totalDurationMs.value >= 100 ? 0 : 2)} ms`
})

function isComplete(stage) {
  return stage.state === 'complete' || stage.state === 'success'
}

function stateLabel(state) {
  return {
    complete: '已完成',
    success: '已完成',
    running: '执行中',
    failed: '失败',
    waiting: '等待',
    skipped: '已跳过',
  }[String(state || '').toLowerCase()] || '等待'
}

function durationText(stage) {
  if (stage.duration_ms != null) return `${Number(stage.duration_ms).toFixed(Number(stage.duration_ms) >= 100 ? 0 : 2)} ms`
  return stage.duration || '尚未返回'
}

function evidenceCount(stage) {
  if (!stage.evidence) return '尚未返回'
  if (Array.isArray(stage.evidence)) return `${stage.evidence.length} 项`
  return `${String(stage.evidence).split(',').filter(Boolean).length || 1} 项`
}

function waitingText(stage) {
  if (stage.state === 'failed') return '该阶段执行失败，详细原因以后台返回的执行报告为准。'
  if (stage.state === 'running') return '正在接收该阶段的实时执行结果。'
  if (stage.state === 'skipped') return '该阶段未进入执行流程。'
  return '后台尚未返回该阶段的输入、输出或证据。'
}
</script>

<style scoped>
.agent-panel { min-width:0; }
.pipeline-overview { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); margin-bottom:16px; border:1px solid rgba(132,164,194,.2); border-radius:6px; background:linear-gradient(90deg,rgba(244,250,253,.88),rgba(248,249,254,.76)); overflow:hidden; }
.pipeline-stat { min-width:0; padding:11px 14px; border-right:1px solid rgba(132,164,194,.18); }
.pipeline-stat:last-child { border-right:0; }
.pipeline-stat span,.pipeline-stat small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pipeline-stat span { color:#718096; font-size:.59rem; font-weight:850; letter-spacing:.35px; }
.pipeline-stat strong { display:block; margin-top:5px; overflow:hidden; color:#172033; font-size:.84rem; font-weight:900; text-overflow:ellipsis; white-space:nowrap; }
.pipeline-stat small { margin-top:4px; color:#8a98aa; font-size:.57rem; font-weight:700; }
.pipeline-body { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(310px,.62fr); gap:20px; min-width:0; }
.stage-rail { position:relative; display:grid; grid-template-columns:repeat(6,minmax(92px,1fr)); align-items:start; gap:0; min-width:0; padding:12px 0 8px; overflow-x:auto; scrollbar-width:thin; }
.stage-rail::before { content:''; position:absolute; top:32px; left:8.2%; right:8.2%; height:1px; background:linear-gradient(90deg,rgba(50,182,216,.2),rgba(111,141,245,.45),rgba(21,155,117,.24)); }
.stage-step { position:relative; z-index:1; min-width:92px; padding:0 6px 10px; border:0; color:inherit; background:transparent; font:inherit; text-align:center; cursor:pointer; }
.step-marker { display:grid; place-items:center; width:40px; height:40px; margin:0 auto 10px; border:1px solid rgba(122,153,184,.32); border-radius:50%; color:#68778d; background:#f8fbfd; box-shadow:0 0 0 6px rgba(255,255,255,.94); font:800 .64rem/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; transition:border-color .18s ease,box-shadow .18s ease,background .18s ease,transform .18s ease; }
.stage-step:hover .step-marker,.stage-step.active .step-marker { transform:translateY(-2px); border-color:rgba(22,137,202,.58); box-shadow:0 0 0 6px rgba(255,255,255,.95),0 8px 20px rgba(22,137,202,.14); }
.stage-step.active .step-copy strong { color:#0879b8; }
.stage-step.complete .step-marker,.stage-step.success .step-marker { color:#087a61; border-color:rgba(21,155,117,.32); background:#eefbf7; }
.stage-step.running .step-marker { color:#0879b8; border-color:rgba(22,137,202,.58); background:#eaf7fd; box-shadow:0 0 0 6px rgba(231,246,253,.72),0 0 22px rgba(50,182,216,.2); }
.stage-step.running .step-marker::after { content:''; position:absolute; width:48px; height:48px; border:1px solid rgba(50,182,216,.24); border-radius:50%; animation:stagePulse 1.8s ease-out infinite; }
.stage-step.failed .step-marker { color:#b52b3b; border-color:rgba(207,83,98,.4); background:#fff1f2; }
.stage-step.waiting { opacity:.62; }
.step-check { font-size:.84rem; }
.step-copy { display:block; min-width:0; }
.step-copy strong { display:block; overflow:hidden; color:#172033; font-size:.73rem; font-weight:900; text-overflow:ellipsis; white-space:nowrap; }
.step-copy small { display:block; margin-top:4px; overflow:hidden; color:#718096; font-size:.6rem; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
.step-state { display:inline-block; margin-top:6px; color:#718096; font-size:.58rem; font-weight:850; }
.stage-step.running .step-state { color:#0284c7; }
.stage-step.failed .step-state { color:#dc2626; }

.stage-inspector { min-width:0; padding:16px; border:1px solid rgba(71,154,211,.22); border-radius:6px; background:linear-gradient(145deg,rgba(246,251,254,.96),rgba(241,247,252,.78)); box-shadow:inset 3px 0 0 rgba(50,182,216,.5); }
.inspector-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:14px; }
.inspector-kicker { color:#1689ca; font:850 .62rem/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; letter-spacing:.4px; text-transform:uppercase; }
.inspector-head h4 { margin:5px 0 0; color:#0f172a; font-size:1rem; }
.inspector-status { display:flex; align-items:center; gap:6px; color:#64748b; font-size:.68rem; font-weight:850; }
.inspector-status i { width:7px; height:7px; border-radius:50%; background:#94a3b8; }
.inspector-status.complete i,.inspector-status.success i { background:#10b981; }
.inspector-status.running i { background:#0ea5e9; box-shadow:0 0 0 5px rgba(14,165,233,.11); }
.inspector-status.failed i { background:#ef4444; }
.inspector-grid { display:grid; grid-template-columns:1fr 1fr; border-top:1px solid rgba(122,153,184,.2); }
.detail-cell { min-width:0; padding:12px 10px; border-left:1px solid rgba(122,153,184,.18); border-bottom:1px solid rgba(122,153,184,.18); }
.detail-wide { grid-column:1 / -1; min-height:72px; border-left:0; padding-left:0; }
.detail-cell:nth-child(2) { border-left:0; padding-left:0; }
.detail-cell > span { display:block; margin-bottom:6px; color:#64748b; font-size:.64rem; font-weight:800; }
.detail-cell p,.detail-cell strong { margin:0; color:#1e293b; font-size:.75rem; font-weight:750; line-height:1.6; overflow-wrap:anywhere; }
.detail-cell strong { font-size:.82rem; }
.evidence-fold { margin-top:12px; border-top:0; }
.evidence-fold summary { padding:8px 0 2px; color:#475569; font-size:.68rem; font-weight:850; cursor:pointer; user-select:none; }
.evidence-fold dl { display:grid; grid-template-columns:54px 1fr; gap:7px 12px; margin:8px 0 0; }
.evidence-fold dt,.evidence-fold dd { margin:0; font-size:.7rem; line-height:1.55; }
.evidence-fold dt { color:#94a3b8; font-weight:850; }
.evidence-fold dd { color:#334155; font-weight:700; overflow-wrap:anywhere; }
@keyframes stagePulse { to { transform:scale(1.3); opacity:0; } }
@media (max-width:1180px) {
  .pipeline-body { grid-template-columns:1fr; }
  .stage-rail { grid-template-columns:repeat(6,minmax(108px,1fr)); }
}
@media (max-width:900px) {
  .pipeline-overview { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .pipeline-stat:nth-child(2) { border-right:0; }
  .pipeline-stat:nth-child(-n+2) { border-bottom:1px solid rgba(132,164,194,.18); }
  .stage-rail { grid-template-columns:repeat(6,120px); }
  .inspector-grid { grid-template-columns:1fr 1fr; }
  .detail-wide { grid-column:1 / -1; border-left:0; padding-left:0; }
}
@media (max-width:560px) {
  .pipeline-overview { grid-template-columns:1fr; }
  .pipeline-stat { border-right:0; border-bottom:1px solid rgba(132,164,194,.18); }
  .pipeline-stat:last-child { border-bottom:0; }
}
</style>
