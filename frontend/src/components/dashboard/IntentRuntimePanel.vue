<template>
  <section class="runtime-panel" data-test="intent-runtime">
    <div v-if="!intentId" class="runtime-empty">
      <span>INTENT RUNTIME</span>
      <h2>尚未选择 Intent</h2>
      <p>提交新意图或从左侧历史记录选择一个 Intent；本页不会自动切换到“最新 Intent”。</p>
    </div>

    <template v-else>
      <header class="runtime-head">
        <div>
          <span>INTENT RUNTIME · {{ intentId }}</span>
          <h2>{{ runtimeData.record?.text || '当前 Intent 运行状态' }}</h2>
        </div>
        <div class="runtime-actions">
          <strong :class="lifecycleTone(lifecycle)">{{ loading ? 'READING' : lifecycle }}</strong>
          <button type="button" @click="load(intentId)">{{ loading ? '读取中…' : '刷新' }}</button>
        </div>
      </header>

      <div v-if="!loading" class="closed-loop" aria-label="Intent lifecycle closed loop">
        <template v-for="(step, index) in loop" :key="step.id">
          <span :class="step.state"><i></i>{{ step.label }}</span>
          <b v-if="index < loop.length - 1">→</b>
        </template>
      </div>

      <div v-if="Object.keys(errors).length" class="runtime-warning">
        部分投影暂不可用：{{ Object.keys(errors).join(' / ') }}。可用字段仍来自真实 API，未用默认值补齐。
      </div>

      <div class="runtime-grid">
        <article>
          <span>Intent</span>
          <strong>{{ intentId }}</strong>
          <small>revision {{ runtime.revision ?? spec.revision ?? 'MISSING' }}</small>
        </article>
        <article>
          <span>Objectives</span>
          <strong>{{ objectives.length || 'NO_SAMPLE' }}</strong>
          <small>{{ objectives.map((item) => item.type).join(' · ') || 'MISSING' }}</small>
        </article>
        <article>
          <span>Initial Plan</span>
          <strong>{{ initialPlan.plan_id || runtime.initial_plan_id || 'MISSING' }}</strong>
          <small>{{ initialPlan.legacy_plan_id ? `legacy ${initialPlan.legacy_plan_id}` : 'FORMAL' }}</small>
        </article>
        <article>
          <span>Healing Plan</span>
          <strong>{{ healingPlan.plan_id || runtime.last_healing_plan_id || 'NOT_CREATED' }}</strong>
          <small>{{ healingPlan.replan_reason || 'NOT_APPLICABLE' }}</small>
        </article>
        <article>
          <span>Plan</span>
          <strong>{{ currentPlan.plan_id || runtime.active_plan_id || 'MISSING' }}</strong>
          <small>CURRENT · {{ currentPlan.validation_status || 'UNAVAILABLE' }}</small>
        </article>
        <article>
          <span>Policy</span>
          <strong>{{ policyIds.length ? policyIds.length : 'NO_SAMPLE' }}</strong>
          <small>{{ policyIds.join(' · ') || 'NOT_APPLICABLE' }}</small>
        </article>
        <article>
          <span>Execution</span>
          <strong>{{ execution.status || 'NOT_STARTED' }}</strong>
          <small>{{ execution.execution_id || 'MISSING' }}</small>
        </article>
        <article>
          <span>Assurance</span>
          <strong>{{ assurance.lifecycle || assuranceStatus }}</strong>
          <small>{{ assurance.evidence_quality || 'MISSING' }}</small>
        </article>
        <article>
          <span>Healing</span>
          <strong>{{ latestHealing.status || 'NO_SAMPLE' }}</strong>
          <small>{{ runtime.healing_attempts ?? 0 }} / {{ runtime.max_healing_attempts ?? 'MISSING' }} attempts</small>
        </article>
        <article>
          <span>Decision</span>
          <strong>{{ latestDecision.decision_type || 'NO_SAMPLE' }}</strong>
          <small>{{ latestDecision.agent_id || 'MISSING' }} · {{ latestDecision.llm_status || 'NOT_USED' }}</small>
        </article>
        <article data-test="llm-runtime-source">
          <span>LLM Decision Source</span>
          <strong>{{ llmRuntimeLabel(latestLlmCall.runtime_mode || latestDecision.llm_runtime_mode, latestLlmCall.status || latestDecision.llm_status) }}</strong>
          <small>{{ latestLlmCall.provider || latestDecision.llm_provider || 'NO_PROVIDER' }} / {{ latestLlmCall.model || latestDecision.llm_model || 'NO_MODEL' }}</small>
        </article>
        <article>
          <span>Evidence</span>
          <strong>{{ evidenceItems.length || 'NO_SAMPLE' }}</strong>
          <small>{{ assurance.observed_at || 'STALE' }}</small>
        </article>
      </div>

      <section class="runtime-detail-row">
        <article>
          <span>当前路径</span>
          <p>{{ currentPath.length ? currentPath.join(' → ') : 'NO_SAMPLE' }}</p>
        </article>
        <article>
          <span>Assurance 摘要</span>
          <p>{{ assurance.summary || 'MISSING' }}</p>
        </article>
        <article>
          <span>最近自愈</span>
          <p>{{ latestHealing.reason || 'NOT_APPLICABLE' }}</p>
        </article>
      </section>

      <section v-if="latestHealing.healing_id" class="healing-truth">
        <header><span>HEALING ACTION TRUTH</span><strong>{{ latestHealing.healing_id }}</strong></header>
        <div>
          <article>
            <span>Suggested</span>
            <p v-for="action in suggestedActions" :key="action.action_id">
              {{ action.type }} · {{ action.reason }}
            </p>
            <p v-if="!suggestedActions.length">NO_SUGGESTION</p>
          </article>
          <article>
            <span>Executed</span>
            <p v-if="executedAction">{{ executedAction.type }} · {{ latestHealing.status }}</p>
            <p v-else>NOT_EXECUTED</p>
          </article>
        </div>
      </section>

      <section v-if="keyTimeline.length" class="key-timeline">
        <span>KEY CLOSED-LOOP TIMELINE</span>
        <ol>
          <li v-for="event in keyTimeline" :key="event.event_id">
            <strong>{{ event.lifecycle }}</strong>
            <p>{{ event.reason }}</p>
            <small>{{ event.plan_id || 'NO_PLAN' }} · {{ event.context_id || 'NO_CONTEXT' }}</small>
          </li>
        </ol>
      </section>

      <details v-if="latestHealing.healing_id">
        <summary>Technical evidence: PlanDiff / candidates / migration proofs / decisions</summary>
        <pre>{{ technicalEvidence }}</pre>
      </details>

      <footer class="runtime-foot">
        <span>历史查看为只读操作；SLA 违反可触发有界自动重规划，撤销仍须由用户显式发起。</span>
        <div>
          <button type="button" @click="$emit('show-detail', intentId)">深度详情</button>
          <button class="primary" type="button" @click="$emit('show-evidence', intentId)">查看完整网络证据</button>
        </div>
      </footer>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, toRef, watch } from 'vue'
