<template>
  <section class="detail-shell">
    <aside class="detail-nav">
      <div class="brand"><span>N</span><div><strong>NetPulse</strong><small>Intent Control Plane</small></div></div>
      <button @click="router.push(`/dashboard/intents/${intentId}`)">← 返回意图助手</button>
      <nav>
        <a href="#understanding">01 意图理解</a><a href="#planning">02 候选规划</a><a href="#execution">03 策略与执行</a><a href="#assurance">04 持续保障</a><a href="#healing">05 诊断与自愈</a>
      </nav>
      <button class="diagnostic" @click="router.push(`/dashboard/evidence/${intentId}`)">网络证据视图</button>
    </aside>

    <main class="detail-main">
      <div v-if="loading" class="state-card">正在读取 Intent 控制面真实状态…</div>
      <div v-else-if="error" class="state-card error">{{ error }}<button @click="load">重试</button></div>
      <template v-else>
        <header class="detail-head">
          <div>
            <p>INTENT / {{ intentId }}</p>
            <h1>{{ record.text || spec.original_text }}</h1>
            <div class="meta"><span>revision {{ spec.revision || 1 }}</span><span>tenant {{ spec.tenant_id || 'default' }}</span><span>priority {{ spec.priority }}</span><span>context {{ runtime.context_id || '-' }}</span></div>
          </div>
          <div class="head-actions">
            <span class="lifecycle" :class="tone(runtime.lifecycle)">{{ runtime.lifecycle || 'RECEIVED' }}</span>
            <button :disabled="busy" @click="reevaluate">重新验证</button>
            <button :disabled="busy" @click="replan">重新规划</button>
            <button class="danger" :disabled="busy || runtime.lifecycle === 'WITHDRAWN'" @click="withdraw">撤销意图</button>
          </div>
        </header>
        <div v-if="infrastructureNotice" class="notice" :class="infrastructureNotice.tone"><strong>{{ infrastructureNotice.title }}</strong><span>{{ infrastructureNotice.message }}</span></div>

        <section class="panel decision-panel">
          <PanelTitle index="00" eyebrow="FINAL DECISION" title="最终决策与执行事实" />
          <div class="decision-grid">
            <article><span>最终状态</span><strong :class="tone(decisionView.finalStatus)">{{ decisionView.finalStatus }}</strong></article>
            <article><span>停止类别</span><strong>{{ decisionView.stopCategory }}</strong></article>
            <article><span>最终原因</span><strong>{{ decisionView.finalReason }}</strong></article>
            <article><span>Southbound</span><strong>{{ decisionView.southbound }}</strong></article>
          </div>
          <p v-if="decisionView.notExecuted" class="not-executed">网络配置：未执行。ONOS、P4Runtime、TC、Telemetry、INT、Assurance 均为 NOT_APPLICABLE。</p>
          <div class="action-evidence"><div><b>计划动作</b><span>{{ decisionView.plannedActions.length ? decisionView.plannedActions.join('；') : '[]' }}</span></div><div><b>实际动作</b><span>{{ decisionView.actualActions.length ? decisionView.actualActions.join('；') : '[]' }}</span></div></div>
        </section>

        <section class="summary-grid">
          <article><span>目标判定</span><strong>{{ assuranceSummary }}</strong><small>{{ assurance.current?.evidence_quality || 'MISSING' }}</small></article>
          <article><span>活动计划</span><strong>{{ runtime.active_plan_id || plan.active?.plan_id || '-' }}</strong><small>{{ plan.active?.validation_status || 'NOT_READY' }}</small></article>
          <article><span>活动策略</span><strong>{{ runtime.active_policy_ids?.length || 0 }}</strong><small>NetworkPolicy / DeviceConfig</small></article>
          <article><span>自愈次数</span><strong>{{ runtime.healing_attempts || 0 }} / {{ runtime.max_healing_attempts || 3 }}</strong><small>bounded · anti-flap</small></article>
        </section>

        <section id="understanding" class="panel">
          <PanelTitle index="01" eyebrow="INTENT UNDERSTANDING" title="自然语言 → IntentSpec" />
          <div class="understanding-grid">
            <article class="raw"><span>原始意图</span><p>{{ spec.original_text || record.text }}</p><small>{{ llmRuntimeLabel(spec.llm_runtime_mode, spec.llm_status) }} · {{ spec.llm_provider || 'NO_PROVIDER' }}/{{ spec.llm_model || 'NO_MODEL' }} · confidence {{ percent(spec.confidence) }}</small></article>
            <dl><div><dt>Source</dt><dd>{{ endpoint(spec.source) }}</dd></div><div><dt>Destination</dt><dd>{{ endpoint(spec.destination) }}</dd></div><div><dt>Service</dt><dd>{{ serviceLabel }}</dd></div><div><dt>Selector</dt><dd>{{ compact(spec.selector) }}</dd></div></dl>
          </div>
          <div class="objective-grid">
            <article v-for="objective in spec.objectives || []" :key="objective.objective_id"><span>{{ objective.type }}</span><strong>{{ operator(objective.operator) }} {{ objective.value }} {{ objective.unit || '' }}</strong><small>{{ objective.origin }} · {{ objective.explicit ? 'explicit' : 'inferred' }}</small></article>
            <article v-if="!spec.objectives?.length" class="unknown"><span>OBJECTIVES</span><strong>尚未解析</strong><small>需要澄清，系统不会编造数值目标</small></article>
          </div>
          <div v-if="spec.clarification_required" class="notice warn"><strong>需要澄清</strong><span>{{ (spec.clarification_questions || []).join('；') }}</span></div>
        </section>

        <section id="planning" class="panel">
          <PanelTitle index="02" eyebrow="CANDIDATE PLANNING" title="版本化上下文与候选计划" />
          <div class="planning-meta"><span>Context {{ plan.active?.context_id || runtime.context_id || '-' }}</span><span>PlanValidator {{ plan.active?.validation_status || 'NOT_READY' }}</span><span>{{ llmRuntimeLabel(plan.active?.llm_runtime_mode, plan.active?.llm_status) }}</span><span>{{ plan.active?.llm_provider || 'NO_PROVIDER' }}/{{ plan.active?.llm_model || 'NO_MODEL' }}</span></div>
          <div class="candidate-list">
            <article v-for="candidate in plan.active?.candidates || []" :key="candidate.candidate_id" :class="{ selected: candidate.candidate_id === plan.active?.selected_candidate_id }">
              <div><span>{{ candidate.candidate_id }}</span><b v-if="candidate.candidate_id === plan.active?.selected_candidate_id">SELECTED</b></div>
              <strong>{{ (candidate.path || []).join(' → ') || 'No feasible path' }}</strong>
              <small>score {{ candidate.score }} · latency {{ candidate.expected_outcomes?.estimated_latency_ms ?? '-' }} ms · capacity {{ candidate.expected_outcomes?.bottleneck_available_mbps ?? '-' }} Mbps</small>
            </article>
          </div>
          <div class="topology-card">
            <div class="topology-head"><strong>Selected Intent Path</strong><small>只有当前计划路径高亮；网络全局视图作为诊断证据。</small></div>
            <svg v-if="topologyNodes.length" viewBox="0 0 800 300" role="img" aria-label="Intent selected path over live topology">
              <line v-for="link in topologyLinks" :key="link.id" :x1="link.a.x" :y1="link.a.y" :x2="link.b.x" :y2="link.b.y" :class="{ active: link.active }" />
              <g v-for="node in topologyNodes" :key="node.id" :transform="`translate(${node.x},${node.y})`" :class="{ active: node.active }"><circle r="15"/><text y="29">{{ node.label }}</text></g>
            </svg>
            <div v-else class="topology-empty">实时拓扑证据暂不可用。</div>
          </div>
        </section>

        <section id="execution" class="panel">
          <PanelTitle index="03" eyebrow="POLICY & EXECUTION" title="IntentPlan → NetworkPolicy → DeviceConfig" />
          <div class="layer-flow"><span>IntentSpec</span><b>→</b><span>IntentPlan</span><b>→</b><span>NetworkPolicy</span><b>→</b><span>DeviceConfig</span><b>→</b><span>Network</span></div>
          <div class="execution-grid">
            <article><span>Deterministic execution</span><strong>{{ execution.current?.status || 'NOT_STARTED' }}</strong><small>{{ execution.current?.execution_id || '-' }}</small></article>
            <article><span>ONOS readback</span><strong>{{ execution.current?.config_verified ? 'VERIFIED' : 'UNKNOWN' }}</strong><small>{{ execution.current?.flows_installed || 0 }} real flow IDs</small></article>
            <article><span>Installed policies</span><strong>{{ runtime.active_policy_ids?.length || 0 }}</strong><small>{{ (runtime.active_policy_ids || []).join(', ') || '-' }}</small></article>
            <article><span>Rollback</span><strong>{{ execution.current?.rollback_refs?.length ? 'READY' : 'UNKNOWN' }}</strong><small>先建后拆，保留控制器真实 flow ID</small></article>
          </div>
          <details v-if="execution.current"><summary>设备执行与读回回执</summary><pre>{{ compact(execution.current) }}</pre></details>
        </section>

        <section id="assurance" class="panel">
          <PanelTitle index="04" eyebrow="INTENT ASSURANCE" title="逐目标证据判定" />
          <div class="assurance-head"><span class="lifecycle" :class="tone(assurance.current?.lifecycle)">{{ assurance.current?.lifecycle || 'NO ASSESSMENT' }}</span><small>{{ assurance.current?.summary || '等待可关联的真实遥测证据。' }}</small></div>
          <div class="assessment-list">
            <article v-for="item in assurance.current?.assessments || []" :key="item.objective_id">
              <i :class="assessmentTone(item.status)">{{ symbol(item.status) }}</i><div><strong>{{ item.objective_type }}</strong><small>{{ item.reason }}</small></div><span :class="assessmentTone(item.status)">{{ item.status }}</span>
            </article>
            <div v-if="!assurance.current?.assessments?.length" class="empty">尚无 ObjectiveAssessment；缺失证据将返回 UNKNOWN，而非成功。</div>
          </div>
          <details><summary>证据明细（{{ evidence.items?.length || 0 }}）</summary><pre>{{ compact(evidence.items || []) }}</pre></details>
        </section>

        <section id="healing" class="panel">
          <PanelTitle index="05" eyebrow="DIAGNOSIS & HEALING" title="诊断、自愈与决策时间线" />
          <div class="healing-grid">
            <article><span>当前路径</span><strong>{{ healingView.currentPathLabel }}</strong><small>来自当前运行态 / INT 证据</small></article>
            <article><span>当前瓶颈</span><strong>{{ healingView.bottleneck }}</strong><small>按根因影响链路定位</small></article>
            <article><span>可安全恢复资源</span><strong>{{ healingView.recoverableMbps }} Mbps</strong><small>仅统计可抢占或已过期资源</small></article>
            <article><span>最终结果</span><strong :class="tone(healingView.lifecycle)">{{ healingView.conclusion }}</strong><small>needs_repair 不作为完成态</small></article>
            <article><span>Consecutive failures</span><strong>{{ assurance.current?.consecutive_failures || runtime.consecutive_failures || 0 }}</strong><small>达到阈值才触发 VIOLATED</small></article>
            <article><span>Cooldown</span><strong>{{ runtime.cooldown_until ? formatTime(runtime.cooldown_until) : 'inactive' }}</strong><small>防止路径和策略反复振荡</small></article>
            <article><span>Healing attempts</span><strong>{{ runtime.healing_attempts || 0 }}</strong><small>仅统计真实数据面变更</small></article>
            <article><span>确认采样</span><strong>{{ runtime.measurement_confirmations || healingView.confirmations.length || 0 }}</strong><small>不是 Healing execution</small></article>
          </div>
          <div class="healing-candidates">
            <article v-for="candidate in healingView.candidates" :key="candidate.id" :class="candidate.applicability.toLowerCase()">
              <header><span>{{ candidate.action }} · {{ candidate.target }}</span><b>{{ candidate.phaseLabel }}</b></header>
              <strong>{{ candidate.applicability }}</strong>
              <small>{{ candidate.reason }}</small>
              <em>预计可用 {{ candidate.expectedAvailableMbps ?? '-' }} Mbps · 增益 {{ candidate.expectedGainMbps ?? '-' }} Mbps · 绕开瓶颈 {{ candidate.bottleneckAvoided ?? '-' }}</em>
            </article>
            <div v-if="!healingView.candidates.length" class="empty">尚无经过适用性校验的候选动作记录。</div>
          </div>
          <div v-if="healingView.executions.length" class="healing-executions">
            <strong>真实 Healing Execution</strong>
            <small v-for="item in healingView.executions" :key="item.id">{{ item.action }} · Plan {{ item.planId }} · Execution {{ item.executionId }} · Data Plane Changed = YES</small>
          </div>
          <details class="technical-timeline">
            <summary>技术日志（{{ reversedHistory.length }}）</summary>
            <ol class="timeline">
              <li v-for="item in reversedHistory" :key="item.event_id"><i :class="tone(item.lifecycle)"></i><div><strong>{{ item.lifecycle }}</strong><span>{{ item.reason }}</span><small>{{ item.actor }} · {{ formatTime(item.created_at) }}</small></div></li>
            </ol>
          </details>
        </section>
      </template>
    </main>
  </section>
