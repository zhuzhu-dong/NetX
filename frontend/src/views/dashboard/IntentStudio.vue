<template>
  <section class="intent-page">
    <div class="panel composer-panel">
      <IntentComposer
        v-model="intent.intentText"
        v-model:priority="intent.priority"
        :priorities="priorities"
        :templates="intent.intentTemplates"
        @focus-change="intent.isComposing = $event"
        @submit="runIntent"
        @apply-template="intent.applyTemplate"
      />

      <div class="parse-panel">
        <div class="section-head">
          <p>LLM + Rule Parse</p>
          <h2>结构化意图解析</h2>
        </div>
        <div class="raw-intent">
          <span>原始自然语言意图</span>
          <strong>{{ activeIntent?.text || intent.intentText || '等待输入意图' }}</strong>
        </div>
        <dl class="parse-grid">
          <template v-for="row in parseRows" :key="row.label">
            <dt>{{ row.label }}</dt>
            <dd>{{ row.value || '-' }}</dd>
          </template>
        </dl>
        <div class="contract-note">
          <b>Prompt Contract</b>
          <span>LLM 只输出结构化 IntentPlan；IP、端口、协议、带宽等硬字段由规则抽取器和 Schema Validator 兜底，不能直接生成 ONOS FlowRule、P4Runtime WriteRequest 或 shell 命令。</span>
        </div>
      </div>
    </div>

    <aside class="panel history-panel">
      <div class="section-head">
        <p>Intent History</p>
        <h2>历史意图</h2>
      </div>

      <IntentQueue
        :intents="intent.intentHistory"
        :selected-intent-id="intent.selectedIntentId"
        @select-intent="intent.selectIntent"
        @delete-intent="intent.deleteIntent"
      />
    </aside>
  </section>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useIntentStore } from '../../stores/intentStore'
import { formatMbps, formatPercent } from '../../composables/useFormatters'
import IntentComposer from '../../components/dashboard/IntentComposer.vue'
import IntentQueue from '../../components/dashboard/IntentQueue.vue'

const router = useRouter()
const intent = useIntentStore()
const priorities = ['Low', 'Medium', 'High', 'Critical']
const activeIntent = computed(() => intent.activeIntent)
const decisionReport = computed(() => activeIntent.value?.result?.decision_report || {})
const understanding = computed(() => (
  decisionReport.value.intent_understanding
  || activeIntent.value?.snapshot?.intent_understanding
  || activeIntent.value?.snapshot?.parsed_intent
  || {}
))
const clarificationQuestion = computed(() => (
  activeIntent.value?.snapshot?.clarification_question
  || intent.activeClarificationQuestion
  || ''
))
const parseRows = computed(() => [
  { label: '业务对象', value: understanding.value.business_object || understanding.value.service || '' },
  { label: '源地址', value: understanding.value.source_ip || understanding.value.source || '' },
  { label: '目的地址', value: understanding.value.destination_ip || understanding.value.destination || '' },
  { label: '协议 / 端口', value: [understanding.value.protocol, understanding.value.destination_port || understanding.value.port].filter(Boolean).join(' / ') },
  { label: '动作类型', value: understanding.value.action_type || '' },
  { label: '带宽目标', value: formatMbps(understanding.value.bandwidth_target_mbps || understanding.value.bandwidth_mbps) },
  { label: '置信度', value: formatPercent(understanding.value.confidence_score || activeIntent.value?.snapshot?.confidence_score) },
  { label: '澄清状态', value: clarificationQuestion.value ? `需要澄清：${clarificationQuestion.value}` : '可执行 / 无需澄清' },
])

onMounted(() => {
  intent.startNewIntent()
  intent.loadIntents()
})

async function runIntent() {
  const intent = await intent.submitIntent()
  if (intent) router.push({ path: '/dashboard/run', query: { intent_id: intent.id } })
}
</script>

<style scoped>
.intent-page {
  display: grid;
  grid-template-columns: minmax(520px, 1fr) minmax(360px, 0.74fr);
  gap: 18px;
  height: 100%;
}

.composer-panel {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 14px;
  overflow: auto;
}

.composer-panel :deep(.intent-composer) {
  width: 100%;
}

.composer-panel :deep(textarea) {
  min-height: 190px;
}

.history-panel {
  overflow: hidden;
}

.section-head {
  margin-bottom: 12px;
}

p,
h2 {
  margin: 0;
}

p {
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
  font-size: 1.08rem;
}

.parse-panel {
  padding: 15px;
  border: 1px solid rgba(186, 230, 253, 0.72);
  border-radius: 15px;
  background: rgba(240, 249, 255, 0.54);
}

.raw-intent {
  padding: 12px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.72);
}

.raw-intent span,
dt {
  color: #64748b;
  font-size: 0.74rem;
  font-weight: 850;
}

.raw-intent strong {
  display: block;
  margin-top: 7px;
  color: #0f172a;
  font-size: 0.88rem;
  line-height: 1.6;
}

.parse-grid {
  display: grid;
  grid-template-columns: 108px 1fr;
  gap: 8px 12px;
  margin: 12px 0 0;
}

dd {
  margin: 0;
  color: #0f172a;
  font-size: 0.8rem;
  font-weight: 850;
}

.contract-note {
  display: grid;
  gap: 5px;
  margin-top: 12px;
  padding: 12px;
  border-radius: 12px;
  color: #075985;
  background: rgba(224, 242, 254, 0.76);
}

.contract-note b,
.contract-note span {
  font-size: 0.75rem;
  line-height: 1.55;
}

.history-panel :deep(.intent-list) {
  grid-template-columns: 1fr;
  max-height: calc(100vh - 285px);
  overflow: auto;
  padding-right: 4px;
}

@media (max-width: 980px) {
  .intent-page {
    grid-template-columns: 1fr;
    height: auto;
  }
}
</style>