import { lifecycleTone, runtimeStepStates } from '../../utils/intentPresentation'
import { llmRuntimeLabel } from '../../utils/llmPresentation'
import { useIntentRuntimeData } from '../../composables/useIntentRuntimeData'

const props = defineProps({ intentId: { type: String, default: '' } })
defineEmits(['show-evidence', 'show-detail'])

const source = toRef(props, 'intentId')
const { data, loading, errors, spec, runtime, lifecycle, load } = useIntentRuntimeData(source)
const runtimeData = computed(() => data.value)
const loop = computed(() => runtimeStepStates(lifecycle.value))
const objectives = computed(() => spec.value.objectives || [])
const initialPlan = computed(() => data.value.plan?.initial || {})
const healingPlan = computed(() => data.value.plan?.healing || {})
const currentPlan = computed(() => data.value.plan?.current || data.value.plan?.active || {})
const execution = computed(() => data.value.execution?.current || {})
const assurance = computed(() => data.value.assurance?.current || {})
const healingItems = computed(() => data.value.healing?.items || [])
const latestHealing = computed(() => healingItems.value[0] || {})
const decisionItems = computed(() => data.value.decisions?.items || [])
const latestDecision = computed(() => decisionItems.value[0] || {})
const llmCallItems = computed(() => data.value.llmCalls?.items || [])
const latestLlmCall = computed(() => llmCallItems.value[0] || {})
const evidenceItems = computed(() => data.value.evidence?.items || [])
const policyIds = computed(() => runtime.value.active_policy_ids || [])
const currentPath = computed(() => runtime.value.current_path?.length
  ? runtime.value.current_path
  : execution.value.selected_path || [])
const assuranceStatus = computed(() => assurance.value.satisfied === true
  ? 'SATISFIED'
  : assurance.value.satisfied === false ? 'VIOLATED' : 'MISSING')
const suggestedActions = computed(() => latestHealing.value.proposed_actions || [])
const executedAction = computed(() => ['EXECUTING', 'VERIFIED', 'FAILED', 'ROLLED_BACK'].includes(latestHealing.value.status)
  ? latestHealing.value.selected_action || null
  : null)