</template>

<script setup>
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as api from '../../api/client'
import { llmRuntimeLabel } from '../../utils/llmPresentation'
import { buildHealingPresentation } from '../../utils/healingPresentation'
import { infrastructurePresentation } from '../../utils/deviceAvailabilityPresentation'

const PanelTitle = defineComponent({ props: ['index','eyebrow','title'], setup: p => () => h('header',{class:'panel-title'},[h('span',p.index),h('div',[h('p',p.eyebrow),h('h2',p.title)])]) })
const route = useRoute(); const router = useRouter(); const intentId = computed(() => String(route.params.intentId || ''))
const loading=ref(true),busy=ref(false),error=ref(''),record=ref({}),spec=ref({}),runtime=ref({}),plan=ref({}),execution=ref({}),assurance=ref({}),history=ref({}),decisions=ref({}),decisionReport=ref({}),llmCalls=ref({}),evidence=ref({}),healing=ref({}),topology=ref({})

let detailRequest = null
async function load(){
  detailRequest?.abort(); const controller = new AbortController(); detailRequest = controller; loading.value=true; error.value=''
  try {
    const [details,t,report] = await Promise.all([
      api.getIntentDetails(intentId.value,{signal:controller.signal}),
      api.getTopology().catch(()=>({})),
      api.getDecisionReport(intentId.value).catch(()=>({})),
    ])
    record.value=details.record||{}; spec.value=details.spec?.intent_spec||details.record?.intent_spec||{}
    runtime.value=details.runtime?.runtime||details.record?.intent_runtime||{}; plan.value=details.plan||{}
    execution.value=details.execution||{}; assurance.value=details.assurance||{}; history.value=details.history||{}
    decisions.value=details.decisions||{}; decisionReport.value=report.decision_report||report||{}; llmCalls.value=details.llm_calls||{}; evidence.value=details.evidence||{}
    healing.value=details.healing||{}; topology.value=t
  }catch(e){if(e.name!=='AbortError')error.value=`无法读取意图控制面：${e.message}`}
  finally{if(detailRequest===controller)loading.value=false}
}
async function act(fn){busy.value=true;try{await fn();await load()}catch(e){error.value=e.message}finally{busy.value=false}}
const reevaluate=()=>act(()=>api.reevaluateIntent(intentId.value));const replan=()=>act(()=>api.replanIntent(intentId.value,{deploy:true}));const withdraw=()=>act(()=>api.withdrawIntent(intentId.value))
const assuranceSummary=computed(()=>assurance.value.current?.satisfied===true?'SATISFIED':assurance.value.current?.satisfied===false?'VIOLATED':'UNKNOWN')
const serviceLabel=computed(()=>spec.value.service?.service_ids?.join(', ')||compact(spec.value.service||{}));const reversedHistory=computed(()=>[...(history.value.items||[])].reverse())
const selectedPath=computed(()=>{if(runtime.value.current_path?.length)return runtime.value.current_path;const active=plan.value.active||{};return (active.candidates||[]).find(c=>c.candidate_id===active.selected_candidate_id)?.path||[]})
const healingView=computed(()=>buildHealingPresentation({items:healing.value.items||[],currentPath:selectedPath.value,lifecycle:runtime.value.lifecycle,llmRuntimeMode:spec.value.llm_runtime_mode}))
const infrastructureNotice=computed(()=>infrastructurePresentation(runtime.value.lifecycle, decisions.value.items?.[0]?.decision_json?.reason_detail || decisions.value.items?.[0]?.reason_detail || {}))
const decisionView=computed(()=>{
  const report=decisionReport.value||{}
  const admission=report.admission||report.capacity_admission||{}
  const executionResult=report.execution_result||report.execution||{}
  const planned=report.planned_actions||report.final_action?.selected_actions||[]
  const actual=executionResult.actual_actions||executionResult.actions_applied||report.actual_actions||[]
  const southbound=report.southbound_execution||executionResult.southbound_execution||executionResult.status||'NOT_EXECUTED'
  const finalStatus=report.final_status||runtime.value.lifecycle||record.value.status||'PENDING'
  const stopCategory=report.stop_category||admission.stop_category||'NOT_APPLICABLE'
  const finalReason=report.final_reason_code||admission.final_reason_code||report.reason_code||report.summary||'NOT_AVAILABLE'
  const notExecuted=String(southbound).toUpperCase()==='NOT_EXECUTED' || ['UNSATISFIABLE','FAILED'].includes(String(finalStatus).toUpperCase()) && !actual.length
  return {finalStatus,stopCategory,finalReason,southbound,plannedActions:Array.isArray(planned)?planned:[planned].filter(Boolean),actualActions:Array.isArray(actual)?actual:[actual].filter(Boolean),notExecuted}
})
const normalizedTopology=computed(()=>topology.value.frontend||topology.value||{})
const topologyNodes=computed(()=>{const raw=normalizedTopology.value.nodes||[];const count=Math.max(raw.length,1);return raw.map((n,i)=>{const angle=(Math.PI*2*i/count)-Math.PI/2;const id=String(n.id||n.name||n.switch_name||`node-${i}`);const label=String(n.name||n.switch_name||id);return{id,label,x:400+Math.cos(angle)*300,y:150+Math.sin(angle)*105,active:selectedPath.value.includes(id)||selectedPath.value.includes(label)}})})
const topologyLinks=computed(()=>{const map=new Map(topologyNodes.value.map(n=>[n.id,n]));return (normalizedTopology.value.links||[]).map((l,i)=>{const ends=l.endpoints||[];const aid=String(l.from||l.source||ends[0]?.node||ends[0]||'');const bid=String(l.to||l.target||ends[1]?.node||ends[1]||'');const a=map.get(aid),b=map.get(bid);if(!a||!b)return null;const pairs=selectedPath.value.slice(0,-1).map((v,j)=>new Set([String(v),String(selectedPath.value[j+1])]));return{id:l.id||`${aid}-${bid}-${i}`,a,b,active:pairs.some(p=>p.has(a.id)&&p.has(b.id))}}).filter(Boolean)})
function endpoint(v){return v?.ip||v?.name||compact(v||{})}function compact(v){const s=JSON.stringify(v,null,0);return s.length>280?`${s.slice(0,280)}…`:s}function percent(v){return v==null?'-':`${Math.round(Number(v)*100)}%`}function operator(v){return({GTE:'≥',LTE:'≤',EQ:'='})[v]||v}function formatTime(v){return v?new Date(v).toLocaleString('zh-CN',{hour12:false}):'-'}function tone(v){if(v==='SATISFIED')return'ok';if(['VIOLATED','FAILED','UNSATISFIABLE','WAITING_FOR_INFRASTRUCTURE'].includes(v))return'bad';if(['DEGRADED','NEEDS_CLARIFICATION','NEEDS_APPROVAL'].includes(v))return'warn';if(v==='WITHDRAWN')return'muted';return'run'}function assessmentTone(v){return v==='PASS'?'ok':v==='FAIL'?'bad':'warn'}function symbol(v){return v==='PASS'?'✓':v==='FAIL'?'!':'?'}
onMounted(load)
watch(intentId, load)
onBeforeUnmount(()=>detailRequest?.abort())
</script>

<style scoped>
*{box-sizing:border-box}.detail-shell{min-height:100vh;display:grid;grid-template-columns:225px 1fr;background:linear-gradient(140deg,#f6fbff,#f5f3ff);color:#18233b}.detail-nav{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;padding:24px 17px;border-right:1px solid #dce8f1;background:rgba(250,253,255,.9)}.brand{display:flex;gap:10px;align-items:center;margin-bottom:30px}.brand>span{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:linear-gradient(135deg,#1689ff,#705ff3);color:#fff;font-weight:900}.brand div{display:grid}.brand small{font-size:10px;color:#8795aa}.detail-nav>button{border:0;border-radius:10px;padding:11px;background:#edf6fc;color:#3979a5;text-align:left;font-weight:700;cursor:pointer}.detail-nav nav{display:grid;gap:6px;margin-top:25px}.detail-nav nav a{padding:10px;color:#687990;text-decoration:none;font-size:12px;font-weight:750;border-left:2px solid #dce8f1}.detail-nav nav a:hover{color:#1689e6;border-color:#1689e6}.detail-nav .diagnostic{margin-top:auto}.detail-main{padding:28px clamp(22px,4vw,58px);overflow:auto}.state-card{padding:30px;border-radius:16px;background:white}.state-card.error{color:#c33}.state-card button{margin-left:10px}.detail-head{display:flex;justify-content:space-between;gap:30px;align-items:flex-start;margin-bottom:20px}.detail-head p,.panel-title p{margin:0;color:#6d819a;font:800 10px ui-monospace;letter-spacing:1.6px}.detail-head h1{max-width:900px;margin:6px 0 10px;font-size:25px}.meta{display:flex;flex-wrap:wrap;gap:7px}.meta span,.planning-meta span{padding:6px 8px;border-radius:999px;background:#eaf3fa;color:#60738b;font:700 10px ui-monospace}.head-actions{display:flex;gap:7px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.head-actions button{border:1px solid #ccdfec;border-radius:9px;padding:8px 10px;background:white;color:#347da9;font-size:11px;font-weight:800;cursor:pointer}.head-actions .danger{color:#c74747}.lifecycle{padding:8px 11px;border-radius:999px;font:900 10px ui-monospace}.ok{background:#dcf8ec!important;color:#087e59!important}.bad{background:#ffe3e3!important;color:#c83a3a!important}.warn{background:#fff0d3!important;color:#9c680e!important}.run{background:#e1f2ff!important;color:#1479b9!important}.muted{background:#edf0f3!important;color:#747f8d!important}.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}.summary-grid article,.execution-grid article,.healing-grid article,.decision-grid article{display:grid;padding:15px;border:1px solid #d9e6ef;border-radius:14px;background:rgba(255,255,255,.8)}.summary-grid span,.execution-grid span,.healing-grid span,.decision-grid span{font-size:10px;color:#72839a}.summary-grid strong,.execution-grid strong,.healing-grid strong,.decision-grid strong{margin:6px 0;font:850 13px ui-monospace;overflow-wrap:anywhere}.summary-grid small,.execution-grid small,.healing-grid small{color:#8b9aab;font-size:10px}.panel{margin-bottom:14px;padding:22px;border:1px solid #d6e4ee;border-radius:18px;background:rgba(255,255,255,.82);box-shadow:0 12px 30px rgba(61,86,119,.07);scroll-margin-top:15px}.panel-title{display:flex;align-items:center;gap:12px;margin-bottom:17px}.panel-title>span{display:grid;place-items:center;width:33px;height:33px;border-radius:10px;background:#e1f1ff;color:#167fca;font:900 11px ui-monospace}.panel-title h2{margin:3px 0 0;font-size:17px}.decision-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}.not-executed{margin:12px 0 0;padding:10px;border-radius:10px;background:#fff4e5;color:#9a5d00;font-size:11px;font-weight:750}.action-evidence{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:10px}.action-evidence div{display:grid;gap:4px;padding:10px;border-radius:10px;background:#f5f9fc}.action-evidence b{font-size:10px;color:#477d9f}.action-evidence span{font:700 10px ui-monospace;overflow-wrap:anywhere}.understanding-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:12px}.raw,dl{margin:0;padding:15px;border-radius:13px;background:#f5f9fc}.raw>span,dt{font-size:10px;color:#74879d;font-weight:800}.raw p{font-weight:650;line-height:1.55}.raw small{color:#7589a0}dl{display:grid;grid-template-columns:1fr 1fr;gap:12px}dl div{min-width:0}dd{margin:4px 0 0;font:700 11px ui-monospace;overflow-wrap:anywhere}.objective-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:9px;margin-top:12px}.objective-grid article{display:grid;padding:13px;border:1px solid #d9e8f1;border-radius:12px}.objective-grid span{font:800 10px ui-monospace;color:#4488b8}.objective-grid strong{margin:7px 0}.objective-grid small{font-size:9px;color:#8798aa}.notice{display:flex;gap:10px;margin-top:12px;padding:12px;border-radius:10px;font-size:11px}.planning-meta{display:flex;gap:7px;flex-wrap:wrap}.candidate-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:9px;margin:13px 0}.candidate-list article{display:grid;padding:13px;border:1px solid #dce7ef;border-radius:12px;background:#fafcff}.candidate-list article.selected{border-color:#39a9ed;box-shadow:inset 0 0 0 1px #aee0ff}.candidate-list div{display:flex;justify-content:space-between}.candidate-list span,.candidate-list small{font:650 9px ui-monospace;color:#8291a4}.candidate-list b{font:850 9px ui-monospace;color:#1687ce}.candidate-list strong{margin:8px 0;font-size:11px}.topology-card{padding:14px;border-radius:14px;background:#f5f9fd}.topology-head{display:flex;justify-content:space-between}.topology-head small{color:#8292a6}.topology-card svg{width:100%;height:270px;margin-top:8px}.topology-card line{stroke:#cbd9e5;stroke-width:2}.topology-card line.active{stroke:#1ea7f2;stroke-width:6;filter:drop-shadow(0 0 5px #59c4ff)}.topology-card g circle{fill:#eef5fa;stroke:#9fb4c8;stroke-width:2}.topology-card g.active circle{fill:#28b4f2;stroke:#fff;stroke-width:4;filter:drop-shadow(0 0 7px #55c6f8)}.topology-card text{text-anchor:middle;font:700 10px ui-monospace;fill:#45576d}.topology-card g.active text{fill:#087aae}.topology-empty,.empty{padding:25px;text-align:center;color:#8191a4}.layer-flow{display:flex;align-items:center;justify-content:center;gap:9px;padding:13px;border-radius:12px;background:#f2f8fc}.layer-flow span{padding:8px;border-radius:8px;background:#fff;border:1px solid #d4e4ef;font:800 10px ui-monospace}.layer-flow b{color:#8ea5b8}.execution-grid,.healing-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:12px}.assurance-head{display:flex;gap:12px;align-items:center;padding-bottom:12px}.assurance-head small{color:#687b91}.assessment-list{display:grid}.assessment-list article{display:grid;grid-template-columns:30px 1fr 70px;gap:10px;align-items:center;padding:12px 5px;border-top:1px solid #e7edf2}.assessment-list i{display:grid;place-items:center;width:25px;height:25px;border-radius:50%;font-style:normal;font-weight:900}.assessment-list div{display:grid}.assessment-list small{color:#7c8c9f;margin-top:3px}.assessment-list>article>span{justify-self:end;padding:5px 8px;border-radius:999px;font:850 9px ui-monospace}details{margin-top:12px}summary{cursor:pointer;color:#477d9f;font-size:11px;font-weight:800}pre{max-height:220px;overflow:auto;padding:12px;border-radius:10px;background:#101827;color:#cce4f5;font-size:9px;white-space:pre-wrap}.healing-grid{grid-template-columns:repeat(3,1fr)}.timeline{margin:16px 0 0;padding:0;list-style:none}.timeline li{display:grid;grid-template-columns:12px 1fr;gap:12px;padding:0 0 16px}.timeline li>i{width:9px;height:9px;margin-top:5px;border-radius:50%}.timeline li div{display:grid;border-left:1px solid #d8e5ee;padding-left:13px}.timeline li span{font-size:11px;color:#5e7188;margin:3px 0}.timeline li small{color:#8b99aa;font-size:9px}@media(max-width:980px){.detail-shell{grid-template-columns:1fr}.detail-nav{display:none}.summary-grid,.decision-grid{grid-template-columns:repeat(2,1fr)}.understanding-grid{grid-template-columns:1fr}.execution-grid,.healing-grid,.action-evidence{grid-template-columns:1fr 1fr}.detail-head{display:grid}}
.healing-grid{grid-template-columns:repeat(4,1fr)}
.healing-candidates{display:grid;grid-template-columns:repeat(auto-fit,minmax(245px,1fr));gap:9px;margin-top:14px}
.healing-candidates article{display:grid;gap:7px;padding:13px;border:1px solid #dce7ef;border-radius:12px;background:#fafcff}
.healing-candidates article.applicable{border-color:#65c5a5;background:#f1fcf8}
.healing-candidates article.redundant,.healing-candidates article.no_expected_effect,.healing-candidates article.not_applicable,.healing-candidates article.unsafe{border-color:#f0c6c6;background:#fff8f8}
.healing-candidates header{display:flex;justify-content:space-between;gap:8px}
.healing-candidates header span,.healing-candidates header b{font:800 9px ui-monospace;color:#60758d}
.healing-candidates strong{font:900 11px ui-monospace}
.healing-candidates small{color:#5f7188}
  .healing-candidates em{font:650 9px ui-monospace;color:#8796a8;font-style:normal}.healing-executions{display:grid;gap:6px;margin-top:12px;padding:12px;border:1px solid #cce9dc;border-radius:12px;background:#f4fcf8}.healing-executions strong{font-size:11px}.healing-executions small{font:650 9px ui-monospace;color:#47715d}.technical-timeline{margin-top:16px}
</style>