const keyTimeline = computed(() => (data.value.healing?.timeline || []).slice(-9))
const technicalEvidence = computed(() => JSON.stringify({
  source: latestDecision.value.llm_status || 'NOT_USED',
  llm_calls: llmCallItems.value,
  plan_diff: latestHealing.value.plan_diff || null,
  affected_intent_analysis: latestHealing.value.affected_intent_analysis || null,
  candidates: latestHealing.value.candidates || [],
  migration_proofs: latestHealing.value.migration_proofs || [],
  execution_result: latestHealing.value.execution_result || {},
  decisions: decisionItems.value,
}, null, 2))

watch(source, (id) => load(id))
onMounted(() => load(props.intentId))
</script>

<style scoped>
.runtime-panel{height:100%;padding:22px;overflow:auto;color:#17213b;background:linear-gradient(145deg,rgba(248,252,255,.96),rgba(246,245,255,.92))}.runtime-empty{display:grid;place-items:center;align-content:center;height:100%;text-align:center}.runtime-empty span,.runtime-head span,.runtime-grid span,.runtime-detail-row span{color:#2d96d6;font:800 10px ui-monospace,monospace;letter-spacing:.12em}.runtime-empty h2{margin:8px 0}.runtime-empty p{max-width:560px;color:#728196}.runtime-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.runtime-head h2{max-width:860px;margin:6px 0 0;font-size:19px}.runtime-actions{display:flex;align-items:center;gap:8px}.runtime-actions strong{padding:7px 10px;border-radius:999px;font:900 10px ui-monospace}.runtime-actions button,.runtime-foot button{border:1px solid #c9dfed;border-radius:9px;padding:8px 11px;background:white;color:#2e7fae;font-weight:800;cursor:pointer}.success{background:#ddf8ed;color:#087c59}.failed{background:#ffe4e4;color:#c93c3c}.clarification{background:#fff0d4;color:#96620b}.cancelled{background:#edf0f4;color:#737d8b}.running{background:#e1f2ff;color:#167bb9}.closed-loop{display:flex;align-items:center;justify-content:center;gap:7px;margin:18px 0;padding:15px;border:1px solid #d7e6f0;border-radius:15px;background:rgba(255,255,255,.72)}.closed-loop span{display:flex;align-items:center;gap:6px;padding:8px 9px;border-radius:9px;background:#edf3f7;color:#8a97a7;font:800 10px ui-monospace}.closed-loop span i{width:6px;height:6px;border-radius:50%;background:currentColor}.closed-loop span.complete{background:#dcf8ed;color:#087c59}.closed-loop span.active{background:#dff2ff;color:#057bbd;box-shadow:0 0 0 2px rgba(45,158,220,.16)}.closed-loop span.failed{background:#ffe4e4;color:#c93c3c}.closed-loop span.withdrawn{opacity:.55}.closed-loop b{color:#98a8b8}.runtime-warning{margin-bottom:12px;padding:10px 13px;border-radius:10px;background:#fff3da;color:#94620c;font-size:11px}.runtime-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.runtime-grid article,.runtime-detail-row article{display:grid;min-width:0;padding:14px;border:1px solid #d9e6ef;border-radius:13px;background:rgba(255,255,255,.82)}.runtime-grid strong{margin:7px 0;font:850 12px ui-monospace;overflow-wrap:anywhere}.runtime-grid small{color:#8392a5;font-size:9px;overflow-wrap:anywhere}.runtime-detail-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}.runtime-detail-row p{margin:8px 0 0;color:#52657b;font-size:11px;line-height:1.55;overflow-wrap:anywhere}.runtime-foot{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-top:13px;padding-top:13px;border-top:1px solid #dde7ef;color:#7d8c9e;font-size:10px}.runtime-foot div{display:flex;gap:7px}.runtime-foot .primary{border-color:transparent;background:linear-gradient(135deg,#1598e5,#6c62ef);color:white}@media(max-width:900px){.closed-loop{justify-content:flex-start;overflow:auto}.runtime-grid,.runtime-detail-row{grid-template-columns:1fr 1fr}.runtime-foot{align-items:flex-start;flex-direction:column}}
.healing-truth,.key-timeline{margin-top:12px;padding:14px;border:1px solid #d9e6ef;border-radius:13px;background:rgba(255,255,255,.82)}.healing-truth header{display:flex;justify-content:space-between}.healing-truth>div{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:9px}.healing-truth article{padding:10px;border-radius:9px;background:#f3f8fc}.healing-truth p,.key-timeline p{margin:5px 0;font-size:10px;color:#53677c}.key-timeline ol{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:0;list-style:none}.key-timeline li{padding:9px;border-left:3px solid #2d96d6;background:#f5f9fc}.key-timeline small{font:8px ui-monospace;color:#8291a4}details{margin-top:12px}summary{cursor:pointer;color:#477d9f;font-size:11px;font-weight:800}pre{max-height:300px;overflow:auto;padding:12px;border-radius:10px;background:#101827;color:#cce4f5;font-size:9px;white-space:pre-wrap}
</style>
