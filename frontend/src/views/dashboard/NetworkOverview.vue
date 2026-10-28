<template>
  <section class="chatops-page" :class="{ 'quick-returning': quickReturnPending }">

    <main
      class="topology-workbench"
      :class="{
        'conversation-engaged': conversationEngaged,
        'conversation-empty': !conversationEngaged,
      }"
    >
      <section class="workspace-card">
        <div class="workspace-head">
          <div class="workspace-title">
            <span>Intent Conversation</span>
            <strong>{{ activeWorkspaceTitle }}</strong>
          </div>
        </div>

        <div class="workspace-stage">
          <div class="conversation-view">
            <div v-if="!network.activeIntent" class="conversation-empty-state">
              <p>NETPULSE AUTONOMOUS NETWORK</p>
              <h2>今天想让网络完成什么？</h2>
              <span>描述目标、作用范围与约束条件，系统将完成感知、编排和闭环验证。</span>
            </div>

            <div v-else-if="historyConversationLoading" class="conversation-loading" role="status" aria-live="polite">
              <span class="conversation-loading-spinner" aria-hidden="true"></span>
              <strong>正在加载对话</strong>
              <p>正在同步历史消息与意图执行结果</p>
            </div>

            <div v-else ref="conversationScrollEl" class="conversation-scroll">
              <template v-for="thread in (isPureChatActive ? pureChatThreads : conversationThreads)" :key="thread.id">
                <article v-if="thread.role === 'user'" class="chat-thread agent-thread-row user-thread-row">
                  <section class="agent-thread user-plain-thread">
                    <div class="agent-step-list">
                      <section class="agent-step user-plain-step">
                        <i class="step-dot"></i>
                        <div class="step-content">
                          <div class="thread-meta">
                            <span>你</span>
                            <em>{{ thread.messages[0]?.tag || 'operator' }}</em>
                          </div>
                          <p>{{ thread.messages[0]?.content }}</p>
                        </div>
                      </section>
                    </div>
                  </section>
                </article>

                <article v-else class="chat-thread agent-thread-row">
                  <section class="agent-thread">
                    <div class="agent-step-list">
                      <section
                        v-for="(step, index) in displayedThreadMessages(thread)"
                        :key="step.id"
                        class="agent-step"
                        :class="{ final: index === displayedThreadMessages(thread).length - 1 && (step.action || step.report) }"
                      >
                        <i class="step-dot"></i>
                        <div class="step-content">
                          <div class="thread-meta">
                            <span>{{ step.tag || `step-${index + 1}` }}</span>
                          </div>
                          <p v-if="isThinkingStep(step)" class="chat-waiting-line compact">
                            <span class="chat-waiting-spinner" aria-hidden="true"></span>
                            <span>{{ thinkingStepLabel(step) }}</span>
                          </p>
                          <p v-else-if="step.pending && !step.content" class="chat-waiting-line">
                            <span class="chat-waiting-spinner" aria-hidden="true"></span>
                            <span>思考中</span>
                          </p>
                          <p v-else>{{ step.content }}</p>

                          <div v-if="step.action === 'topology'" class="agent-result-actions">
                            <button
                              type="button"
                              class="inline-action"
                              @click="openNetworkView"
                            >
                              查看拓扑态势
                            </button>
                          </div>

                        </div>
                      </section>
                    </div>

                    <div v-if="hasCollapsedThoughts(thread)" class="thought-fold">
                      <button type="button" class="thought-toggle" @click="toggleThoughtThread(thread.id)">
                        <span>{{ isThoughtThreadExpanded(thread.id) ? '收起执行过程' : '查看执行过程' }}</span>
                        <em>{{ collapsedThreadMessages(thread).length }} 个阶段</em>
                      </button>

                      <div v-if="isThoughtThreadExpanded(thread.id)" class="thought-details">
                        <section v-for="item in collapsedThreadMessages(thread)" :key="item.id" class="thought-step">
                          <i></i>
                          <div>
                            <span>{{ item.tag || 'agent' }}</span>
                            <p>{{ item.content }}</p>
                          </div>
                        </section>
                      </div>
                    </div>

                    <div
                      v-for="presentation in (thread.finalPresentations || [])"
                      :key="`summary-${presentation.intentId}`"
                      class="agent-step-summary thread-summary"
                    >
                      <ExecutionSummaryCard :summary="presentation.executionSummary" />
                    </div>
                  </section>
                </article>
              </template>

            </div>

            <!--
              永久隐藏：对话底部流水线阶段芯片（Sense/Parse/Check/Plan/Act/Verify）。
              该组件占据对话底部整行空间，视觉干扰大；流水线进度已通过对话消息内的
              执行报告卡片体现，此处属于冗余展示。如需恢复，取消下方注释即可。
            -->
            <!--
            <div v-if="!isPureChatActive && pipelineHasProgress" class="execution-strip">
              <div class="pipeline-progress">
                <span class="pipeline-progress-bar" :style="{ width: pipelineProgressPercent + '%' }"></span>
              </div>
              <div class="pipeline-track">
                <template v-for="(stage, idx) in network.pipelineStages" :key="stage.key">
                  <button
                    type="button"
                    class="stage-chip"
                    :class="stage.state"
                    @click="network.selectStage(stage.key)"
                  >
                    <span class="stage-key">{{ stage.key }}</span>
                    <span class="stage-name">{{ stage.name }}</span>
                  </button>
                  <span
                    v-if="idx < network.pipelineStages.length - 1"
                    class="stage-arrow"
                    :class="arrowState(stage, network.pipelineStages[idx + 1])"
                  >→</span>
                </template>
              </div>
            </div>
            -->
          </div>

        </div>
      </section>

      <form class="intent-composer" :class="{ active: network.isComposing, locked: network.isComposerLocked }" @submit.prevent="runIntent">
        <div v-if="previewResult" class="preview-result" role="status">
          <strong>预览 / 尚未执行</strong>
          <span>{{ previewSummary }}</span>
          <small>预览只读取语义、规划和准入信息；未创建 Policy、Binding、ONOS、P4Runtime 或 TC 配置。</small>
        </div>
        <div class="input-row">
          <textarea
            v-model="network.intentText"
            :placeholder="composerPlaceholder"
            rows="2"
            :disabled="network.isComposerLocked"
            @focus="network.isComposing = true"
            @blur="network.isComposing = false"
            @keydown.enter.exact.prevent="runIntent"
          ></textarea>

          <div class="input-actions">
            <button class="preview-btn" type="button" :disabled="previewLoading || network.isComposerLocked || !network.intentText.trim()" @click="previewIntentOnly">
              {{ previewLoading ? '预览中' : '预览' }}
            </button>
            <button class="submit-btn" type="submit" :disabled="network.isComposerLocked || !network.intentText.trim()">
              {{ network.interactionMode === network.INTERACTION_MODE.AWAITING_INPUT ? '发送补充信息' : (network.interactionMode === network.INTERACTION_MODE.ACTIVE ? '运行中' : '运行') }}
            </button>
            <button
              v-if="network.cancellableRuntimeId"
              class="cancel-btn"
              type="button"
              :disabled="network.isCancellingActiveIntent"
              @click="stopActiveExecution"
            >
              {{ network.isCancellingActiveIntent ? '正在终止...' : '终止执行' }}
            </button>
          </div>
        </div>
      </form>

      <Transition name="capability-fade">
        <div v-if="!network.activeIntent && !conversationStarting" class="empty-capabilities" aria-label="系统能力">
          <div class="empty-capability">
            <span class="capability-symbol sense-symbol" aria-hidden="true"><i></i></span>
            <span><strong>网络感知</strong><small>识别拓扑、链路与遥测变化</small></span>
          </div>
          <div class="empty-capability">
            <span class="capability-symbol intent-symbol" aria-hidden="true"><i></i></span>
            <span><strong>意图编排</strong><small>将自然语言转化为执行策略</small></span>
          </div>
          <div class="empty-capability">
            <span class="capability-symbol verify-symbol" aria-hidden="true"><i></i></span>
            <span><strong>闭环验证</strong><small>验证执行结果并反馈网络状态</small></span>
          </div>
        </div>
      </Transition>
    </main>
  </section>
</template>

<script setup>
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useNetworkStore } from '../../stores/network'
import { previewIntent } from '../../api/client'
import TopologyMap from '../../components/dashboard/TopologyMap.vue'
import TopologyHealthDonut from '../../components/dashboard/TopologyHealthDonut.vue'
import IntProbeCard from '../../components/dashboard/IntProbeCard.vue'
import LineAreaChart from '../../components/charts/LineAreaChart.vue'
import ExecutionSummaryCard from '../../components/dashboard/ExecutionSummaryCard.vue'
import { buildGeneratedMessages, mergeConversationMessages } from '../../composables/useReportBuilder'
import { toLineAreaOption } from '../../composables/useChartData'
import * as echarts from 'echarts'
import { formatMbps, formatMs, normalizePercent, asArray } from '../../composables/useFormatters'
import { uniqueAgents, getRuntimeTimeline } from '../../composables/useDataExtractors'
import { WELCOME_MESSAGES } from '../../constants'

const router = useRouter()
const network = useNetworkStore()
const sidebarCollapsed = ref(false)
const workspaceMode = ref('chat')
const topologyViewKey = ref(0)
const welcomeMessage = WELCOME_MESSAGES[Math.floor(Math.random() * WELCOME_MESSAGES.length)]
const topologyDetailVisible = ref(false)
const quickReturnPending = ref(false)
const selectedSystemPanel = ref('overview')
const telemetryBarHost = ref(null)
let telemetryBarChart = null
const expandedThoughtThreads = ref(new Set())
const conversationScrollEl = ref(null)
const typedConversationMessages = ref([])
const typingTimer = ref(null)
const suppressNextTypewriter = ref(false)
const historyViewLockedIds = ref(new Set())
const animatedConversationIds = ref(new Set())
const typedConversationOwnerKey = ref('')
const accountMenuOpen = ref(false)
const modelMenuOpen = ref(false)
const previewLoading = ref(false)
const previewResult = ref(null)
const isEditingName = ref(false)
const accountAreaRef = ref(null)
const profileFileInput = ref(null)
const nameInputEl = ref(null)

const conversationTitles = ref(readConversationTitles())
const editingHistoryId = ref('')
const historyTitleDraft = ref('')
const userSession = ref({ uid: 'OPERATOR' })
const userProfile = reactive({ uid: 'OPERATOR', name: '网络运维员', avatar: '' })
const profileDraft = reactive({
  name: userProfile.name,
  avatar: userProfile.avatar,
})

const modelOptions = [{ id: 'server-managed', name: '服务端 Real LLM', desc: '模型与凭据由服务端管理' }]
const selectedModel = ref('server-managed')

const userUid = computed(() => userSession.value?.email || 'anonymous')
const displayName = computed(() => userProfile.name?.trim() || '未命名')
const selectedModelLabel = computed(() => modelOptions.find((item) => item.id === selectedModel.value)?.name || '服务端 Real LLM')
const previewSummary = computed(() => {
  const result = previewResult.value || {}
  return result.assistant_message || result.summary || result.message || result.status || '后端已返回预览结果'
})
const profileInitial = computed(() => displayName.value.slice(0, 1).toUpperCase() || 'N')
const draftInitial = computed(() => (profileDraft.name?.trim() || '未命名').slice(0, 1).toUpperCase() || 'N')

const isRunning = computed(() => {
  const active = network.activeIntent
  if (!active) return false

  // Block a second submission while /chat is still classifying the current
  // turn. Otherwise two turns can share and overwrite transient runtime state.
  if (active.pendingTurnId) return true

  const id = String(active.id || '')
  const result = active.result || {}

  // 普通聊天 CHAT-* 不能锁输入框，也不能显示“运行中”。
  if (
    id.startsWith('CHAT-')
    || result.chat_only
    || result.intent_detected === false
  ) {
    return false
  }

  const statusClass = String(active.statusClass || '').toLowerCase()
  const status = String(active.status || '').toLowerCase()

  const terminalStates = ['success', 'failed', 'error', 'cancelled', 'canceled', 'completed', 'done']
  if (terminalStates.includes(statusClass) || terminalStates.includes(status)) return false

  const runningStates = [
    'pending',
    'queued',
    'planning',
    'running',
    'executing',
    'sensing',
    'thinking',
    'checking',
    'acting',
    'verifying',
    'pending_approval',
  ]

  return runningStates.includes(statusClass) || runningStates.includes(status)
})

const activeWorkspaceTitle = computed(() => {
  return network.activeIntent ? displayHistoryTitle(network.activeIntent) : '自然语言网络意图助手'
})

const historyConversationLoading = computed(() => Boolean(network.activeIntent?._historyLoading))
const conversationStarting = ref(false)
const conversationEngaged = computed(() => Boolean(conversationStarting.value || (network.activeIntent && !historyConversationLoading.value)))

function isPureChatRecord(record) {
  if (!record) return false
  const result = record.result || {}
  const id = String(record.id || '')
  const status = String(record.status || '')
  const hasIntentRuntime = Boolean(
    record.activeRunId
    || (record.runIds || []).length
    || (record.milestones || []).length
    || getRuntimeTimeline(record).length
    || result.intent_id
    || result.intentId
    || (result.intent_detected === true && !result.chat_only)
  )

  if (hasIntentRuntime) return false

  return Boolean(
    result.chat_only
    || result.intent_detected === false
    || id.startsWith('CHAT-')
    || status === 'chatting'
    || (status === 'success' && result.chat_only)
  )
}

function conversationIdentityKeys(record) {
  if (!record) return []
  const keys = new Set()
  const add = (value) => {
    if (value !== undefined && value !== null && String(value).trim()) {
      keys.add(String(value))
    }
  }

  add(record.id)
  add(record.sessionId)
  add(record.session_id)
  add(record.activeRunId)
  add(record.intentId)
  add(record.intent_id)
  ;(record.runIds || []).forEach(add)
  return [...keys]
}

function primaryConversationKey(record) {
  return conversationIdentityKeys(record)[0] || ''
}

function markConversationSet(setRef, record) {
  const next = new Set(setRef.value)
  conversationIdentityKeys(record).forEach((key) => next.add(key))
  setRef.value = next
}

function unmarkConversationSet(setRef, record) {
  const next = new Set(setRef.value)
  conversationIdentityKeys(record).forEach((key) => next.delete(key))
  setRef.value = next
}

function isConversationMarked(setRef, record) {
  return conversationIdentityKeys(record).some((key) => setRef.value.has(key))
}

const isPureChatActive = computed(() => isPureChatRecord(network.activeIntent))

const pipelineHasProgress = computed(() => {
  const stages = network.pipelineStages || []
  return stages.some(s => s.state !== 'waiting')
})

const pipelineProgressPercent = computed(() => {
  const stages = network.pipelineStages || []
  if (!stages.length) return 0
  const done = stages.filter(s => s.state === 'complete' || s.state === 'failed').length
  return Math.round((done / stages.length) * 100)
})

function arrowState(prev, next) {
  if (prev.state === 'complete' && next.state === 'running') return 'active'
  if (prev.state === 'complete' && next.state === 'complete') return 'done'
  return ''
}

function isIntentAcceptedPlaceholder(message) {
  const content = String(message?.content || '')
  return content === '▷ 等待后台处理中…' || content === '执行中...'
}

function messageIntentId(message = {}) {
  return message?.intentId
    || message?.intent_id
    || message?.report?.intent_id
    || message?.report?.intentId
    || message?.report?.decision_report?.intent_id
    || message?.report?.decision_report?.intentId
    || ''
}

function isTerminalOutcomeMessage(message = {}) {
  const content = normalizeMessageContent(message?.content)
  const type = normalizeMessageTag(message.messageType || message.message_type)
  return Boolean(
    message?.report
    || message?.action === 'topology'
    || type === 'execution_result'
    || content.startsWith('执行失败')
    || content.includes('意图已执行')
    || content.includes('执行完成')
    || content.includes('最终结论')
  )
}

function threadIntentId(thread) {
  const messages = thread?.messages || []
  const pendingTurnId = network.activeIntent?.pendingTurnId
  if (pendingTurnId && messages.some((message) => message?.turnId === pendingTurnId)) {
    return ''
  }

  const presentations = network.activeIntent?.finalPresentation || {}
  const exactFrozenIntent = Object.entries(presentations).find(([, presentation]) => (
    presentation?.finalMessageId
    && messages.some((message) => String(message?.id || '') === String(presentation.finalMessageId))
  ))?.[0]
  if (exactFrozenIntent) return exactFrozenIntent

  const withIntent = [...messages].reverse().find((message) => messageIntentId(message))
  const explicitIntentId = messageIntentId(withIntent)
  if (explicitIntentId) return explicitIntentId

  if (!threadHasRuntimeSignal(thread)) return ''

  const active = network.activeIntent
  if (!active || isPureChatRecord(active)) return ''
  const ids = active.runIds || []
  if (ids.length === 1) return ids[0]
  return ''
}

function threadHasRuntimeSignal(thread) {
  const messages = thread?.messages || []
  return messages.some((message) => {
    const tag = normalizeMessageTag(message.tag)
    const type = inferDisplayMessageType(message)
    return Boolean(
      message?.intentId
      || message?.intent_id
      || messageIntentId(message)
      || message?.report
      || message?.thoughtOnly
      || message?.action === 'topology'
      || message?.action === 'thinking'
      || INTERNAL_MESSAGE_TYPES.has(type)
      || INTERNAL_MESSAGE_TAGS.has(tag)
    )
  })
}

function intentRuntimeInfo(intentId) {
  if (!intentId) return null
  const active = network.activeIntent

  const frozen = finalPresentationForIntent(intentId)
  if (frozen) {
    return {
      id: intentId,
      status: frozen.status,
      statusClass: frozen.statusClass,
      result: frozen.result || null,
      milestones: frozen.milestones || [],
      snapshot: frozen.snapshot || null,
      executionSummary: frozen.executionSummary || null,
      stages: frozen.stages || [],
      finalReply: frozen.finalReply || '',
      finalMessageId: frozen.finalMessageId || '',
      text: frozen.text || active?.text || active?.content || '',
      _frozen: true,
    }
  }

  const cached = active?.intentResults?.[intentId]
  if (cached) return cached

  if (active?.activeRunId === intentId || active?.id === intentId) {
    return {
      id: intentId,
      status: active.status,
      statusClass: active.statusClass,
      result: active.result || null,
      milestones: active.milestones || [],
      snapshot: active.snapshot || null,
      text: active.text || active.content || '',
    }
  }

  return null
}

function isRuntimeTerminal(runtime) {
  const state = normalizeMessageTag(`${runtime?.statusClass || ''} ${runtime?.status || ''}`)
  return Boolean(
    state.includes('success')
    || state.includes('fail')
    || state.includes('error')
    || state.includes('cancel')
  )
}

function statusText(status = '', statusClass = '') {
  const state = normalizeMessageTag(statusClass || status)
  if (state.includes('success')) return '执行完成'
  if (state.includes('fail') || state.includes('error')) return '执行失败'
  if (state.includes('clarification')) return '等待补充'
  if (state.includes('approval')) return '等待确认'
  if (state.includes('cancel')) return '已取消'
  return '执行中'
}

function summaryConclusion(thread, runtime) {
  const runtimeId = runtime?.id || threadIntentId(thread)
  const clarificationAnswered = hasAnsweredCurrentClarification(runtimeId)
  const openClarification = hasOpenClarificationQuestion(runtimeId)
  const snapshotQuestion = runtime?.snapshot?.clarification_question
  if (snapshotQuestion && openClarification) {
    return `需要补充信息：${snapshotQuestion}`
  }

  const result = runtime?.result || {}
  const decision = result.decision_report || {}
  const error = result.error
  const reason = [
    decision.final_conclusion,
    decision.summary,
    result.failure_reason,
    result.error_message,
    result.message,
    typeof error === 'object' ? error.message : error,
    typeof error === 'object' ? error.detail : '',
    result.detail,
    result.details,
  ].map((item) => normalizeMessageContent(item)).find((item) => item && !['none', 'null', 'undefined'].includes(item.toLowerCase()))

  if (reason) {
    if (clarificationAnswered && !openClarification && looksLikeClarificationRequest(reason)) {
      return '执行中...'
    }

    return statusText(runtime?.status, runtime?.statusClass) === '执行失败'
      ? `执行失败：${reason}`
      : reason
  }

  const status = statusText(runtime?.status, runtime?.statusClass)
  if (status === '执行失败') return '执行失败：系统未返回明确原因，可能是拓扑不可用、参数不足、策略冲突或策略下发失败。'
  if (status === '等待补充') {
    if (clarificationAnswered && !openClarification) {
      return '执行中...'
    }

    const clarification = [...(thread?.messages || [])].reverse().find((message) => {
      const tag = normalizeMessageTag(message.tag)
      const type = inferDisplayMessageType(message)
      return message.content && (type === 'clarification_question' || tag.includes('clarification') || tag.includes('需要补充'))
    })
    return clarification?.content || '该意图需要补充关键对象、范围或约束后才能继续执行。'
  }
  if (status === '执行完成') return '本次网络意图已执行完成。'

  const visible = [...(thread?.messages || [])].reverse().find((message) => (
    !message.thoughtOnly
    && message.role !== 'user'
    && message.content
    && !isIntentAcceptedPlaceholder(message)
    && !looksLikeNetworkAck(message.content)
  ))
  if (visible?.content) return visible.content

  return '执行中...'
}

const INTERNAL_MESSAGE_TAGS = new Set([
  'intent_submitted',
  'intent',
  'assistant_thinking',
  'stage_progress',
  'approval_required',
  'execution_result',
  'verification_result',
  'system',
  'planning',
  'sensing',
  'thinking',
  'checking',
  'acting',
  'verifying',
  'completed',
  'sense',
  'parse',
  'probe',
  'plan',
  'act',
  'verify',
  'a2a',
  '执行过程',
  '执行结果',
  '深度思考',
])

const INTERNAL_MESSAGE_TYPES = new Set([
  'intent',
  'intent_submitted',
  'assistant_thinking',
  'stage_progress',
  'approval_required',
  'execution_result',
  'verification_result',
  'system',
])

function normalizeMessageTag(tag = '') {
  return String(tag || '').trim().toLowerCase()
}

function inferDisplayMessageType(message = {}) {
  const explicitType = normalizeMessageTag(message.messageType || message.message_type)
  if (explicitType) return explicitType

  const tag = normalizeMessageTag(message.tag)
  if (INTERNAL_MESSAGE_TYPES.has(tag)) return tag
  if (tag === 'clarification' || tag.includes('需要补充')) return 'clarification_question'
  return ''
}

function hasNetworkRuntime(record) {
  return Boolean(
    record?.activeRunId
    || (record?.runIds || []).length
    || (record?.milestones || []).length
    || record?.result?.intent_detected
    || record?.result?.decision_report
  )
}

function looksLikeNetworkAck(content = '') {
  const text = String(content || '')
  return [
    '已理解需求',
    '已理解你的需求',
    '收到需求',
    '理解你的需求',
    '系统即将进行',
    '系统开始',
    '即将进行意图编排',
    '开始意图编排',
    '进入智能体编排',
    '请稍候',
    '意图已识别',
    '等待后台处理中',
    '执行中',
    '正在理解意图',
    '正在执行网络意图',
  ].some((item) => text.includes(item))
}

function looksLikeClarificationRequest(content = '') {
  const text = normalizeMessageContent(content)
  return [
    '需要用户澄清',
    '需要补充信息',
    '需要补充',
    '请补充',
    '请指定',
    '请提供',
    '还需要补充',
  ].some((item) => text.includes(item))
}

function shouldKeepPinnedMessage(active, message) {
  if (!message?.content && !message?.pending) return false
  if (message.role === 'user') return true
  if (isPureChatRecord(active)) return true
  if (!hasNetworkRuntime(active)) return true
  if (message.thoughtOnly) return false

  const tag = normalizeMessageTag(message.tag)
  const type = inferDisplayMessageType(message)
  if (INTERNAL_MESSAGE_TYPES.has(type)) return false
  if (message.action === 'thinking' && normalizeMessageContent(message.content) === '执行中...') return true
  if (type === 'assistant_text') return true
  if (type === 'clarification_question' || type === 'error' || type === 'status') return true
  if (message.report || message.action === 'topology') return true
  if (tag.includes('clarification') || tag.includes('需要补充')) return true
  if (tag === 'netpulse') return true
  if (String(message.content || '').startsWith('请求失败')) return true
  if (INTERNAL_MESSAGE_TAGS.has(tag)) return false

  return false
}

const isClarificationActive = computed(() => {
  const active = network.activeIntent
  if (!active?.id) return false
  return network.interactionMode === network.INTERACTION_MODE.AWAITING_INPUT
    && network.canAnswerActiveClarification
})

const composerPlaceholder = computed(() => {
  if (network.interactionMode === network.INTERACTION_MODE.AWAITING_INPUT) {
    return '智能体需要你的补充信息，填写后将继续执行...'
  }
  return network.isGlobalIntentLocked
    ? '智能体正在执行意图，可查看历史记录，完成后恢复输入...'
    : '输入网络意图，例如：隔离异常主机并保障视频业务 SLA...'
})

const conversationMessages = computed(() => {
  const active = network.activeIntent
  if (!active) {
    return [
      {
        id: 'welcome',
        role: 'assistant',
        tag: 'ready',
        content: welcomeMessage,
        action: 'topology',
      },
    ]
  }

  const messages = [
    {
      id: `${active.id}-user`,
      role: 'user',
      tag: active.id,
      content: active.text || active.content || '新的网络意图',
    },
  ]

  if (network.activeClarificationQuestion) {
    messages.push({
      id: `${active.id}-clarification`,
      role: 'assistant',
      tag: 'clarification',
      content: network.activeClarificationQuestion,
    })
    return messages
  }

  const milestones = active.milestones || []
  if (!milestones.length) {
    if (isPureChatRecord(active)) {
      return messages.filter((message) => !isIntentAcceptedPlaceholder(message))
    }

    messages.push({
      id: `${active.id}-accepted`,
      role: 'assistant',
      tag: active.status || 'planning',
      content: '执行中...',
      action: 'topology',
    })
    return messages
  }

  milestones
    .filter((item) => item.summary)
    .slice(-5)
    .forEach((item, index) => {
      messages.push({
        id: `${active.id}-${item.stage}-${index}`,
        role: 'assistant',
        tag: `${item.agent_id || item.stage} · ${item.status}`,
        content: item.summary,
        action: index === 0 ? 'topology' : '',
      })
    })

  const timeline = active.result?.a2a_timeline || active.result?.a2a_runtime_evidence?.messages || []
  const latestTrace = timeline.at?.(-1) || timeline[timeline.length - 1]
  if (latestTrace?.output_summary) {
    messages.push({
      id: `${active.id}-trace-${latestTrace.id || latestTrace.message_type}`,
      role: 'assistant',
      tag: latestTrace.message_type || 'a2a',
      content: latestTrace.output_summary,
      action: 'topology',
    })
  }

  const conclusion = active.result?.decision_report?.final_conclusion
  if (conclusion && active.statusClass === 'success') {
    messages.push({
      id: `${active.id}-final`,
      role: 'assistant',
      tag: 'completed',
      content: conclusion,
      action: 'topology',
    })
  }

  return messages
})

const presentedConversationMessages = computed(() => {
  const active = network.activeIntent
  if (!active) {
    return [
      {
        id: 'welcome',
        role: 'assistant',
        tag: 'ready',
        content: welcomeMessage,
      },
    ]
  }

  const recordMessages = normalizeDialogMessages(active.dialogMessages || [], active.id, 'record', active)
  const pinnedMessages = dedupeDisplayMessages(recordMessages)

  // 普通聊天只显示真实对话消息，不再补网络意图占位、不挂报告工具。
  if (isPureChatRecord(active)) {
    return dedupeDisplayMessages(pinnedMessages
      .filter((message) => !isIntentAcceptedPlaceholder(message))
      .filter((message) => message.content || message.pending))
  }

  const visiblePinnedMessages = dedupeDisplayMessages(pinnedMessages.filter((message) => shouldKeepPinnedMessage(active, message)))
  const hasUserMessage = visiblePinnedMessages.some((message) => message.role === 'user')
  const hasClarificationMessage = visiblePinnedMessages.some((message) => {
    const tag = normalizeMessageTag(message.tag)
    const type = normalizeMessageTag(message.messageType)
    return type === 'clarification_question' || tag.includes('clarification') || tag.includes('需要补充')
  })
  // A newly submitted turn has not been bound to a real intent yet. Reusing
  // activeRunId here would append the previous intent's result to this turn.
  const generatedMessages = active.pendingTurnId
    ? [{
        id: `${active.pendingTurnId}-thinking`,
        role: 'assistant',
        tag: 'NetPulse',
        action: 'thinking',
        pending: true,
        content: '执行中...',
        thinkingLabel: '思考中',
        turnId: active.pendingTurnId,
        source: 'turn-state',
      }]
    : buildGeneratedMessages(
        active,
        !hasUserMessage,
        hasClarificationMessage ? '' : network.activeClarificationQuestion,
      )
  const mergedMessages = dedupeDisplayMessages(mergeConversationMessages(visiblePinnedMessages, generatedMessages))
  const activeRuntimeThinking = Boolean(
    active.activeTurnIntentId
    && !active.pendingTurnId
    && isRunning.value
    && !mergedMessages.some(isThinkingStep)
  )

  if (activeRuntimeThinking) {
    mergedMessages.push({
      id: `${active.activeTurnIntentId}-runtime-thinking`,
      role: 'assistant',
      tag: 'NetPulse',
      action: 'thinking',
      pending: true,
      content: '执行中...',
      thinkingLabel: intentProgressLabel(active, active.activeTurnIntentId, '思考中'),
      intentId: active.activeTurnIntentId,
      turnId: active.activeTurnId || '',
      source: 'runtime-state',
    })
  }

  return dedupeDisplayMessages(attachRuntimeFinalTools(mergedMessages, active))
})

function attachRuntimeFinalTools(messages = [], active) {
  if (!active || isPureChatRecord(active)) return messages

  const runtimeIds = [...new Set([
    ...(active.runIds || []),
    active.activeRunId,
    active.intent_id,
    active.intentId,
    active.id,
  ].filter(Boolean))]

  let next = [...messages]
  runtimeIds.forEach((runtimeId) => {
    const runtime = intentRuntimeInfo(runtimeId)
    if (!isRuntimeTerminal(runtime) || !runtime?.result?.decision_report) return

    let targetIndex = -1
    for (let index = next.length - 1; index >= 0; index -= 1) {
      const message = next[index]
      if (message?.role !== 'assistant') continue
      if (messageIntentId(message) === runtimeId) {
        targetIndex = index
        break
      }
    }
    if (targetIndex < 0) return

    next[targetIndex] = {
      ...next[targetIndex],
      action: 'topology',
      report: next[targetIndex].report || runtime.result,
      intentId: runtimeId,
    }
  })

  return next
}

function buildConversationThreadsFromMessages(messages = []) {
  const threads = []
  let activeAgentThread = null

  messages.forEach((message, index) => {
    if (!message?.content && !message?.pending) return

    if (message.role === 'user') {
      activeAgentThread = null
      threads.push({
        id: message.id || `user-thread-${index}`,
        role: 'user',
        messages: [message],
      })
      return
    }

    if (!activeAgentThread) {
      activeAgentThread = {
        id: `agent-thread-${index}-${message.id || 'message'}`,
        role: 'assistant',
        messages: [],
      }
      threads.push(activeAgentThread)
    }

    activeAgentThread.messages.push(message)
  })

  return threads
}

const pureChatThreads = computed(() => {
  const active = network.activeIntent
  if (!isPureChatRecord(active)) return []

  const messages = dedupeDisplayMessages(active?.dialogMessages || [])
    .filter((message) => message?.content || message?.pending)
    .map((message, index) => ({
      id: message.id || `${active.id}-pure-chat-${index}`,
      role: message.role || 'assistant',
      tag: message.tag || '',
      content: String(message.content || ''),
      rawContent: String(message.content || ''),
      pending: Boolean(message.pending),
      isTypingComplete: true,
      isTypewriterVisible: true,
    }))

  return buildConversationThreadsFromMessages(messages)
})

const conversationThreads = computed(() => {
  const renderMessages = typedConversationMessages.value.length
    ? typedConversationMessages.value.filter((message) => (
      message.role !== 'assistant' || message.isTypewriterVisible || message.isTypingComplete
    ))
    : presentedConversationMessages.value

  return attachFrozenPresentationsToThreads(
    buildConversationThreadsFromMessages(renderMessages),
  )
})

function attachFrozenPresentationsToThreads(threads = []) {
  const active = network.activeIntent
  const presentations = active?.finalPresentation || {}
  if (!active || isPureChatRecord(active) || !Object.keys(presentations).length) {
    return threads.map((thread) => ({ ...thread, finalPresentations: [] }))
  }

  const nextThreads = threads.map((thread) => ({ ...thread, finalPresentations: [] }))
  const exactOwnerByMessageId = new Map()
  const fallbackOwnerByIntent = new Map()

  nextThreads.forEach((thread, threadIndex) => {
    if (thread.role !== 'assistant') return

    ;(thread.messages || []).forEach((message) => {
      if (message?.id) exactOwnerByMessageId.set(String(message.id), threadIndex)
      const intentId = messageIntentId(message)
      if (intentId) fallbackOwnerByIntent.set(String(intentId), threadIndex)
    })
  })

  const runOrder = new Map((active.runIds || []).map((intentId, index) => [String(intentId), index]))
  const orderedPresentations = Object.entries(presentations)
    .filter(([, presentation]) => presentation?.executionSummary)
    .sort(([leftId], [rightId]) => (
      (runOrder.get(String(leftId)) ?? Number.MAX_SAFE_INTEGER)
      - (runOrder.get(String(rightId)) ?? Number.MAX_SAFE_INTEGER)
    ))

  orderedPresentations.forEach(([intentId, presentation]) => {
    const exactOwner = presentation.finalMessageId
      ? exactOwnerByMessageId.get(String(presentation.finalMessageId))
      : undefined
    const ownerIndex = exactOwner ?? fallbackOwnerByIntent.get(String(intentId))
    if (ownerIndex === undefined) return

    nextThreads[ownerIndex].finalPresentations.push({
      ...presentation,
      intentId,
    })
  })

  return nextThreads
}

function typedMessageSemanticKey(message = {}, rawContent = '') {
  const content = normalizeMessageContent(rawContent || message.rawContent || message.content)
  const intentId = messageIntentId(message)
  if (!content || !intentId) return ''
  return `${message.role || 'assistant'}:${intentId}:${content}`
}

function syncTypedConversationMessages(messages = presentedConversationMessages.value, options = {}) {
  const shouldAnimate = options.animate ?? isRunning.value
  const previousMap = new Map(typedConversationMessages.value.map((message) => [message.id, message]))
  const previousSemanticMap = new Map(
    typedConversationMessages.value
      .map((message) => [typedMessageSemanticKey(message), message])
      .filter(([key]) => key),
  )

  typedConversationMessages.value = messages.map((message, index) => {
    const id = message.id || `${message.role || 'message'}-${index}`
    const rawContent = String(message.content || '')
    const semanticKey = typedMessageSemanticKey(message, rawContent)
    const previous = previousMap.get(id) || (semanticKey ? previousSemanticMap.get(semanticKey) : null)

    if (message.role !== 'assistant') {
      return {
        ...message,
        id,
        content: rawContent,
        rawContent,
        isTypingComplete: true,
        isTypewriterVisible: true,
      }
    }

    // Pending feedback is UI state, not prose. Show it immediately so a fast
    // second intent cannot finish before the typewriter reveals the spinner.
    if (isThinkingStep(message)) {
      return {
        ...message,
        id,
        rawContent,
        content: rawContent,
        action: message.action || 'thinking',
        report: null,
        isTypingComplete: true,
        isTypewriterVisible: true,
      }
    }

    if (!shouldAnimate) {
      return {
        ...message,
        id,
        rawContent,
        content: rawContent,
        action: message.action || '',
        report: message.report || null,
        isTypingComplete: true,
        isTypewriterVisible: true,
      }
    }

    const isSameMessage = previous?.rawContent === rawContent
    const previousContent = isSameMessage ? String(previous.content || '') : ''
    const content = previous?.isTypingComplete && isSameMessage ? rawContent : previousContent
    const isTypingComplete = rawContent.length === 0 || content.length >= rawContent.length
    const isTypewriterVisible = isSameMessage
      ? Boolean(previous.isTypewriterVisible || content.length || isTypingComplete)
      : false

    return {
      ...message,
      id,
      rawContent,
      content: isTypingComplete ? rawContent : content,
      action: isTypingComplete ? message.action : '',
      report: isTypingComplete ? message.report : null,
      isTypingComplete,
      isTypewriterVisible,
    }
  })

  if (shouldAnimate) startTypewriter()
  else {
    if (typingTimer.value) {
      window.clearTimeout(typingTimer.value)
      typingTimer.value = null
    }
    persistActiveConversation()
    scrollConversationToBottom()
  }
}

function startTypewriter() {
  if (typingTimer.value) return

  const tick = () => {
    const index = typedConversationMessages.value.findIndex((message) => (
      message.role === 'assistant'
      && String(message.content || '').length < String(message.rawContent || '').length
    ))

    if (index < 0) {
      typingTimer.value = null
      revealCompletedAssistantTools()
      return
    }

    const messages = [...typedConversationMessages.value]
    const current = { ...messages[index] }
    const rawContent = String(current.rawContent || '')
    current.isTypewriterVisible = true
    const nextLength = Math.min(String(current.content || '').length + 1, rawContent.length)
    current.content = rawContent.slice(0, nextLength)
    current.isTypingComplete = nextLength >= rawContent.length

    if (current.isTypingComplete) {
      const source = presentedConversationMessages.value.find((message) => (message.id || '') === current.id)
      current.action = source?.action || current.action || ''
      current.report = source?.report || current.report || null
    }

    messages[index] = current
    typedConversationMessages.value = messages
    persistActiveConversation()
    scrollConversationToBottom()

    typingTimer.value = window.setTimeout(tick, current.isTypingComplete ? 160 : 18)
  }

  typingTimer.value = window.setTimeout(tick, 80)
}

function revealCompletedAssistantTools() {
  const sourceMap = new Map(presentedConversationMessages.value.map((message) => [message.id, message]))
  let changed = false
  const messages = typedConversationMessages.value.map((message) => {
    if (message.role !== 'assistant' || !message.isTypingComplete) return message
    const source = sourceMap.get(message.id)
    if (!source) return message
    if (message.action === source.action && message.report === source.report) return message
    changed = true
    return {
      ...message,
      action: source.action || '',
      report: source.report || null,
    }
  })
  if (changed) {
    typedConversationMessages.value = messages
    persistActiveConversation()
  }
}

function readConversationTitles() {
  // Conversation content and summaries are always rehydrated from /sessions
  // and /intents.  A browser cache must never become the history authority.
  return {}
}

function persistConversationTitles() {
  // Deliberately no-op: titles remain derived from persisted server records.
}

function conversationTitleKey(record) {
  if (!record) return ''
  // 标题必须按每条历史记录自己的 id 存，不能优先用 sessionId。
  // 多个新建聊天可能共用同一个 sessionId；如果用 sessionId 当标题 key，
  // 后面的新对话标题就会沿用第一条对话的标题。
  return record.id || record.sessionId || record.activeRunId || ''
}

function normalizeHistoryTitle(text) {
  return String(text || '')
    .replace(/\s+/g, ' ')
    .trim()
}

const HISTORY_TITLE_MAX_LENGTH = 40

function _isWatcherIntent(record) {
  if (!record) return false
  return record.owner === 'watcher' || record.user_id === 'watcher'
}

function fallbackHistoryTitle(record) {
  if (!record) return '新的对话'

  const localMessages = record.dialogMessages || []
  const firstUserMessage = localMessages.find((message) => message.role === 'user' && normalizeHistoryTitle(message.content))
  const raw = normalizeHistoryTitle(firstUserMessage?.content || record.text || record.content || record.title)
  if (!raw) return '新的对话'

  let title = raw
  if (title.length > HISTORY_TITLE_MAX_LENGTH) {
    title = title.slice(0, HISTORY_TITLE_MAX_LENGTH) + '…'
  }
  if (_isWatcherIntent(record)) {
    title = '⚡ ' + title
  }
  return title
}

function displayHistoryTitle(record) {
  const key = conversationTitleKey(record)
  const customTitle = key ? normalizeHistoryTitle(conversationTitles.value[key]) : ''
  return customTitle || fallbackHistoryTitle(record)
}

function ensureConversationTitle(record) {
  const key = conversationTitleKey(record)
  if (!key || normalizeHistoryTitle(conversationTitles.value[key])) return

  const title = fallbackHistoryTitle(record)
  conversationTitles.value = {
    ...conversationTitles.value,
    [key]: title,
  }
  persistConversationTitles()
}

function beginHistoryTitleEdit(intent) {
  if (!intent?.id) return
  editingHistoryId.value = intent.id
  historyTitleDraft.value = displayHistoryTitle(intent)
}

function commitHistoryTitle(intent) {
  if (editingHistoryId.value !== intent?.id) return

  const key = conversationTitleKey(intent)
  const title = normalizeHistoryTitle(historyTitleDraft.value) || fallbackHistoryTitle(intent)

  if (key) {
    conversationTitles.value = {
      ...conversationTitles.value,
      [key]: title,
    }
    persistConversationTitles()
  }

  intent.title = title
  editingHistoryId.value = ''
  historyTitleDraft.value = ''
}

function cancelHistoryTitleEdit() {
  editingHistoryId.value = ''
  historyTitleDraft.value = ''
}

function normalizeDialogMessages(messages = [], fallbackId = 'conversation', source = 'cache', contextRecord = null) {
  const normalized = (messages || [])
    .map((message, index) => {
      const messageType = inferDisplayMessageType(message)
      const tag = message.tag || ''
      const normalizedTag = normalizeMessageTag(tag)
      if (messageType === 'intent' || normalizedTag === 'intent') return null

      return {
        id: message.id || `${fallbackId}-${source}-${index}`,
        role: message.role || 'assistant',
        tag,
        messageType,
        content: message.content || '',
        action: message.action || '',
        pending: Boolean(message.pending),
        report: cloneReport(message.report),
        intentId: messageIntentId(message),
        turnId: message.turnId || message.turn_id || '',
        finalOutcome: Boolean(message.finalOutcome),
        thoughtOnly: Boolean(message.thoughtOnly)
          || INTERNAL_MESSAGE_TYPES.has(messageType)
          || INTERNAL_MESSAGE_TAGS.has(normalizedTag),
        source,
      }
    })
    .filter((message) => message && (message.content || message.pending))

  return backfillTurnIntentIds(normalized, contextRecord)
}

function isNetworkLikeTurn(turn = []) {
  return turn.some((message) => {
    if (messageIntentId(message) || message.report || message.action === 'topology') return true
    const type = inferDisplayMessageType(message)
    const tag = normalizeMessageTag(message.tag)
    const content = normalizeMessageContent(message.content)
    return Boolean(
      INTERNAL_MESSAGE_TYPES.has(type)
      || type === 'clarification_question'
      || tag.includes('clarification')
      || tag.includes('需要补充')
      || content.includes('意图')
      || content.includes('执行')
      || content.includes('拓扑')
      || content.includes('网络')
    )
  })
}

function backfillTurnIntentIds(messages = [], contextRecord = null) {
  const next = messages.map((message) => ({ ...message }))
  let turnStart = 0
  const turns = []

  const fillTurn = (start, end) => {
    if (end <= start) return
    turns.push([start, end])
    const turnIntentId = next
      .slice(start, end)
      .map(messageIntentId)
      .filter(Boolean)
      .at(-1)
    if (!turnIntentId) return

    for (let index = start; index < end; index += 1) {
      if (!messageIntentId(next[index])) {
        next[index].intentId = turnIntentId
      }
    }
  }

  for (let index = 1; index < next.length; index += 1) {
    if (next[index].role === 'user') {
      fillTurn(turnStart, index)
      turnStart = index
    }
  }

  fillTurn(turnStart, next.length)

  const runtimeIds = contextRecord && !isPureChatRecord(contextRecord)
    ? [...new Set(contextRecord.runIds || [])]
    : []
  if (runtimeIds.length) {
    let runtimeCursor = 0
    turns.forEach(([start, end]) => {
      const turn = next.slice(start, end)
      const hasExplicitIntent = turn.some((message) => messageIntentId(message))
      if (hasExplicitIntent || !isNetworkLikeTurn(turn)) return

      const runtimeId = runtimeIds[runtimeCursor]
      runtimeCursor += 1
      if (!runtimeId) return

      for (let index = start; index < end; index += 1) {
        if (!messageIntentId(next[index])) {
          next[index].intentId = runtimeId
        }
      }
    })
  }

  return next
}

function normalizeMessageContent(content = '') {
  return String(content || '').replace(/\s+/g, ' ').trim()
}

function sourceWeight(source = '') {
  if (source === 'record') return 3
  if (source === 'cache') return 2
  if (source === 'generated') return 1
  return 0
}

function mergeDisplayMessage(existing = {}, incoming = {}) {
  const preferIncoming = sourceWeight(incoming.source) > sourceWeight(existing.source)
  const base = preferIncoming ? incoming : existing
  const extra = preferIncoming ? existing : incoming

  return {
    ...base,
    id: base.id || extra.id,
    tag: base.tag || extra.tag || '',
    messageType: base.messageType || extra.messageType || extra.message_type || '',
    content: base.content || extra.content || '',
    action: base.action || extra.action || '',
    pending: Boolean(base.pending || extra.pending),
    report: base.report || extra.report || null,
    intentId: messageIntentId(base) || messageIntentId(extra),
    thoughtOnly: Boolean(base.thoughtOnly || extra.thoughtOnly),
    source: base.source || extra.source || '',
  }
}

function dedupeDisplayMessages(messages = []) {
  const result = []
  const idIndex = new Map()
  const semanticIndex = new Map()

  ;(messages || []).forEach((message) => {
    const content = normalizeMessageContent(message?.content)
    if (!content && !message?.pending) return

    const role = message.role || 'assistant'
    const id = message.id || ''
    const previous = result[result.length - 1]

    if (
      role === 'user'
      && previous?.role === 'user'
      && normalizeMessageContent(previous.content) === content
    ) {
      result[result.length - 1] = mergeDisplayMessage(previous, message)
      if (id) idIndex.set(id, result.length - 1)
      return
    }

    if (id && idIndex.has(id)) {
      const index = idIndex.get(id)
      result[index] = mergeDisplayMessage(result[index], message)
      return
    }

    const semanticIntentId = messageIntentId(message)
    const semanticKey = role !== 'user' && semanticIntentId && content
      ? `${role}:${semanticIntentId}:${content}`
      : ''
    if (role !== 'user' && semanticKey && semanticIndex.has(semanticKey)) {
      const index = semanticIndex.get(semanticKey)
      result[index] = mergeDisplayMessage(result[index], message)
      if (id) idIndex.set(id, index)
      return
    }

    const index = result.length
    result.push({
      ...message,
      role,
      content,
    })
    if (id) idIndex.set(id, index)
    if (role !== 'user' && semanticKey) semanticIndex.set(semanticKey, index)
  })

  return result
}

function messagesSignature(messages = []) {
  return JSON.stringify((messages || []).map((message) => ({
    id: message.id || '',
    role: message.role || '',
    tag: message.tag || '',
    messageType: message.messageType || '',
    content: message.content || '',
    action: message.action || '',
    pending: Boolean(message.pending),
    hasReport: Boolean(message.report),
    intentId: messageIntentId(message),
    turnId: message.turnId || message.turn_id || '',
    finalOutcome: Boolean(message.finalOutcome),
    thoughtOnly: Boolean(message.thoughtOnly),
  })))
}

function snapshotMessagesForPersistence() {
  const source = typedConversationMessages.value.length
    ? typedConversationMessages.value
    : presentedConversationMessages.value

  return dedupeDisplayMessages(source
    .filter((message) => message.role !== 'assistant' || message.isTypingComplete || message.content)
  ).map((message, index) => ({
      id: message.id || `${message.role || 'message'}-${index}`,
      role: message.role || 'assistant',
      tag: message.tag || '',
      messageType: message.messageType || message.message_type || '',
      content: String(message.rawContent || message.content || ''),
      action: message.action || '',
      pending: Boolean(message.pending),
      report: cloneReport(message.report),
      intentId: messageIntentId(message),
      turnId: message.turnId || message.turn_id || '',
      finalOutcome: Boolean(message.finalOutcome),
      thoughtOnly: Boolean(message.thoughtOnly),
      source: message.source || '',
    }))
    .filter((message) => message.content || message.pending)
}

function isPureChatStreaming(record) {
  if (!record) return false
  return isPureChatRecord(record) && (
    network.isStreaming
    || record.status === 'chatting'
    || record.statusClass === 'running'
  )
}

function persistActiveConversation() {
  const active = network.activeIntent
  if (!active?.id) return

  if (active._historyLoaded) {
    ensureConversationTitle(active)
    return
  }

  // The store owns messages while a turn is being classified or streamed.
  // Persisting rendered placeholders here can overwrite the current turn with
  // stale messages from the previous intent.
  if (active.pendingTurnId || active.activeTurnIntentId || network.isStreaming) {
    ensureConversationTitle(active)
    return
  }

  // 普通聊天正在流式输出时，不能把 typedConversationMessages 反写回 dialogMessages。
  // 否则空的 assistant 占位消息会被过滤掉，后续 token 虽然回来了，页面也找不到那条 assistant 消息。
  if (isPureChatStreaming(active)) {
    return
  }

  const messages = snapshotMessagesForPersistence()
  if (!messages.length) return
  if (messagesSignature(active.dialogMessages || []) !== messagesSignature(messages)) {
    active.dialogMessages = messages
  }
  ensureConversationTitle(active)
}

function hydrateActiveConversationFromServer(record) {
  if (!record?.id) return

  // 普通聊天正在流式输出时，不覆盖当前消息数组。
  if (isPureChatStreaming(record)) {
    ensureConversationTitle(record)
    return
  }

  record.dialogMessages = dedupeDisplayMessages(record.dialogMessages || [])
  ensureConversationTitle(record)
}

function refreshUserProfile() {
  resetProfileDraft()
}

function persistUserProfile() { /* no client-side identity persistence */ }

function toggleAccountMenu() {
  refreshUserProfile()
  accountMenuOpen.value = !accountMenuOpen.value
  if (!accountMenuOpen.value) {
    isEditingName.value = false
    modelMenuOpen.value = false
  }
}

function closeAccountMenu() {
  accountMenuOpen.value = false
  isEditingName.value = false
  modelMenuOpen.value = false
}

function handleAccountOutsideClick(event) {
  if (!accountMenuOpen.value) return

  const accountArea = accountAreaRef.value
  if (!accountArea) return

  if (!accountArea.contains(event.target)) {
    closeAccountMenu()
  }




}

function resetProfileDraft() {
  profileDraft.name = userProfile.name || '未命名'
  profileDraft.avatar = userProfile.avatar || ''
  if (profileFileInput.value) profileFileInput.value.value = ''
}

function triggerAvatarPicker() {
  profileFileInput.value?.click()
}

function handleAvatarSelected(event) {
  const file = event.target.files?.[0]
  if (!file) return

  const reader = new FileReader()
  reader.onload = () => {
    userProfile.avatar = String(reader.result || '')
    profileDraft.avatar = userProfile.avatar
    persistUserProfile()
  }
  reader.readAsDataURL(file)
}

function beginNameEdit() {
  profileDraft.name = userProfile.name || '未命名'
  isEditingName.value = true
  nextTick(() => nameInputEl.value?.focus())
}

function commitProfileName() {
  if (!isEditingName.value) return
  userProfile.name = profileDraft.name?.trim() || '未命名'
  persistUserProfile()
  isEditingName.value = false
}

function cancelNameEdit() {
  profileDraft.name = userProfile.name || '未命名'
  isEditingName.value = false
}

function selectModel(modelId) {
  selectedModel.value = modelId
  modelMenuOpen.value = false
}

function openUpgrade() {
  accountMenuOpen.value = false
  window.dispatchEvent(new CustomEvent('netpulse-toast', { detail: '当前演示账号已启用基础能力，套餐模块待接入。' }))
}

function openSettings() {
  accountMenuOpen.value = false
  window.dispatchEvent(new CustomEvent('netpulse-toast', { detail: '设置中心待接入。' }))
}

function openHelp() {
  accountMenuOpen.value = false
  window.dispatchEvent(new CustomEvent('netpulse-toast', { detail: '帮助文档待接入。' }))
}

function logout() {
  accountMenuOpen.value = false
}

onMounted(() => {
  refreshUserProfile()
  network.startIntentPolling()
  network.loadTopology()
  network.startTopologySSE()
  network.startTelemetryPolling()
  network.loadIntTelemetry()
  document.addEventListener('pointerdown', handleAccountOutsideClick)
  nextTick(() => {
    initTelemetryBarChart()
    initTelemetryBarWatch()
  })
})

onActivated(() => {
  network.startIntentPolling()
  network.startTelemetryPolling()
  network.startTopologySSE()
  nextTick(() => {
    disposeTelemetryBarChart()
    initTelemetryBarChart()
    initTelemetryBarWatch()
  })
})

onDeactivated(() => {
  network.stopTelemetryPolling()
  network.stopTopologySSE()
  disposeTelemetryBarChart()
})

onUnmounted(() => {
  network.stopTelemetryPolling()
  network.stopTopologySSE()
  document.removeEventListener('pointerdown', handleAccountOutsideClick)
  disposeTelemetryBarChart()
  if (typingTimer.value) window.clearTimeout(typingTimer.value)
})

watch(
  presentedConversationMessages,
  (messages) => {
    const active = network.activeIntent
    const activeKey = primaryConversationKey(active)
    const sameTypedOwner = activeKey && typedConversationOwnerKey.value === activeKey
    const animate = isRunning.value
      && !active?._historyLoaded
      && !suppressNextTypewriter.value
      && !isConversationMarked(historyViewLockedIds, active)
      && (!isConversationMarked(animatedConversationIds, active) || sameTypedOwner)

    if (animate && active) {
      markConversationSet(animatedConversationIds, active)
    }

    syncTypedConversationMessages(messages, { animate })
    typedConversationOwnerKey.value = activeKey
    suppressNextTypewriter.value = false
  },
  { immediate: true, deep: true },
)

watch(
  () => [network.activeIntent?.id, presentedConversationMessages.value.length, workspaceMode.value],
  () => scrollConversationToBottom(),
  { flush: 'post' },
)

watch(
  () => network.activeIntent?.id,
  () => {
    hydrateActiveConversationFromServer(network.activeIntent)
  },
  { flush: 'post' },
)

function scrollConversationToBottom(force = false) {
  nextTick(() => {
    const el = conversationScrollEl.value
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    if (force || distanceFromBottom < 80) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    }
  })
}
function countBy(list = [], predicate = () => false) {
  return list.filter((item) => predicate(item)).length
}

function normalizeState(state) {
  return String(state || '').toLowerCase()
}

function formatSystemValue(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return `${Number.isInteger(value) ? value : value.toFixed(2)}${suffix}`
  return `${value}${suffix}`
}

const systemOverviewCards = computed(() => {
  const nodes = network.topologyNodes || []
  const links = network.topologyLinks || []
  const metrics = network.metrics || []
  const abnormalNodes = countBy(nodes, (node) => ['down', 'error', 'warning', 'degraded', 'critical'].includes(normalizeState(node.state)))
  const abnormalLinks = countBy(links, (link) => ['down', 'error', 'warning', 'degraded', 'critical'].includes(normalizeState(link.state)))

  return [
    { key: 'nodes', label: '节点总数', value: nodes.length, hint: '当前拓扑节点' },
    { key: 'links', label: '链路总数', value: links.length, hint: '实时链路关系' },
    { key: 'alerts', label: '异常对象', value: abnormalNodes + abnormalLinks, hint: '节点 / 链路异常' },
    { key: 'metrics', label: '遥测指标', value: Array.isArray(metrics) ? metrics.length : Object.keys(metrics || {}).length, hint: '后台返回指标量' },
  ]
})

const systemBackendSignals = computed(() => {
  const health = network.healthStatus || {}
  const watcher = network.watcherStatus || {}
  const incidents = Array.isArray(network.watcherIncidents) ? network.watcherIncidents.length : 0
  const healthValue = health.status || health.state || health.health || 'unknown'
  const watcherValue = watcher.status || watcher.state || watcher.mode || (incidents ? 'active' : 'ready')
  const telemetryValue = network.telemetryMode || (telemetryMetricStats.value.length ? 'online' : 'idle')
  const topologyValue = network.topologySource || 'dynamic'

  const signalTone = (value, fallback = 'info') => {
    const state = normalizeState(value)
    if (['ok', 'ready', 'healthy', 'success', 'online'].some((word) => state.includes(word))) return 'ok'
    if (['error', 'failed', 'down', 'critical', 'warn', 'degraded'].some((word) => state.includes(word))) return 'warn'
    if (state.includes('active') || state.includes('running')) return 'active'
    return fallback
  }

  return [
    { label: '后台健康', value: healthValue, tone: signalTone(healthValue) },
    { label: '遥测通道', value: telemetryValue, tone: signalTone(telemetryValue) },
    { label: 'Watcher', value: watcherValue, tone: incidents ? 'warn' : signalTone(watcherValue, 'active') },
    { label: '拓扑来源', value: topologyValue, tone: 'info' },
  ]
})

const topologyCompositionStats = computed(() => {
  const nodes = network.topologyNodes || []
  return [
    { label: '交换节点', value: countBy(nodes, (node) => normalizeState(node.type).includes('switch') || normalizeState(node.role).includes('switch')) },
    { label: '主机节点', value: countBy(nodes, (node) => normalizeState(node.type).includes('host') || normalizeState(node.role).includes('host')) },
    { label: '智能体节点', value: countBy(nodes, (node) => normalizeState(node.type).includes('agent')) },
    { label: '在线节点', value: countBy(nodes, (node) => !['down', 'offline', 'error'].includes(normalizeState(node.state))) },
  ]
})

const linkHealthStats = computed(() => {
  const links = network.topologyLinks || []
  return [
    { label: '在线链路', value: countBy(links, (link) => !['down', 'offline', 'error'].includes(normalizeState(link.state))) },
    { label: '高亮链路', value: countBy(links, (link) => Boolean(link.active || link.highlight || link.selected)) },
    { label: '异常链路', value: countBy(links, (link) => ['down', 'offline', 'error', 'warning', 'degraded'].includes(normalizeState(link.state))) },
    { label: '拓扑来源', value: network.topologySource || 'dynamic' },
  ]
})

const telemetryMetricStats = computed(() => {
  const raw = network.metrics || []
  const entries = Array.isArray(raw)
    ? raw.map((item, index) => ({
        label: item.label || item.name || item.key || `Metric ${index + 1}`,
        value: item.value ?? item.current ?? item.score ?? '-',
        status: item.status || item.state || item.level || '',
        tone: item.tone || item.severity || '',
        trend: item.trend || item.direction || '',
        change: item.change ?? item.delta ?? '',
      }))
    : Object.entries(raw || {}).map(([key, value]) => ({
        label: key,
        value,
        status: '',
        tone: '',
        trend: '',
        change: '',
      }))

  if (!entries.length) {
    return [
      { label: '实时指标', value: '等待遥测' },
      { label: '健康状态', value: network.topologySource || 'dynamic' },
      { label: '活跃意图', value: network.activeIntent?.status || '-' },
    ]
  }

  return entries.slice(0, 8).map((item) => ({
    label: item.label,
    value: formatSystemValue(item.value),
    rawValue: item.rawValue ?? null,
    status: item.status,
    tone: item.tone,
    trend: item.trend,
    change: item.change,
  }))
})

const systemDetailTabs = [
  { key: 'overview', label: '总览' },
  { key: 'nodes', label: '节点' },
  { key: 'links', label: '链路' },
  { key: 'metrics', label: '遥测' },
]

function toNumberValue(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : 0
  const parsed = Number(String(value ?? '').replace(/[^0-9.-]/g, ''))
  return Number.isFinite(parsed) ? parsed : 0
}

const topologyHealthPercent = computed(() => {
  const nodes = network.topologyNodes || []
  const links = network.topologyLinks || []
  const total = Math.max(nodes.length + links.length, 1)
  const badNodes = countBy(nodes, (node) => ['down', 'offline', 'error', 'warning', 'degraded', 'critical'].includes(normalizeState(node.state)))
  const badLinks = countBy(links, (link) => ['down', 'offline', 'error', 'warning', 'degraded', 'critical'].includes(normalizeState(link.state)))
  const score = Math.round(((total - badNodes - badLinks) / total) * 100)
  return Math.max(0, Math.min(100, score))
})

const nodeMixBars = computed(() => {
  const stats = topologyCompositionStats.value.slice(0, 4)
  const max = Math.max(...stats.map((item) => Number(item.value) || 0), 1)
  return stats.map((item) => ({
    ...item,
    percent: Math.max(8, Math.round(((Number(item.value) || 0) / max) * 100)),
  }))
})

function _barPercent(numericValue, min, max, higherIsBetter) {
  if (numericValue == null || !Number.isFinite(numericValue)) return 8
  const clamped = Math.max(min, Math.min(max, numericValue))
  const ratio = (clamped - min) / (max - min)
  return Math.round(higherIsBetter ? 5 + ratio * 91 : 96 - ratio * 91)
}

const telemetryBars = computed(() => {
  const metrics = telemetryMetricStats.value.slice(0, 6)
  const numeric = metrics.map((item) => {
    const numericValue = item.rawValue != null ? Number(item.rawValue) : toNumberValue(item.value)
    const key = normalizeState(item.label)
    let percent = 12

    if (key.includes('latency') || key.includes('delay') || item.label.includes('时延')) {
      percent = _barPercent(numericValue, 0, 20, false)
    } else if (key.includes('loss') || item.label.includes('丢包')) {
      percent = _barPercent(numericValue, 0, 5, false)
    } else if (key.includes('throughput') || key.includes('mbps') || item.label.includes('吞吐')) {
      percent = _barPercent(numericValue, 0, 100, true)
    } else if (key.includes('queue') || item.label.includes('队列')) {
      percent = _barPercent(numericValue, 0, 50, false)
    } else if (key.includes('hop') || item.label.includes('跳')) {
      percent = _barPercent(numericValue, 1, 10, false)
    } else {
      percent = numericValue > 0 ? Math.min(96, Math.max(12, 12 + numericValue)) : 12
    }

    return { ...item, numeric: numericValue, percent }
  })

  if (!numeric.length) {
    return [
      { label: '核心时延', value: '--', numeric: 0, percent: 8 },
      { label: '主链路丢包', value: '--', numeric: 0, percent: 8 },
      { label: '吞吐量', value: '--', numeric: 0, percent: 8 },
      { label: '队列深度', value: '--', numeric: 0, percent: 8 },
    ]
  }

  return numeric
})

function _fmtTime(isoString) {
  if (!isoString) return '--:--'
  try {
    const t = new Date(isoString)
    const hh = String(t.getHours()).padStart(2, '0')
    const mm = String(t.getMinutes()).padStart(2, '0')
    const ss = String(t.getSeconds()).padStart(2, '0')
    return `${hh}:${mm}:${ss}`
  } catch {
    return '--:--'
  }
}

const systemTrendPoints = computed(() => {
  const history = network.telemetryHistory || []

  if (history.length === 0) {
    return []
  }

  return history.map((snap) => {
    const throughput = snap.throughput_mbps
    let value = 50
    if (throughput != null) {
      value = Math.max(5, Math.min(96, (throughput / 50) * 100))
    }
    return {
      label: _fmtTime(snap.timestamp),
      value: Math.round(value),
    }
  })
})

const systemTrendPath = computed(() => {
  const points = systemTrendPoints.value
  if (!points.length) return ''
  return points.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x} ${point.y}`).join(' ')
})

const intTelemetryData = computed(() => network.intTelemetry)

const trendOption = computed(() => {
  const points = (network.telemetryHistory || [])
    .filter(snap => snap.throughput_mbps != null)
    .slice(-30)
    .map(snap => ({
      time: _fmtTime(snap.timestamp),
      value: Number(snap.throughput_mbps),
    }))
  return toLineAreaOption(points, '吞吐量 (Mbps)')
})

function initTelemetryBarChart() {
  if (!telemetryBarHost.value || telemetryBarChart) return
  telemetryBarChart = echarts.init(telemetryBarHost.value)
  updateTelemetryBarChart()
}

function updateTelemetryBarChart() {
  if (!telemetryBarChart || telemetryBarChart.isDisposed()) return
  const items = telemetryBars.value.slice(0, 6)
  if (!items.length) { telemetryBarChart.clear(); return }
  const option = {
    grid: { left: 10, right: 18, top: 14, bottom: 24, containLabel: true },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(226,232,240,0.4)' } }, axisLabel: { color: '#94a3b8', fontSize: 10 } },
    yAxis: { type: 'category', data: items.map(i => i.label), inverse: true, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#64748b', fontSize: 10, fontWeight: 600 } },
    series: [{
      type: 'bar',
      data: items.map((item, i) => ({
        value: item.numeric || 0,
        itemStyle: {
          borderRadius: [0, 6, 6, 0],
          color: i < 3
            ? new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#38bdf8' }, { offset: 1, color: '#6366f1' }])
            : new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#818cf8' }, { offset: 1, color: '#8b5cf6' }]),
        },
      })),
      barWidth: 22,
      label: { show: true, position: 'right', color: '#0f172a', fontSize: 11, fontWeight: 700, formatter: p => String(p.value).slice(0, 8) },
    }],
    animation: true, animationDuration: 600, animationEasing: 'cubicOut',
  }
  telemetryBarChart.setOption(option, true)
}

function disposeTelemetryBarChart() {
  if (telemetryBarChart && !telemetryBarChart.isDisposed()) {
    telemetryBarChart.dispose()
    telemetryBarChart = null
  }
}

function initTelemetryBarWatch() {
  watch(telemetryBars, () => nextTick(updateTelemetryBarChart), { deep: true })
}

function isThinkingStep(step = {}) {
  return Boolean(
    step.pending
    || step.action === 'thinking'
    || normalizeMessageContent(step.content) === '执行中...'
    || normalizeMessageContent(step.content) === '思考中'
  )
}

function thinkingStepLabel(step = {}) {
  return step.thinkingLabel || '思考中'
}

function intentProgressLabel(active = {}, intentId = '', fallback = '思考中') {
  const runtimeId = intentId || active.activeTurnIntentId || active.activeRunId || ''
  const transport = runtimeId ? active.intentTransport?.[runtimeId] : null

  switch (transport?.state) {
    case 'slow':
      return '处理中，响应较慢'
    case 'stalled':
      return '执行时间较长，正在确认状态'
    case 'polling':
      return '实时连接中断，正在确认状态'
    case 'degraded':
      return '状态同步异常，正在重试'
    default:
      return fallback
  }
}

function isClarificationMessage(message = {}) {
  const tag = normalizeMessageTag(message.tag)
  const type = inferDisplayMessageType(message)
  const content = normalizeMessageContent(message.content)
  return Boolean(
    type === 'clarification_question'
    || tag.includes('clarification')
    || tag.includes('需要补充')
    || looksLikeClarificationRequest(content)
  )
}

function messageRuntimeId(message = {}) {
  return message.intentId || message.intent_id || ''
}

function isClarificationAnswerMessage(message = {}) {
  const type = inferDisplayMessageType(message)
  return message.role === 'user' && type === 'clarification_answer'
}

function normalizeClarificationPrompt(content = '') {
  return normalizeMessageContent(content)
    .replace(/^需要补充信息[:：]\s*/, '')
    .replace(/^需要用户澄清[:：]\s*/, '')
    .replace(/^当前意图还需要补充信息[,，:：]?\s*/, '')
    .replace(/^请补充[:：]?\s*/, '')
    .trim()
}

function latestClarificationState(intentId = '') {
  const active = network.activeIntent
  const messages = normalizeDialogMessages(active?.dialogMessages || [], active?.id || 'conversation', 'record')
  let questionIndex = -1
  let answerIndex = -1
  let latestQuestionText = ''
  let answeredQuestionText = ''

  messages.forEach((message, index) => {
    if (messageRuntimeId(message) !== intentId) return
    if (message.role === 'assistant' && isClarificationMessage(message)) {
      questionIndex = index
      latestQuestionText = normalizeClarificationPrompt(message.content)
    }
    if (isClarificationAnswerMessage(message)) {
      answerIndex = index
      answeredQuestionText = latestQuestionText
    }
  })

  return { questionIndex, answerIndex, latestQuestionText, answeredQuestionText }
}

function hasOpenClarificationQuestion(intentId = '') {
  if (!intentId) return false
  const { questionIndex, answerIndex, latestQuestionText, answeredQuestionText } = latestClarificationState(intentId)
  if (questionIndex < 0 || questionIndex <= answerIndex) return false
  if (answerIndex >= 0 && latestQuestionText && latestQuestionText === answeredQuestionText) return false
  return true
}

function hasAnsweredCurrentClarification(intentId = '') {
  if (!intentId) return false
  const { questionIndex, answerIndex } = latestClarificationState(intentId)
  return answerIndex >= 0 && (questionIndex < 0 || answerIndex > questionIndex)
}

function isAssistantIntroCandidate(message = {}) {
  if (!message?.content || message.thoughtOnly || message.pending) return false
  if (message.report || message.action === 'topology' || message.action === 'thinking') return false
  if (isIntentAcceptedPlaceholder(message)) return false
  if (isClarificationMessage(message)) return false
  return true
}

function displayedThreadMessages(thread) {
  const msgs = thread.messages || []
  const visible = msgs.filter((message) => !message.thoughtOnly)
  if (thread?.role !== 'assistant' || isPureChatActive.value) return visible

  const intentId = threadIntentId(thread)
  if (!intentId) {
    const lastVisible = [...visible].reverse().find((message) => message?.content || message?.pending)
    return lastVisible ? [lastVisible] : []
  }

  const runtime = intentRuntimeInfo(intentId)
  const terminal = isRuntimeTerminal(runtime)
  const frozen = finalPresentationForIntent(intentId)
  const frozenMessage = visible.find((message) => (
    (frozen?.finalMessageId && String(message.id || '') === String(frozen.finalMessageId))
    || (message.finalOutcome && messageIntentId(message) === intentId)
  ))
  const content = terminal
    ? (frozen?.finalReply || frozenMessage?.content || summaryConclusion(thread, runtime))
    : summaryConclusion(thread, runtime)
  const showTopologyAction = terminal && Boolean(runtime?.result)
  const steps = []
  const intro = visible.find(isAssistantIntroCandidate)
  const latestRuntimeThread = isLatestRuntimeThreadForIntent(thread, intentId)

  if (!latestRuntimeThread) {
    const isClarificationResumeThread = visible.some((message) => normalizeMessageContent(message.content).includes('已收到补充信息'))
    return visible.filter((message) => (
      !isTerminalOutcomeMessage(message)
      && !isThinkingStep(message)
      && !(isClarificationResumeThread && isClarificationMessage(message))
    ))
  }

  if (intro) {
    steps.push({
      ...intro,
      id: `${intentId}-intro-${intro.id || 'message'}`,
      action: '',
      report: null,
      intentId,
    })
  }

  const normalizedIntro = normalizeMessageContent(intro?.content)
  const normalizedContent = normalizeMessageContent(content)

  if (!terminal && normalizedContent === '执行中...') {
    steps.push({
      id: `${intentId}-thinking`,
      role: 'assistant',
      tag: 'NetPulse',
      action: 'thinking',
      pending: true,
      content: '',
      thinkingLabel: intentProgressLabel(network.activeIntent, intentId, intro ? '思考中' : '执行中'),
      intentId,
    })
    return steps
  }

  if (normalizedContent && normalizedContent !== normalizedIntro) {
    steps.push({
      id: `${intentId}-visible-outcome`,
      role: 'assistant',
      tag: terminal ? 'NetPulse' : '需要补充信息',
      content,
      action: showTopologyAction ? 'topology' : '',
      report: showTopologyAction ? runtime.result : null,
      intentId,
    })
  }

  return steps
}

function isLatestRuntimeThreadForIntent(thread, intentId) {
  if (!thread?.id || !intentId) return false

  const threads = conversationThreads.value || []
  const sameIntentThreads = threads.filter((item) => (
    item?.role === 'assistant'
    && threadIntentId(item) === intentId
  ))
  const latest = sameIntentThreads[sameIntentThreads.length - 1]
  return latest?.id === thread.id
}

function isExecutionStageItem(item = {}) {
  const type = String(item.type || item.messageType || item.message_type || '').toLowerCase()
  const stage = String(item.stage || item.name || '').toLowerCase()
  const tag = String(item.tag || '').toLowerCase()
  const content = String(item.summary || item.content || item.text || '').trim()

  // 明确排除聊天等待、思考 token、普通系统消息
  if (['assistant_waiting', 'assistant_thinking', 'thinking', 'thinking_token', 'system', 'status'].includes(type)) {
    return false
  }

  // 没有真实 stage 的 pending 消息，不算执行阶段
  if (item.pending && !stage) return false

  // “思考中 / 执行中...”是聊天等待语句，不进入查看执行过程
  if ((content === '思考中' || content === '执行中...' || content.includes('思考中')) && !stage) {
    return false
  }

  // 这些才是后端执行阶段
  const validStages = [
    'sense',
    'sensing',
    'parse',
    'parse_intent',
    'check',
    'check_conflict',
    'generate',
    'generate_policy',
    'validate',
    'validate_yang',
    'execute',
    'execute_flow',
    'verify',
    'verify_status',
    'human_approval',
  ]

  if (validStages.includes(stage)) return true

  // 兜底：有 agent_id 且有 summary 的，才当成阶段
  return Boolean((item.agent_id || item.agentId) && content)
}

const EXECUTION_STAGE_PRESENTATION = {
  sense: {
    agent: 'network_sensing',
    content: '读取当前拓扑快照与遥测状态，定位本次意图涉及的设备、链路和业务对象。',
  },
  parse: {
    agent: 'intent_planner',
    content: '解析自然语言请求，提取目标对象、动作类型、作用范围和业务约束。',
  },
  check: {
    agent: 'policy_conflict_checker',
    content: '检查参数完整性、既有策略冲突和潜在业务影响，确定流程是否可以继续。',
  },
  generate: {
    agent: 'policy_modeling',
    content: '结合拓扑关系与策略约束形成候选方案，并整理策略下发所需参数。',
  },
  validate: {
    agent: 'safety_council',
    content: '执行下发前安全校验，核对策略语义、设备能力和回滚条件。',
  },
  execute: {
    agent: 'p4_runtime_executor',
    content: '记录策略下发状态；当前置检查未通过时保持未下发，避免影响现网。',
  },
  verify: {
    agent: 'status_verifier',
    content: '回读拓扑与遥测指标，汇总业务可达性、KPI 和闭环验证状态。',
  },
  human_approval: {
    agent: 'safety_council',
    content: '高风险变更进入人工确认，等待授权后再继续执行。',
  },
}

const EXECUTION_STAGE_ORDER = ['sense', 'parse', 'check', 'generate', 'validate', 'execute', 'verify', 'human_approval']

function displayExecutionStage(stage = '') {
  const normalized = normalizeMessageTag(stage)
  if (normalized === 'sensing') return 'sense'
  if (normalized === 'parse_intent') return 'parse'
  if (normalized === 'check_conflict') return 'check'
  if (normalized === 'generate_policy') return 'generate'
  if (normalized === 'validate_yang') return 'validate'
  if (normalized === 'execute_flow' || normalized === 'acting') return 'execute'
  if (normalized === 'verify_status' || normalized === 'verifying') return 'verify'
  return normalized
}

function sortExecutionStageMessages(stages = []) {
  return [...stages].sort((left, right) => {
    const leftRank = EXECUTION_STAGE_ORDER.indexOf(displayExecutionStage(left.stage || left.name))
    const rightRank = EXECUTION_STAGE_ORDER.indexOf(displayExecutionStage(right.stage || right.name))
    const safeLeftRank = leftRank < 0 ? EXECUTION_STAGE_ORDER.length : leftRank
    const safeRightRank = rightRank < 0 ? EXECUTION_STAGE_ORDER.length : rightRank
    return safeLeftRank - safeRightRank
  })
}

function stageMessageFromMilestone(item, index, intentId, runtime) {
  const stage = item.stage || item.name || `stage-${index + 1}`
  const displayStage = displayExecutionStage(stage)
  const presentation = EXECUTION_STAGE_PRESENTATION[displayStage] || {}
  const rawAgent = String(item.agent_id || item.agentId || '').trim()
  const agent = rawAgent && !['agent', 'unknown'].includes(rawAgent.toLowerCase())
    ? rawAgent
    : (presentation.agent || stage)
  const status = item.status || runtime?.status || 'running'
  const content = String(item.summary || item.content || item.text || '').trim()
    || presentation.content
    || '该执行阶段已被后台记录，暂未返回更详细的过程说明。'

  return {
    id: item.id || `${intentId || 'intent'}-runtime-stage-${stage}-${index}`,
    role: 'assistant',
    tag: `${agent} · ${status}`,
    content,
    intentId,
    stage,
    thoughtOnly: true,
    type: 'execution_stage',
  }
}

function enrichExecutionStages(intentId, runtime, stages = []) {
  if (!intentId) return stages

  const enriched = [...stages]
  const seenContent = new Set(enriched.map((item) => normalizeMessageContent(item.content || item.summary)))
  const hasStage = (stageName) => enriched.some((item) => displayExecutionStage(item.stage || item.name) === stageName)
  const openClarification = hasOpenClarificationQuestion(intentId)
  const answeredClarification = hasAnsweredCurrentClarification(intentId)
  let status = statusText(runtime?.status, runtime?.statusClass)
  if (status === '等待补充' && answeredClarification && !openClarification) {
    status = '执行中'
  }

  const addStage = (stage, tag, content) => {
    const normalized = normalizeMessageContent(content)
    if (!normalized || seenContent.has(normalized) || hasStage(stage)) return
    seenContent.add(normalized)
    enriched.push({
      id: `${intentId}-supplemental-stage-${stage}`,
      role: 'assistant',
      tag,
      content,
      intentId,
      stage,
      thoughtOnly: true,
      type: 'execution_stage',
    })
  }

  addStage(
    'sense',
    'Sense · context',
    '读取当前会话、拓扑快照与遥测状态，定位本次意图可能影响的设备、链路和业务对象。'
  )
  addStage(
    'parse',
    'Parse · intent',
    '解析自然语言请求，抽取目标对象、动作类型、范围约束和需要保留的业务条件。'
  )

  if (openClarification) {
    addStage(
      'check',
      'Check · need clarification',
      '参数完整性检查发现关键对象或范围不足，执行链路暂停并等待用户补充信息。'
    )
    return sortExecutionStageMessages(enriched)
  }

  if (answeredClarification) {
    addStage(
      'check',
      'Check · clarified',
      '已接收补充信息，并将补充参数合并到原始意图，继续沿同一个执行链路推进。'
    )
  }

  addStage('check', 'Check · constraints', EXECUTION_STAGE_PRESENTATION.check.content)
  addStage('generate', 'Plan · policy draft', EXECUTION_STAGE_PRESENTATION.generate.content)
  addStage('validate', 'Validate · safety', EXECUTION_STAGE_PRESENTATION.validate.content)
  addStage('execute', 'Act · delivery', EXECUTION_STAGE_PRESENTATION.execute.content)

  if (status !== '执行中') {
    addStage('verify', 'Verify · closed loop', EXECUTION_STAGE_PRESENTATION.verify.content)
  }

  return sortExecutionStageMessages(enriched)
}

function collapsedThreadMessages(thread) {
  if (!thread || thread.role !== 'assistant' || isPureChatActive.value) return []

  const intentId = threadIntentId(thread)
  const runtime = intentRuntimeInfo(intentId)

  // 1. 如果已经有冻结的最终阶段，优先用冻结阶段
  if (runtime?.stages?.length) {
    const frozenStages = runtime.stages
      .filter(isExecutionStageItem)
      .map((item, index) => stageMessageFromMilestone(item, index, intentId, runtime))
    return enrichExecutionStages(intentId, runtime, frozenStages)
  }

  // 2. 运行中/历史中统一从 milestones 生成执行阶段
  const milestones = (runtime?.milestones || [])
    .filter(isExecutionStageItem)

  if (milestones.length) {
    return enrichExecutionStages(
      intentId,
      runtime,
      milestones.map((item, index) => stageMessageFromMilestone(item, index, intentId, runtime))
    )
  }

  // 3. 没有真实阶段时，不要把“思考中”塞进来
  return enrichExecutionStages(intentId, runtime, [])
}

function hasCollapsedThoughts(thread) {
  return collapsedThreadMessages(thread).length > 0
}

function isThoughtThreadExpanded(threadId) {
  return expandedThoughtThreads.value.has(threadId)
}

function toggleThoughtThread(threadId) {
  const next = new Set(expandedThoughtThreads.value)
  if (next.has(threadId)) next.delete(threadId)
  else next.add(threadId)
  expandedThoughtThreads.value = next
}

function toggleTopologyDetail() {
  if (workspaceMode.value !== 'topology') {
    workspaceMode.value = 'topology'
    network.selectNode(null)
  }

  topologyDetailVisible.value = !topologyDetailVisible.value

  if (!topologyDetailVisible.value) {
    nextTick(() => {
      topologyViewKey.value += 1
    })
  }
}

function handleQuickIntent(payload = {}) {
  if (network.isGlobalIntentLocked) return
  const text = typeof payload === 'string' ? payload : payload.text
  if (!text) return
  network.intentText = text
  if (payload.priority) network.priority = payload.priority
  network.isComposing = true
  topologyDetailVisible.value = false
  quickReturnPending.value = true

  requestAnimationFrame(() => {
    setTimeout(() => {
      setWorkspaceMode('chat')
      nextTick(() => {
        scrollConversationToBottom()
        setTimeout(() => { quickReturnPending.value = false }, 220)
      })
    }, 120)
  })
}

function openNetworkView() {
  router.push('/dashboard/overview')
}

function startNewIntent() {
  cancelHistoryTitleEdit()
  persistActiveConversation()
  network.startNewIntent()
  conversationStarting.value = false
  network.intentText = ''
  historyViewLockedIds.value.clear()
  typedConversationMessages.value = []
  typedConversationOwnerKey.value = ''
  suppressNextTypewriter.value = true
  network.resetPipeline()
  setWorkspaceMode('chat')
}

function selectHistoryIntent(intent) {
  conversationStarting.value = false
  cancelHistoryTitleEdit()
  persistActiveConversation()
  suppressNextTypewriter.value = true
  markConversationSet(historyViewLockedIds, intent)
  network.selectIntent(intent)
  markConversationSet(historyViewLockedIds, network.activeIntent)
  hydrateActiveConversationFromServer(network.activeIntent)
  setWorkspaceMode('chat')
  nextTick(() => {
    hydrateActiveConversationFromServer(network.activeIntent)
    syncTypedConversationMessages(presentedConversationMessages.value, { animate: false })
    typedConversationOwnerKey.value = primaryConversationKey(network.activeIntent)
  })
}

async function previewIntentOnly() {
  const text = String(network.intentText || '').trim()
  if (!text || previewLoading.value || network.isComposerLocked) return
  previewLoading.value = true
  previewResult.value = null
  try {
    previewResult.value = await previewIntent({ text, userId: 'operator' })
  } catch (error) {
    previewResult.value = { message: error?.detail || error?.message || '预览请求失败' }
  } finally {
    previewLoading.value = false
  }
}

async function runIntent() {
  if (network.isComposerLocked) return

  setWorkspaceMode('chat')
  const active = network.activeIntent

  if (network.interactionMode === network.INTERACTION_MODE.AWAITING_INPUT && active?.id) {
    const answer = String(network.intentText || '').trim()
    if (!answer) return

    network.isComposing = false
    const result = await network.answerClarification(active.activeRunId || active.id, answer)
    if (result) {
      network.intentText = ''
      hydrateActiveConversationFromServer(network.activeIntent || active)
    }
    return
  }

  if (active?.id) {
    active._historyLoaded = false
    unmarkConversationSet(historyViewLockedIds, active)
    unmarkConversationSet(animatedConversationIds, active)
    typedConversationOwnerKey.value = primaryConversationKey(active)
    pinCurrentConversation(active)
  }

  // 只要当前页面已经处在一个对话里，就继续追加到当前对话；
  // 点击“新建意图”会清空 active，此时下一句才会创建新历史。
  const shouldAppendToActive = Boolean(active?.id)

  if (!active?.id) conversationStarting.value = true
  const intent = await network.submitIntent({ appendToActive: shouldAppendToActive })
  if (intent) {
    ensureConversationTitle(intent)
    hydrateActiveConversationFromServer(intent)
    network.isComposing = false
  } else if (!network.activeIntent) {
    conversationStarting.value = false
  }
}

async function stopActiveExecution() {
  const runtimeId = network.cancellableRuntimeId
  if (!runtimeId) return
  await network.cancelIntent(runtimeId)
}

function pinCurrentConversation(active) {
  if (!active?.id) return
  const messages = snapshotMessagesForPersistence()
  active.dialogMessages = messages
}

function cloneReport(report) {
  if (!report) return null
  return JSON.parse(JSON.stringify(report))
}

function setWorkspaceMode(mode) {
  if (workspaceMode.value === mode) {
    if (mode === 'topology') topologyDetailVisible.value = false
    return
  }

  if (workspaceMode.value === 'topology') {
    network.selectNode(null)
  }

  workspaceMode.value = mode

  if (mode === 'topology') {
    topologyDetailVisible.value = false
    network.selectNode(null)
    topologyViewKey.value += 1
  }
}
function finalPresentationForIntent(intentId) {
  if (!intentId) return null
  const active = network.activeIntent
  return active?.finalPresentation?.[intentId] || null
}
</script>

<style scoped>
.preview-result { display:grid; gap:4px; margin:0 0 9px; padding:10px 12px; border:1px solid #7dd3fc; border-radius:10px; color:#075985; background:#f0f9ff; font-size:12px; }
.preview-result strong { font-weight:850; }
.preview-result small { color:#64748b; line-height:1.45; }
.preview-btn { border:1px solid #7dd3fc; border-radius:9px; padding:8px 11px; color:#075985; background:#f0f9ff; font-weight:800; cursor:pointer; }
.preview-btn:disabled { cursor:not-allowed; opacity:.55; }
.chatops-page {
  position: relative;
  height: 100%;
  box-sizing: border-box;
  overflow: hidden;
}

.history-sidebar,
.topology-workbench {
  min-width: 0;
  min-height: 0;
  box-sizing: border-box;
  border: 1px solid rgba(186, 230, 253, 0.5);
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.76), rgba(248, 250, 252, 0.42)),
    radial-gradient(circle at 0 0, rgba(14, 165, 233, 0.1), transparent 36%);
  box-shadow: 0 28px 72px rgba(15, 23, 42, 0.075), inset 0 1px 0 rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(18px);
}

.history-sidebar {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  z-index: 5;
  width: var(--sidebar-width);
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 18px;
  padding: 18px 14px 18px 18px;
  border-left: 0;
  border-radius: 0 28px 28px 0;
  overflow: hidden;
  contain: layout paint;
  transition:
    padding 0.11s cubic-bezier(0.16, 1, 0.3, 1),
    border-color 0.1s ease,
    background 0.1s ease,
    box-shadow 0.1s ease,
    backdrop-filter 0.1s ease;
  will-change: padding;
  animation: sidebarEnter 0.72s cubic-bezier(0.16, 1, 0.3, 1) both;
}

.chatops-page.collapsed .history-sidebar {
  padding: 18px 0 0 20px;
  border-color: transparent;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  backdrop-filter: none;
  pointer-events: none;
}

.sidebar-toggle,
.new-intent-btn,
.history-item,
.submit-btn,
.mode-switch button,
.inline-action,
.stage-chip {
  font: inherit;
  cursor: pointer;
}

.sidebar-header {
  position: relative;
  min-width: 0;
  min-height: 42px;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  pointer-events: auto;
}

.brand-block {
  min-width: 0;
  flex: 1;
  padding-right: 0;
  opacity: 1;
  transform: translateX(0) scale(1);
  transform-origin: left center;
  transition:
    opacity 0.08s ease,
    transform 0.1s cubic-bezier(0.16, 1, 0.3, 1);
}

.chatops-page.collapsed .brand-block {
  flex: 0 0 auto;
  width: max-content;
  max-width: 180px;
  padding-right: 0;
}

.chatops-page.collapsed .sidebar-header {
  width: max-content;
}

.chatops-page.collapsed .sidebar-header:hover .brand-block {
  opacity: 0;
  transform: translateX(-3px) scale(0.98);
}

.sidebar-toggle {
  position: relative;
  flex: 0 0 34px;
  width: 30px;
  height: 30px;
  margin-top: 0;
  display: grid;
  place-items: center;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 12px;
  color: #334155;
  background: rgba(255, 255, 255, 0.74);
  font-weight: 950;
  opacity: 1;
  transform: translateX(0);
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.06), inset 0 1px 0 rgba(255, 255, 255, 0.9);
  transition:
    opacity 0.08s ease,
    transform 0.1s cubic-bezier(0.16, 1, 0.3, 1),
    border-color 0.11s ease,
    box-shadow 0.11s ease,
    background 0.11s ease;
}

.toggle-icon {
  width: 21px;
  height: 21px;
  display: block;
}

.panel-icon {
  width: 100%;
  height: 100%;
  display: block;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.panel-icon-open {
  display: none;
}

.chatops-page.collapsed .sidebar-toggle {
  position: absolute;
  left: 0;
  top: 0;
  width: 42px;
  height: 42px;
  border-radius: 13px;
  opacity: 0;
  transform: translateX(-4px) scale(0.98);
  pointer-events: none;
}

.chatops-page.collapsed .brand-block:hover + .sidebar-toggle,
.chatops-page.collapsed .sidebar-toggle:hover {
  opacity: 1;
  transform: translateX(0) scale(1);
  pointer-events: auto;
}

.chatops-page.collapsed .panel-icon-open {
  display: block;
}

.chatops-page.collapsed .panel-icon-close {
  display: none;
}

.sidebar-body {
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  gap: 18px;
  opacity: 1;
  transform: translateX(0);
  visibility: visible;
  transition:
    opacity 0.09s ease,
    transform 0.12s cubic-bezier(0.16, 1, 0.3, 1),
    visibility 0s linear 0s;
  will-change: opacity, transform;
}

.chatops-page.collapsed .sidebar-body {
  opacity: 0;
  transform: translateX(-6px);
  visibility: hidden;
  pointer-events: none;
  transition:
    opacity 0.06s ease,
    transform 0.1s ease,
    visibility 0s linear 0.06s;
}

.sidebar-toggle:hover {
  border-color: rgba(14, 165, 233, 0.42);
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.92), rgba(224, 242, 254, 0.68)),
    radial-gradient(circle at 30% 20%, rgba(56, 189, 248, 0.28), transparent 52%);
  box-shadow: 0 16px 32px rgba(14, 165, 233, 0.14), inset 0 1px 0 rgba(255, 255, 255, 0.96);
}

.brand-block h1,
.brand-block p {
  margin: 0;
}

.brand-block h1 {
  font-size: 2.12rem;
  line-height: 1;
  font-weight: 950;
  background: linear-gradient(110deg, #111827 0%, #0f766e 34%, #2563eb 70%, #111827 100%);
  background-size: 230% auto;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: titleFlow 7s linear infinite;
  white-space: nowrap;
}

.brand-block p {
  margin-top: 9px;
  color: #475569;
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 1.8px;
  opacity: 1;
  transform: translateY(0);
  transition: opacity 0.14s ease, transform 0.16s ease;
}

.chatops-page.collapsed .brand-block p {
  opacity: 0;
  transform: translateY(-4px);
  pointer-events: none;
}

.new-intent-btn {
  height: 42px;
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 12px;
  color: #0f172a;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.92), rgba(224, 242, 254, 0.62)),
    linear-gradient(90deg, rgba(56, 189, 248, 0.14), transparent);
  box-shadow: 0 14px 26px rgba(15, 23, 42, 0.06);
  font-weight: 950;
}

.history-block {
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.history-head span,
.sidebar-status span {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.68rem;
  font-weight: 950;
  letter-spacing: 1.4px;
  text-transform: uppercase;
}

.history-head strong {
  color: #0284c7;
  font-size: 0.8rem;
}

.history-list {
  min-height: 0;
  display: grid;
  align-content: start;
  gap: 8px;
  overflow: auto;
  padding-right: 4px;
}

.history-list::-webkit-scrollbar {
  width: 6px;
}

.history-list::-webkit-scrollbar-thumb {
  border-radius: 999px;
  background: rgba(125, 211, 252, 0.44);
}

.history-item {
  min-height: 60px;
  position: relative;
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) 28px;
  align-items: center;
  gap: 10px;
  padding: 0 7px 0 12px;
  border: 1px solid rgba(203, 213, 225, 0.58);
  border-radius: 13px;
  color: inherit;
  background: rgba(255, 255, 255, 0.46);
  text-align: left;
  cursor: pointer;
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}

.history-item:hover,
.history-item.active,
.history-item.editing,
.history-item:focus-visible {
  transform: translateY(-1px);
  border-color: rgba(14, 165, 233, 0.42);
  background: rgba(255, 255, 255, 0.72);
  box-shadow: 0 16px 30px rgba(15, 23, 42, 0.07);
  outline: none;
}

.history-copy {
  min-width: 0;
}

.history-item i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #94a3b8;
}

.history-item i.running,
.history-item i.pending {
  background: #38bdf8;
  box-shadow: 0 0 12px rgba(56, 189, 248, 0.72);
}

.history-item i.success {
  background: #22c55e;
  box-shadow: 0 0 12px rgba(34, 197, 94, 0.56);
}

.history-item i.failed {
  background: #ef4444;
}

/* Watcher auto-intent —— subtle dot tint, no border/background */
.history-item.watcher i {
  background: #f59e0b;
  box-shadow: 0 0 6px rgba(245, 158, 11, 0.32);
}

.history-item strong,
.history-item small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-item strong {
  color: #0f172a;
  font-size: 0.78rem;
}

.history-item small {
  margin-top: 5px;
  color: #64748b;
  font-size: 0.66rem;
  font-weight: 800;
}

.history-edit-btn {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: rgba(71, 85, 105, 0.72);
  font-size: 0.82rem;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.18s ease, background 0.18s ease, color 0.18s ease;
}

.history-item:hover .history-edit-btn,
.history-item.editing .history-edit-btn {
  opacity: 1;
}

.history-edit-btn:hover {
  background: rgba(14, 165, 233, 0.12);
  color: #0284c7;
}

.history-title-input {
  width: 100%;
  min-width: 0;
  height: 26px;
  box-sizing: border-box;
  border: 1px solid rgba(14, 165, 233, 0.32);
  border-radius: 8px;
  outline: none;
  padding: 0 8px;
  background: rgba(255, 255, 255, 0.88);
  color: #0f172a;
  font-size: 0.76rem;
  font-weight: 800;
}

.history-title-input:focus {
  border-color: rgba(14, 165, 233, 0.58);
  box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.12);
}

.sidebar-status {
  min-height: 48px;
  display: grid;
  align-content: center;
  gap: 4px;
  padding: 0 14px;
  border: 1px solid rgba(203, 213, 225, 0.48);
  border-radius: 14px;
  background: rgba(248, 250, 252, 0.58);
}

.sidebar-status strong {
  color: #0f766e;
  font-size: 0.82rem;
}


.account-area {
  position: relative;
  margin-top: auto;
}

.sidebar-profile {
  width: 100%;
  min-height: 50px;
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 16px;
  align-items: center;
  gap: 9px;
  padding: 7px 9px;
  border: 0;
  border-radius: 16px;
  color: inherit;
  background: rgba(255, 255, 255, 0.72);
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.06), inset 0 1px 0 rgba(255, 255, 255, 0.86);
  text-align: left;
  cursor: pointer;
  transition: transform 0.18s ease, background 0.18s ease, box-shadow 0.18s ease;
}

.sidebar-profile:hover {
  transform: translateY(-1px);
  background: rgba(255, 255, 255, 0.86);
  box-shadow: 0 14px 30px rgba(15, 23, 42, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.94);
}

.profile-avatar,
.account-avatar-btn {
  position: relative;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 0;
  padding: 0;
  cursor: pointer;
}
.profile-avatar {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
}

.profile-avatar img,
.account-avatar-btn img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  border-radius: 50%;
}

.profile-avatar b,
.account-avatar-btn span {
  position: relative;
  z-index: 1;
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  border-radius: 50%;
  color: #ffffff;
  background:
    radial-gradient(circle at 35% 30%, rgba(255, 255, 255, 0.38), transparent 28%),
    linear-gradient(135deg, #94a3b8 0%, #64748b 48%, #475569 100%);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.36),
    0 8px 18px rgba(71, 85, 105, 0.22);
  font-size: 0.7rem;
  font-weight: 860;
  letter-spacing: 0.02em;
}

.profile-avatar::after {
  content: '更换头像';
  position: absolute;
  left: 50%;
  bottom: calc(100% + 8px);
  transform: translateX(-50%) translateY(4px);
  width: max-content;
  padding: 5px 8px;
  border-radius: 9px;
  color: #ffffff;
  background: rgba(15, 23, 42, 0.88);
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.16);
  font-size: 0.66rem;
  font-weight: 760;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.16s ease, transform 0.16s ease;
  z-index: 50;
}

.profile-avatar:hover::after {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}

.profile-meta {
  min-width: 0;
  display: grid;
  gap: 1px;
}

.profile-meta strong,
.profile-meta small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-meta strong {
  color: #0f172a;
  font-size: 0.82rem;
  font-weight: 760;
}

.profile-meta small {
  color: #64748b;
  font-size: 0.68rem;
  font-weight: 650;
}

.profile-chevron {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: #0f172a;
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.account-menu-card {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + 10px);
  z-index: 40;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  padding: 8px;
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: 0 24px 70px rgba(15, 23, 42, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.92);
  color: #111827;
  backdrop-filter: blur(14px) saturate(1.08);
}

.account-menu-head {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  padding: 7px 7px 9px;
}

.account-avatar-btn {
  position: relative;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 0;
  cursor: pointer;
}

.account-avatar-btn em {
  position: absolute;
  left: 50%;
  bottom: -32px;
  transform: translateX(-50%) translateY(4px);
  width: max-content;
  padding: 5px 8px;
  border-radius: 8px;
  color: #ffffff;
  background: rgba(15, 23, 42, 0.86);
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.18);
  font-size: 0.68rem;
  font-style: normal;
  font-weight: 760;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.account-avatar-btn:hover em {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}

.profile-file-input {
  display: none;
}

.account-identity {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.account-name-btn,
.account-name-input {
  width: 100%;
  border: 0;
  outline: 0;
  padding: 0;
  color: #0f172a;
  background: transparent;
  font: inherit;
  font-size: 0.98rem;
  font-weight: 760;
  text-align: left;
}

.account-name-btn {
  cursor: text;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-name-input {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  height: 28px;
  padding: 0 8px;
  border: 1px solid rgba(148, 163, 184, 0.34);
  border-radius: 9px;
  background: rgba(248, 250, 252, 0.92);
}
.account-identity small {
  overflow: hidden;
  color: #64748b;
  font-size: 0.8rem;
  font-weight: 640;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-menu-section {
  padding: 7px 0;
  border-top: 1px solid rgba(15, 23, 42, 0.08);
}

.account-menu-section:first-of-type {
  border-top: 0;
}

.account-menu-item {
  width: 100%;
  min-height: 40px;
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr) auto;
  align-items: center;
  gap: 5px;
  padding: 7px 9px;
  border: 0;
  border-radius: 11px;
  color: #111827;
  background: transparent;
  font: inherit;
  font-size: 0.86rem;
  font-weight: 680;
  text-align: left;
  cursor: pointer;
}

.account-menu-item:hover {
  background: rgba(15, 23, 42, 0.055);
}

.account-menu-item.is-disabled,
.account-menu-item:disabled {
  cursor: default;
  color: #111827;
  opacity: 1;
}

.account-menu-item.is-disabled:hover,
.account-menu-item:disabled:hover {
  background: transparent;
}

.menu-icon {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  color: #111827;
  font-size: 1rem;
}

.model-entry strong {
  color: #64748b;
  font-size: 0.72rem;
  font-weight: 700;
}

.model-options {
  display: grid;
  gap: 4px;
  padding: 2px 8px 6px 44px;
}

.model-options button {
  display: grid;
  gap: 2px;
  padding: 8px 10px;
  border: 0;
  border-radius: 10px;
  color: #334155;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.model-options button:hover,
.model-options button.active {
  background: rgba(59, 130, 246, 0.09);
  color: #1d4ed8;
}

.model-options span {
  font-size: 0.82rem;
  font-weight: 820;
}

.model-options small {
  color: #64748b;
  font-size: 0.68rem;
  font-weight: 650;
}

.danger-section {
  padding-bottom: 0;
}

.logout-item {
  color: #991b1b;
}

.logout-item .menu-icon {
  color: #991b1b;
}

.account-menu-fade-enter-active,
.account-menu-fade-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.account-menu-fade-enter-from,
.account-menu-fade-leave-to {
  opacity: 0;
  transform: translateY(6px) scale(0.98);
}

.topology-workbench {
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  bottom: 0;
  z-index: 1;
  display: block;
  padding: 22px;
  margin: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
  backdrop-filter: none;
}

.workspace-card {
  position: relative;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 14px;
  height: 100%;
  box-sizing: border-box;
  padding: 18px 18px 118px;
  border: 0;
  border-radius: 28px;
  overflow: hidden;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.68), rgba(248, 250, 252, 0.32)),
    linear-gradient(120deg, rgba(14, 165, 233, 0.08), transparent 36%, rgba(129, 140, 248, 0.08));
  box-shadow:
    0 26px 70px rgba(15, 23, 42, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(20px) saturate(1.12);
  animation: panelMaterialize 0.58s cubic-bezier(0.16, 1, 0.3, 1) 0.12s both;
  transition: box-shadow 0.35s ease, background 0.35s ease;
}

.workspace-card::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, rgba(255, 255, 255, 0.62), transparent 18%, transparent 82%, rgba(255, 255, 255, 0.46)),
    linear-gradient(180deg, rgba(255, 255, 255, 0.52), transparent 24%, rgba(224, 242, 254, 0.22));
}

.workspace-head,
.workspace-stage {
  position: relative;
  z-index: 1;
}

.workspace-head {
  min-height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 2px 2px 0;
}

.workspace-title {
  min-width: 0;
}

.workspace-title span {
  display: block;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.68rem;
  font-weight: 950;
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.workspace-title strong {
  display: block;
  margin-top: 6px;
  overflow: hidden;
  color: #0f172a;
  font-size: 1.2rem;
  line-height: 1.18;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mode-switch {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.mode-switch button {
  min-width: 86px;
  height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid transparent;
  border-radius: 999px;
  color: #64748b;
  background: transparent;
  font-size: 0.78rem;
  font-weight: 950;
  transition: color 0.14s ease, background 0.14s ease, box-shadow 0.14s ease, transform 0.14s ease;
}

.mode-switch button.active {
  color: #0f172a;
  border-color: rgba(203, 213, 225, 0.62);
  background: rgba(255, 255, 255, 0.78);
  box-shadow: 0 12px 26px rgba(15, 23, 42, 0.09), inset 0 1px 0 rgba(255, 255, 255, 0.92);
}

.mode-switch button:hover {
  color: #0f172a;
  background: rgba(255, 255, 255, 0.52);
  transform: translateY(-1px);
}

.mode-switch svg {
  position: static;
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.workspace-stage {
  min-height: 0;
  overflow: hidden;
  border-radius: 20px;
  background: transparent;
}

.conversation-view {
  height: 100%;
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
}

.conversation-empty-state {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding-bottom: 280px;
  box-sizing: border-box;
  color: #0f172a;
  text-align: center;
  user-select: none;
  animation: emptyStateIn 0.5s cubic-bezier(0.16, 1, 0.3, 1) both;
}

.conversation-empty-state > p {
  margin: 0 0 10px;
  color: #1683b7;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.66rem;
  font-weight: 800;
  letter-spacing: 2.2px;
}

.conversation-empty-state h2 {
  margin: 0;
  color: #142033;
  font-size: clamp(1.55rem, 2.2vw, 2.05rem);
  font-weight: 760;
  line-height: 1.3;
  letter-spacing: 0;
}

.conversation-empty-state > span {
  max-width: 590px;
  margin-top: 13px;
  color: #718096;
  font-size: 0.86rem;
  font-weight: 600;
  line-height: 1.75;
}

.empty-capabilities {
  position: absolute;
  top: calc(52% + 78px);
  left: 50%;
  z-index: 19;
  width: min(820px, calc(100% - 96px));
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  transform: translateX(-50%);
  border-top: 1px solid rgba(148, 163, 184, 0.2);
  user-select: none;
  pointer-events: none;
}

.empty-capability {
  min-width: 0;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  padding: 18px 22px;
}

.empty-capability + .empty-capability {
  border-left: 1px solid rgba(148, 163, 184, 0.18);
}

.empty-capability > span:last-child {
  min-width: 0;
  display: grid;
  gap: 4px;
  text-align: left;
}

.empty-capability strong {
  color: #26364b;
  font-size: 0.85rem;
  font-weight: 780;
}

.empty-capability small {
  overflow: hidden;
  color: #8290a3;
  font-size: 0.7rem;
  font-weight: 600;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-symbol {
  position: relative;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(125, 211, 252, 0.5);
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.66);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.94);
}

.capability-symbol::before,
.capability-symbol::after,
.capability-symbol i {
  content: '';
  position: absolute;
  box-sizing: border-box;
}

.sense-symbol::before {
  width: 14px;
  height: 14px;
  border: 1px solid #2b9dcb;
  border-radius: 50%;
}

.sense-symbol::after {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: #2b9dcb;
}

.sense-symbol i {
  width: 20px;
  height: 8px;
  border-top: 1px solid rgba(43, 157, 203, 0.45);
  border-radius: 50%;
}

.intent-symbol::before {
  width: 17px;
  height: 1px;
  background: #537fc4;
  box-shadow: 0 -5px 0 rgba(83, 127, 196, 0.6), 0 5px 0 rgba(83, 127, 196, 0.6);
}

.intent-symbol::after,
.intent-symbol i {
  width: 4px;
  height: 4px;
  border: 1px solid #537fc4;
  border-radius: 50%;
  background: #ffffff;
}

.intent-symbol::after { left: 6px; top: 8px; }
.intent-symbol i { right: 6px; bottom: 8px; }

.verify-symbol::before {
  width: 15px;
  height: 15px;
  border: 1px solid #159b83;
  border-radius: 50%;
}

.verify-symbol::after {
  width: 7px;
  height: 4px;
  border-left: 1.5px solid #159b83;
  border-bottom: 1.5px solid #159b83;
  transform: translateY(-1px) rotate(-45deg);
}

.capability-fade-enter-active,
.capability-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.28s cubic-bezier(0.16, 1, 0.3, 1);
}

.capability-fade-enter-from,
.capability-fade-leave-to {
  opacity: 0;
  transform: translate(-50%, 8px);
}

.conversation-scroll {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 22px;
  overflow: auto;
  scroll-behavior: smooth;
  padding: 34px clamp(26px, 5vw, 72px) 30px;
}

.conversation-scroll > .chat-thread {
  width: min(1080px, 100%);
  margin-inline: auto;
}

.conversation-scroll::-webkit-scrollbar {
  width: 7px;
}

.conversation-scroll::-webkit-scrollbar-thumb {
  border-radius: 999px;
  background: rgba(125, 211, 252, 0.42);
}

.conversation-loading {
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #0f172a;
  user-select: none;
}

.conversation-loading-spinner {
  width: 30px;
  height: 30px;
  margin-bottom: 8px;
  border: 2px solid rgba(148, 163, 184, 0.24);
  border-top-color: #38bdf8;
  border-right-color: #818cf8;
  border-radius: 50%;
  animation: conversationLoadingSpin 0.72s linear infinite;
}

.conversation-loading strong {
  font-size: 0.95rem;
  font-weight: 750;
}

.conversation-loading p {
  margin: 0;
  color: #64748b;
  font-size: 0.8rem;
  font-weight: 600;
}

@keyframes conversationLoadingSpin {
  to { transform: rotate(360deg); }
}

.chat-thread {
  width: 100%;
  display: flex;
}

.user-thread {
  justify-content: flex-start;
}

.agent-thread-row {
  justify-content: flex-start;
}

.user-thread-row {
  justify-content: flex-end;
}

.user-plain-thread {
  width: min(760px, 52%);
  justify-items: end;
}

.user-plain-thread .agent-step-list {
  width: 100%;
  justify-items: end;
}

.user-plain-step {
  justify-content: flex-end;
  text-align: left;
}

.user-plain-step .step-content {
  align-items: flex-end;
  max-width: 100%;
}


.user-plain-step .thread-meta {
  justify-content: flex-end;
  text-align: right;
}

.user-plain-step .thread-meta span {
  color: #0f172a;
}

.user-plain-step .step-content p {
  color: #111827;
  font-weight: 800;
  max-width: 100%;
  text-align: left;
  line-height: 1.85;
  overflow-wrap: anywhere;
  word-break: break-word;
  user-select: text;
  -webkit-user-select: text;
}

.agent-thread {
  width: min(820px, 76%);
  display: grid;
  gap: 10px;
  padding: 4px 0 4px;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  backdrop-filter: none;
}

.agent-thread-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(203, 213, 225, 0.46);
}

.agent-thread-head > div {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.agent-thread-head span {
  color: #0f172a;
  font-size: 0.9rem;
  font-weight: 1000;
}

.agent-thread-head strong {
  color: #64748b;
  font-size: 0.68rem;
  font-weight: 950;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.agent-thread-head em {
  flex: 0 0 auto;
  color: #0284c7;
  font-style: normal;
  font-size: 0.66rem;
  font-weight: 950;
}

.agent-step-list {
  display: grid;
  gap: 0;
}

.agent-step {
  position: relative;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  gap: 12px;
  padding: 2px 0 18px;
}

.agent-step:not(:last-child)::after {
  content: '';
  position: absolute;
  left: 4px;
  top: 22px;
  bottom: -1px;
  width: 1px;
  background: linear-gradient(180deg, rgba(56, 189, 248, 0.42), rgba(148, 163, 184, 0.16));
}

.step-dot {
  position: relative;
  z-index: 1;
  width: 9px;
  height: 9px;
  margin-top: 9px;
  border: 0;
  border-radius: 50%;
  background: #94a3b8;
  box-shadow: none;
  opacity: 0;
}

.agent-step.final .step-dot {
  background: #22c55e;
  box-shadow: none;
}

.step-content {
  min-width: 0;
  display: grid;
  gap: 7px;
}

.step-content p {
  margin: 0;
  color: #1e293b;
  font-size: 0.96rem;
  line-height: 1.78;
  font-weight: 690;
  letter-spacing: 0;
  user-select: text;
  -webkit-user-select: text;
}

.thread-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
}


.thread-meta span {
  color: #0f172a;
  font-size: 0.76rem;
  font-weight: 1000;
}

.thread-meta em {
  min-width: 0;
  max-width: 260px;
  overflow: hidden;
  padding: 2px 8px;
  border-radius: 999px;
  color: #64748b;
  background: rgba(226, 232, 240, 0.6);
  font-style: normal;
  font-size: 0.58rem;
  font-weight: 950;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-result-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 3px;
}

.agent-step-summary {
  width: min(680px, 100%);
  margin: 12px 0 4px;
}

.agent-step-summary :deep(.exec-summary-card) {
  max-width: 100%;
  margin: 0;
}

.message-row {
  width: min(660px, 72%);
  justify-self: start;
  align-self: flex-start;
  display: grid;
  gap: 8px;
  color: #0f172a;
}

.message-row.user {
  width: fit-content;
  max-width: min(520px, 58%);
  justify-self: end;
  align-self: flex-end;
  color: #111827;
  text-align: left;
}

.message-row.assistant {
  text-align: left;
}

.message-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
}

.message-row.user .message-meta {
  justify-content: flex-end;
}

.message-meta span {
  color: #0f172a;
  font-size: 0.78rem;
  font-weight: 950;
}

.message-meta em {
  min-width: 0;
  max-width: 230px;
  overflow: hidden;
  padding: 2px 7px;
  border-radius: 999px;
  color: #64748b;
  background: rgba(241, 245, 249, 0.72);
  font-style: normal;
  font-size: 0.6rem;
  font-weight: 950;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.message-row.user .message-meta em {
  color: #475569;
  background: rgba(226, 232, 240, 0.68);
}

.message-row p {
  margin: 0;
  color: #1e293b;
  font-size: 0.98rem;
  line-height: 1.82;
  font-weight: 650;
  letter-spacing: 0;
}

.message-row.user p {
  font-weight: 720;
  text-align: left;
  direction: ltr;
  unicode-bidi: plaintext;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.inline-action {
  width: fit-content;
  height: 32px;
  margin-top: 2px;
  padding: 0 12px;
  border: 1px solid rgba(14, 165, 233, 0.26);
  border-radius: 999px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.5);
  font-size: 0.72rem;
  font-weight: 950;
}

.message-row.user .inline-action,
.message-row.user .execution-report-card {
  justify-self: end;
}

.execution-report-card {
  position: relative;
  width: min(720px, 100%);
  margin-top: 10px;
  padding: 18px;
  overflow: hidden;
  border: 1px solid rgba(14, 165, 233, 0.22);
  border-radius: 22px;
  background:
    radial-gradient(circle at 8% 0, rgba(14, 165, 233, 0.18), transparent 34%),
    radial-gradient(circle at 92% 12%, rgba(99, 102, 241, 0.16), transparent 30%),
    linear-gradient(145deg, rgba(255, 255, 255, 0.92), rgba(241, 245, 249, 0.72));
  box-shadow:
    0 24px 70px rgba(15, 23, 42, 0.12),
    inset 0 1px 0 rgba(255, 255, 255, 0.94);
  text-align: left;
}

.execution-report-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 3px;
  background: linear-gradient(90deg, #38bdf8, #818cf8, #22c55e);
  opacity: 0.82;
}

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
  border: 1px solid rgba(34, 197, 94, 0.24);
  border-radius: 999px;
  color: #047857;
  background: rgba(220, 252, 231, 0.72);
  font-size: 0.66rem;
  font-weight: 1000;
  box-shadow: 0 8px 22px rgba(34, 197, 94, 0.12);
}

.report-summary {
  margin: 0 0 14px;
  color: #334155;
  font-size: 0.86rem;
  line-height: 1.75;
  font-weight: 760;
}

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

/* ── Pipeline execution strip ── */
.execution-strip {
  display: flex;
  flex-direction: column;
  gap: 0;
  overflow: hidden;
  border-top: 1px solid rgba(203, 213, 225, 0.32);
  /* no background — transparent */
}

/* ── Top progress bar ── */
.pipeline-progress {
  height: 2px;
  background: rgba(203, 213, 225, 0.38);
}

.pipeline-progress-bar {
  display: block;
  height: 100%;
  border-radius: 0 2px 2px 0;
  background: linear-gradient(90deg, #0ea5e9, #22c55e);
  transition: width 0.45s cubic-bezier(0.22, 1, 0.36, 1);
}

/* ── Chip track ── */
.pipeline-track {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 0;
  padding: 6px 12px 8px;
  overflow-x: auto;
}

/* ── Stage chip (1:2 height:width ratio, compact) ── */
.stage-chip {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0;
  height: 26px;
  min-width: 52px;
  padding: 2px 8px;
  border: 1px solid rgba(203, 213, 225, 0.48);
  border-radius: 8px;
  color: #94a3b8;
  background: rgba(248, 250, 252, 0.52);
  font: inherit;
  cursor: pointer;
  transition: border-color 0.22s ease, color 0.22s ease, background 0.22s ease, box-shadow 0.22s ease, transform 0.18s ease;
}

.stage-chip:hover {
  transform: translateY(-1px);
}

.stage-chip .stage-key {
  font-size: 0.52rem;
  font-weight: 1000;
  letter-spacing: 0.03em;
  line-height: 1;
}

.stage-chip .stage-name {
  display: none;
}

/* ── Waiting ── */
.stage-chip.waiting {
  color: #94a3b8;
  border-color: rgba(203, 213, 225, 0.36);
  background: rgba(248, 250, 252, 0.32);
}

/* ── Running: pulse glow ── */
.stage-chip.running {
  color: #0369a1;
  border-color: rgba(56, 189, 248, 0.58);
  background: rgba(224, 242, 254, 0.72);
  animation: pipelinePulse 1.8s ease-in-out infinite;
}

/* ── Complete: green solid ── */
.stage-chip.complete {
  color: #047857;
  border-color: rgba(34, 197, 94, 0.32);
  background: rgba(220, 252, 231, 0.62);
}

/* ── Failed: red ── */
.stage-chip.failed {
  color: #b91c1c;
  border-color: rgba(248, 113, 113, 0.36);
  background: rgba(254, 226, 226, 0.66);
}

/* ── Arrows between chips ── */
.stage-arrow {
  flex: 0 0 auto;
  width: 16px;
  text-align: center;
  color: #cbd5e1;
  font-size: 0.62rem;
  font-weight: 900;
  transition: color 0.3s ease, opacity 0.3s ease;
}

.stage-arrow.done {
  color: #86efac;
}

.stage-arrow.active {
  color: #0ea5e9;
  animation: arrowFlow 1.2s ease-in-out infinite;
}

/* ── Keyframe animations ── */
@keyframes pipelinePulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.44); }
  50%      { box-shadow: 0 0 0 5px rgba(56, 189, 248, 0); }
}

@keyframes arrowFlow {
  0%, 100% { opacity: 0.35; }
  50%      { opacity: 1; }
}

.topology-shell {
  min-width: 0;
  min-height: 0;
  height: 100%;
  box-sizing: border-box;
  padding: 0;
  border: 0;
  border-radius: 20px;
  background: transparent;
  box-shadow: none;
  overflow: hidden;
}

.intent-composer {
  position: absolute;
  left: 50%;
  bottom: 30px;
  z-index: 20;
  width: min(920px, calc(100% - 88px));
  display: grid;
  transform: translate3d(-50%, 0, 0);
  padding: 10px 10px 10px 16px;
  border: 1px solid rgba(148, 163, 184, 0.32);
  border-radius: 24px;
  background:
    linear-gradient(135deg, rgba(255,255,255,0.9), rgba(248,250,252,0.72)),
    radial-gradient(circle at 0 0, rgba(56, 189, 248, 0.1), transparent 42%);
  box-shadow: 0 24px 54px rgba(15, 23, 42, 0.1), inset 0 1px 0 rgba(255, 255, 255, 0.94);
  transition:
    bottom 0.52s cubic-bezier(0.16, 1, 0.3, 1),
    transform 0.52s cubic-bezier(0.16, 1, 0.3, 1),
    width 0.34s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.2s ease,
    border-color 0.2s ease;
  will-change: bottom, transform;
}

.topology-workbench.conversation-empty .intent-composer {
  bottom: 48%;
  width: min(860px, calc(100% - 88px));
  transform: translate3d(-50%, 50%, 0);
}

.topology-workbench.conversation-engaged .intent-composer {
  bottom: 30px;
  transform: translate3d(-50%, 0, 0);
}

.intent-composer.active {
  border-color: rgba(14, 165, 233, 0.52);
  box-shadow: 0 28px 62px rgba(14, 165, 233, 0.14), inset 0 1px 0 rgba(255, 255, 255, 0.96);
}

.intent-composer.locked {
  border-color: rgba(148, 163, 184, 0.28);
  background:
    linear-gradient(135deg, rgba(248, 250, 252, 0.9), rgba(241, 245, 249, 0.72)),
    radial-gradient(circle at 0 0, rgba(100, 116, 139, 0.1), transparent 42%);
  box-shadow: 0 18px 42px rgba(15, 23, 42, 0.075), inset 0 1px 0 rgba(255, 255, 255, 0.88);
}

.input-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: end;
}

textarea {
  width: 100%;
  min-height: 50px;
  max-height: 108px;
  box-sizing: border-box;
  resize: vertical;
  padding: 12px 8px;
  border: 0;
  border-radius: 18px;
  outline: 0;
  color: #0f172a;
  background: transparent;
  font: inherit;
  font-size: 0.92rem;
  font-weight: 700;
  line-height: 1.55;
  text-align: left;
  direction: ltr;
  unicode-bidi: plaintext;
  user-select: text;
  -webkit-user-select: text;
}

textarea:disabled {
  cursor: not-allowed;
  color: #94a3b8;
  -webkit-text-fill-color: #94a3b8;
}

textarea:focus {
  box-shadow: none;
}

.input-actions {
  display: grid;
  justify-items: end;
}

.submit-btn {
  width: 88px;
  height: 42px;
  border: 0;
  border-radius: 999px;
  color: #ffffff;
  background: linear-gradient(135deg, #0f172a, #1e40af);
  box-shadow: 0 16px 30px rgba(15, 23, 42, 0.18);
  font-weight: 950;
}

.submit-btn:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

@keyframes titleFlow {
  to { background-position: 220% center; }
}

@keyframes sidebarEnter {
  0% {
    opacity: 0;
    transform: translateX(-42px);
    filter: blur(10px);
  }
  58% {
    opacity: 1;
    transform: translateX(3px);
    filter: blur(0);
  }
  100% {
    opacity: 1;
    transform: translateX(0);
    filter: blur(0);
  }
}

@keyframes workbenchEnter {
  0% {
    opacity: 0;
    transform: translateX(30px) scale(0.985);
    filter: blur(12px);
  }
  64% {
    opacity: 1;
    transform: translateX(-2px) scale(1);
    filter: blur(0);
  }
  100% {
    opacity: 1;
    transform: translateX(0) scale(1);
    filter: blur(0);
  }
}

@keyframes panelMaterialize {
  0% {
    opacity: 0;
    transform: translateY(12px);
    box-shadow:
      0 8px 28px rgba(15, 23, 42, 0.04),
      inset 0 1px 0 rgba(255, 255, 255, 0.72);
  }
  100% {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes composerEnter {
  0% {
    opacity: 0;
    transform: translateY(16px);
  }
  100% {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes emptyStateIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes profilePanelIn {
  0% {
    opacity: 0;
    transform: translateY(12px) scale(0.98);
    filter: blur(8px);
  }
  100% {
    opacity: 1;
    transform: translateY(0) scale(1);
    filter: blur(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .history-sidebar,
  .topology-workbench,
  .workspace-card,
  .intent-composer,
  .conversation-empty-state,
  .empty-capabilities {
    animation: none;
    transition: none;
  }
}

/* Keep the desktop composition above this breakpoint.  The welcome view uses
   absolute anchors there; below 1200px use the same DOM in normal flow. */
@media (max-width: 1200px) {
  .chatops-page { width:100%; min-width:0; overflow-x:hidden; overflow-y:auto; }
  .topology-workbench { position:relative; inset:auto; width:100%; min-width:0; min-height:100%; padding:18px; }
  .workspace-card { height:auto; min-height:560px; padding:16px; }
  .workspace-stage { overflow:visible; }
  .conversation-view { height:auto; min-height:0; display:block; }
  .conversation-empty-state { height:auto; min-height:260px; padding:54px 18px 30px; justify-content:flex-start; }
  .intent-composer,
  .topology-workbench.conversation-empty .intent-composer,
  .topology-workbench.conversation-engaged .intent-composer { position:relative; left:auto; bottom:auto; width:min(100%,900px); max-width:100%; min-width:0; height:auto; margin:18px auto 0; transform:none; }
  .empty-capabilities { position:relative; top:auto; left:auto; width:min(100%,820px); margin:20px auto 0; transform:none; }
}

.cancel-btn {
  min-width: 96px;
  border: 1px solid rgba(220, 38, 38, 0.45);
  border-radius: 10px;
  background: rgba(254, 242, 242, 0.9);
  color: #b91c1c;
  font-weight: 700;
  cursor: pointer;
}

.cancel-btn:disabled {
  cursor: wait;
  opacity: 0.68;
}

@media (max-width: 900px) {
  .chatops-page,
  .chatops-page.collapsed {
    height: auto;
    grid-template-columns: 1fr;
  }

  .history-sidebar {
    min-height: 76px;
  }

  .topology-workbench {
    min-height: 720px;
    padding: 12px;
  }

  .workspace-card {
    padding: 14px 14px 124px;
    border-radius: 22px;
  }

  .conversation-scroll {
    padding-inline: 22px;
  }

  .message-row,
  .message-row.user {
    width: min(100%, 620px);
  }

  .input-row {
    grid-template-columns: 1fr;
  }

  .input-actions {
    justify-items: start;
  }

  .intent-composer,
  .topology-workbench.conversation-empty .intent-composer {
    width: calc(100% - 32px);
  }

  .topology-workbench.conversation-engaged .intent-composer {
    bottom: 18px;
  }

  .topology-workbench.conversation-empty .intent-composer {
    bottom: 44%;
  }

  .conversation-empty-state {
    padding: 0 18px 230px;
  }

  .empty-capabilities {
    top: calc(56% + 74px);
    width: calc(100% - 40px);
  }

  .empty-capability {
    grid-template-columns: 28px minmax(0, 1fr);
    gap: 8px;
    padding: 14px 10px;
  }

  .capability-symbol {
    width: 28px;
    height: 28px;
  }

  .empty-capability small {
    white-space: normal;
  }
}

@media (max-width: 620px) {
  .conversation-empty-state > p,
  .conversation-empty-state > span {
    display: none;
  }

  .conversation-empty-state {
    padding-bottom: 250px;
  }

  .empty-capabilities {
    top: calc(56% + 68px);
    grid-template-columns: 1fr;
  }

  .empty-capability {
    padding: 9px 12px;
  }

  .empty-capability + .empty-capability {
    border-top: 1px solid rgba(148, 163, 184, 0.18);
    border-left: 0;
  }
}

.chat-waiting-line {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: #50617c;
  font-weight: 700;
}

.chat-waiting-line.compact {
  width: fit-content;
  padding: 6px 10px;
  border: 1px solid rgba(186, 230, 253, 0.72);
  border-radius: 999px;
  color: #64748b;
  background: rgba(255, 255, 255, 0.62);
  box-shadow: 0 10px 24px rgba(15, 23, 42, 0.045), inset 0 1px 0 rgba(255, 255, 255, 0.85);
  font-size: 0.72rem;
  font-weight: 850;
  letter-spacing: 0.02em;
}

.chat-waiting-spinner {
  width: 16px;
  height: 16px;
  border-radius: 999px;
  border: 2px solid rgba(59, 130, 246, 0.16);
  border-top-color: rgba(37, 99, 235, 0.88);
  border-right-color: rgba(14, 165, 233, 0.75);
  animation: chatWaitingSpin 0.8s linear infinite;
  flex: 0 0 auto;
}

.chat-waiting-line.compact .chat-waiting-spinner {
  width: 13px;
  height: 13px;
  border-width: 1.5px;
}

@keyframes chatWaitingSpin { to { transform: rotate(360deg); } }



/* ---- NetPulse topology detail carousel + side toggle ---- */
.topology-shell {
  position: relative;
  overflow: visible;
  isolation: isolate;
}

.topology-carousel {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  border-radius: inherit;
}

.topology-slide {
  position: absolute;
  inset: 0;
  min-width: 0;
  min-height: 0;
  transition:
    transform 0.68s cubic-bezier(0.16, 1, 0.3, 1),
    opacity 0.42s ease,
    filter 0.42s ease;
  will-change: transform, opacity, filter;
}

.topology-map-slide {
  transform: translate3d(0, 0, 0);
  opacity: 1;
}

.topology-detail-slide {
  transform: translate3d(0, 105%, 0);
  opacity: 0;
  filter: blur(8px);
  pointer-events: none;
}

.topology-carousel.details .topology-map-slide {
  transform: translate3d(0, -105%, 0);
  opacity: 0;
  filter: blur(8px);
  pointer-events: none;
}

.topology-carousel.details .topology-detail-slide {
  transform: translate3d(0, 0, 0);
  opacity: 1;
  filter: blur(0);
  pointer-events: auto;
}

.topology-side-toggle {
  position: absolute;
  left: 14px;
  top: 50%;
  z-index: 80;
  width: 42px;
  min-height: 132px;
  transform: translateY(-50%);
  display: grid;
  place-items: center;
  gap: 8px;
  padding: 12px 7px;
  border: 1px solid rgba(125, 211, 252, 0.48);
  border-radius: 999px;
  color: #0369a1;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 20px 46px rgba(14, 165, 233, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.92);
  cursor: pointer;
  transition:
    transform 0.24s cubic-bezier(0.16, 1, 0.3, 1),
    color 0.2s ease,
    border-color 0.2s ease,
    box-shadow 0.2s ease;
}

.topology-side-toggle:hover {
  transform: translateY(-50%) translateX(3px);
  color: #1d4ed8;
  border-color: rgba(99, 102, 241, 0.48);
  box-shadow: 0 24px 56px rgba(99, 102, 241, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.96);
}

.topology-side-toggle.active svg {
  transform: rotate(180deg);
}

.topology-side-toggle span {
  writing-mode: vertical-rl;
  color: currentColor;
  font-size: 0.68rem;
  font-weight: 1000;
  letter-spacing: 0.08em;
}

.topology-side-toggle svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.24s ease;
}

.system-detail-panel {
  height: 100%;
  box-sizing: border-box;
  overflow: auto;
  padding: clamp(22px, 3vw, 34px);
  border-radius: 20px;
  background:
    radial-gradient(circle at 12% 6%, rgba(14, 165, 233, 0.14), transparent 32%),
    radial-gradient(circle at 90% 0, rgba(99, 102, 241, 0.12), transparent 30%),
    linear-gradient(145deg, rgba(255, 255, 255, 0.94), rgba(248, 250, 252, 0.72));
}

.system-detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.system-detail-head span {
  display: block;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.68rem;
  font-weight: 950;
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

.system-detail-head strong {
  display: block;
  margin-top: 6px;
  color: #0f172a;
  font-size: 1.25rem;
  font-weight: 1000;
}

.system-detail-head button,
.thought-toggle {
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

.system-detail-head button {
  height: 32px;
  padding: 0 12px;
}

.system-detail-head button:hover,
.thought-toggle:hover {
  transform: translateY(-1px);
  border-color: rgba(14, 165, 233, 0.38);
  background: rgba(240, 249, 255, 0.86);
}

.system-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.system-kpi-card,
.system-detail-card {
  min-width: 0;
  border: 1px solid rgba(186, 230, 253, 0.5);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.76);
  box-shadow: 0 18px 42px rgba(15, 23, 42, 0.07), inset 0 1px 0 rgba(255, 255, 255, 0.9);
}

.system-kpi-card {
  display: grid;
  gap: 6px;
  padding: 15px;
}

.system-kpi-card small,
.system-kpi-card span,
.system-stat-list span,
.system-metric-list small {
  color: #64748b;
  font-size: 0.66rem;
  font-weight: 920;
}

.system-kpi-card strong {
  color: #0f172a;
  font-size: 1.48rem;
  font-weight: 1000;
}

.system-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.system-detail-card {
  padding: 16px;
}

.system-detail-card.wide {
  grid-column: 1 / -1;
}

.system-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  color: #0f172a;
  font-size: 0.78rem;
  font-weight: 1000;
}

.system-card-title i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #38bdf8;
  box-shadow: 0 0 16px rgba(56, 189, 248, 0.75);
}

.system-stat-list {
  display: grid;
  gap: 9px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.system-stat-list li {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding: 8px 0;
  border-bottom: 1px solid rgba(226, 232, 240, 0.7);
}

.system-stat-list li:last-child {
  border-bottom: 0;
}

.system-stat-list strong,
.system-metric-list strong {
  color: #0f172a;
  font-weight: 1000;
}

.system-metric-list {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.system-metric-list span {
  display: grid;
  gap: 6px;
  padding: 10px;
  border-radius: 14px;
  background: rgba(248, 250, 252, 0.78);
}

/* ---- Collapsed agent process, GPT-like ---- */
.thought-fold {
  margin-top: 4px;
  padding-left: 30px;
}

.thought-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 0 11px;
}

.thought-toggle em {
  color: #0284c7;
  font-size: 0.62rem;
  font-style: normal;
  font-weight: 950;
}

.thought-details {
  display: grid;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid rgba(203, 213, 225, 0.5);
  border-radius: 16px;
  background: rgba(248, 250, 252, 0.76);
}

.thought-step {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr);
  gap: 10px;
}

.thought-step i {
  width: 6px;
  height: 6px;
  margin-top: 8px;
  border-radius: 999px;
  background: #94a3b8;
}

.thought-step span {
  display: block;
  color: #64748b;
  font-size: 0.64rem;
  font-weight: 950;
}

.thought-step p {
  margin: 3px 0 0;
  color: #334155;
  font-size: 0.76rem;
  line-height: 1.62;
  font-weight: 720;
  user-select: text;
  -webkit-user-select: text;
}

/* ---- Smoother sidebar collapse. Keep animation on transform/opacity, avoid heavy reflow. ---- */
.history-sidebar {
  transform: translate3d(0, 0, 0);
  transition:
    transform 0.34s cubic-bezier(0.16, 1, 0.3, 1),
    opacity 0.26s ease,
    border-color 0.22s ease,
    background 0.22s ease,
    box-shadow 0.22s ease;
  will-change: transform, opacity;
}

.chatops-page.collapsed .history-sidebar {
  transform: translate3d(calc(-1 * var(--sidebar-width) + 58px), 0, 0);
  opacity: 1;
  padding: 18px 12px;
  pointer-events: auto;
}

.topology-workbench {
  transition:
    left 0.34s cubic-bezier(0.16, 1, 0.3, 1),
    border-radius 0.34s cubic-bezier(0.16, 1, 0.3, 1);
}

.sidebar-body,
.brand-block,
.sidebar-toggle {
  transition-duration: 0.24s;
}

@media (max-width: 980px) {
  .system-kpi-grid,
  .system-detail-grid,
  .system-metric-list {
    grid-template-columns: 1fr;
  }

  .topology-side-toggle {
    left: 10px;
    min-height: 112px;
  }
}
.topology-shell {
  overflow: hidden;
  border-radius: 24px;
}

.topology-carousel {
  background: linear-gradient(180deg, rgba(248, 250, 252, 0.86), rgba(255, 255, 255, 0.64));
}

.topology-under-toggle {
  position: absolute;
  left: 50%;
  bottom: 18px;
  z-index: 95;
  transform: translate3d(-50%, 0, 0);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 38px;
  padding: 0 16px;
  border: 1px solid rgba(125, 211, 252, 0.58);
  border-radius: 999px;
  color: #0369a1;
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 18px 42px rgba(14, 165, 233, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.92);
  font: inherit;
  font-size: 0.74rem;
  font-weight: 1000;
  letter-spacing: 0.04em;
  cursor: pointer;
  transition:
    top 0.62s cubic-bezier(0.16, 1, 0.3, 1),
    bottom 0.62s cubic-bezier(0.16, 1, 0.3, 1),
    transform 0.28s cubic-bezier(0.16, 1, 0.3, 1),
    border-color 0.2s ease,
    color 0.2s ease,
    box-shadow 0.2s ease;
}

.topology-under-toggle:hover {
  transform: translate3d(-50%, -2px, 0);
  color: #1d4ed8;
  border-color: rgba(99, 102, 241, 0.5);
  box-shadow: 0 22px 50px rgba(99, 102, 241, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.96);
}

.topology-under-toggle svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.3s ease;
}

.topology-under-toggle.active {
  top: 18px;
  bottom: auto;
}

.topology-under-toggle.active svg {
  transform: rotate(180deg);
}

.topology-side-toggle { display: none !important; }

.system-detail-panel {
  padding-top: 72px;
  animation: detailPanelIn 0.72s cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes detailPanelIn {
  from { opacity: 0; transform: translate3d(0, 16px, 0) scale(0.985); }
  to { opacity: 1; transform: translate3d(0, 0, 0) scale(1); }
}

.system-detail-head.enhanced {
  align-items: flex-start;
}

.system-signal-rail {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 0 0 14px;
}

.system-signal {
  position: relative;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  column-gap: 8px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid rgba(203, 213, 225, 0.56);
  border-radius: 16px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.88), rgba(248, 250, 252, 0.62)),
    radial-gradient(circle at 0 0, rgba(56, 189, 248, 0.10), transparent 48%);
  box-shadow: 0 12px 30px rgba(15, 23, 42, 0.045), inset 0 1px 0 rgba(255, 255, 255, 0.86);
  overflow: hidden;
  animation: signalRise 0.62s cubic-bezier(0.16, 1, 0.3, 1) both;
}

.system-signal::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,0.56) 44%, transparent 72%);
  opacity: 0;
  transform: translateX(-80%);
}

.system-signal:hover::after {
  animation: systemSheen 1.1s ease;
}

.system-signal i {
  grid-row: 1 / span 2;
  width: 8px;
  height: 8px;
  border-radius: 99px;
  background: #38bdf8;
  box-shadow: 0 0 0 6px rgba(56, 189, 248, 0.10), 0 0 18px rgba(56, 189, 248, 0.38);
  animation: signalPulse 2.2s ease-in-out infinite;
}

.system-signal.ok i { background: #22c55e; box-shadow: 0 0 0 6px rgba(34,197,94,0.10), 0 0 18px rgba(34,197,94,0.32); }
.system-signal.warn i { background: #f97316; box-shadow: 0 0 0 6px rgba(249,115,22,0.10), 0 0 18px rgba(249,115,22,0.32); }
.system-signal.active i { background: #8b5cf6; box-shadow: 0 0 0 6px rgba(139,92,246,0.10), 0 0 18px rgba(139,92,246,0.32); }

.system-signal small {
  min-width: 0;
  color: #64748b;
  font-size: 0.58rem;
  font-weight: 950;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.system-signal strong {
  min-width: 0;
  color: #0f172a;
  font-size: 0.82rem;
  font-weight: 1000;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-tabs {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px;
  border: 1px solid rgba(203, 213, 225, 0.58);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.68);
}

.detail-tabs button {
  height: 28px;
  padding: 0 11px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: #64748b;
  font: inherit;
  font-size: 0.68rem;
  font-weight: 950;
  cursor: pointer;
  transition: background 0.18s ease, color 0.18s ease, transform 0.18s ease;
}

.detail-tabs button.active,
.detail-tabs button:hover {
  color: #0369a1;
  background: rgba(224, 242, 254, 0.82);
  transform: translateY(-1px);
}

.system-kpi-grid.interactive .system-kpi-card {
  position: relative;
  overflow: hidden;
  text-align: left;
  cursor: pointer;
  transition: transform 0.22s cubic-bezier(0.16, 1, 0.3, 1), border-color 0.18s ease, box-shadow 0.18s ease;
}

.system-kpi-grid.interactive .system-kpi-card::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(112deg, transparent 0%, rgba(255,255,255,0.62) 42%, transparent 72%);
  opacity: 0;
  transform: translateX(-90%);
  pointer-events: none;
}

.system-kpi-grid.interactive .system-kpi-card:hover::after,
.system-kpi-grid.interactive .system-kpi-card.active::after {
  animation: systemSheen 1.18s ease;
}

.system-kpi-grid.interactive .system-kpi-card:hover,
.system-kpi-grid.interactive .system-kpi-card.active {
  transform: translateY(-3px);
  border-color: rgba(56, 189, 248, 0.68);
  box-shadow: 0 24px 58px rgba(14, 165, 233, 0.13), inset 0 1px 0 rgba(255, 255, 255, 0.92);
}

.system-insight-layout {
  display: grid;
  grid-template-columns: 1.06fr 1.35fr;
  gap: 14px;
  margin: 14px 0;
}

.system-visual-card {
  min-width: 0;
  border: 1px solid rgba(186, 230, 253, 0.52);
  border-radius: 20px;
  background:
    radial-gradient(circle at 12% 0, rgba(14, 165, 233, 0.12), transparent 36%),
    rgba(255, 255, 255, 0.78);
  box-shadow: 0 20px 46px rgba(15, 23, 42, 0.07), inset 0 1px 0 rgba(255, 255, 255, 0.9);
  padding: 16px;
  overflow: hidden;
  animation: detailCardRise 0.7s cubic-bezier(0.16, 1, 0.3, 1) both;
}

.trend-card,
.metric-card { animation-delay: 0.06s; }
.bar-card { animation-delay: 0.1s; }

@keyframes detailCardRise {
  from { opacity: 0; transform: translate3d(0, 18px, 0); }
  to { opacity: 1; transform: translate3d(0, 0, 0); }
}

.health-card {
  display: grid;
  align-content: start;
}

.health-orbit {
  --score: 80;
  position: relative;
  width: min(220px, 100%);
  aspect-ratio: 1;
  margin: 4px auto 8px;
  display: grid;
  place-items: center;
}

.health-orbit svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
}

.ring-base,
.ring-value {
  fill: none;
  stroke-width: 10;
}

.ring-base { stroke: rgba(224, 242, 254, 0.95); }

.ring-value {
  stroke: url(#detailLine);
  stroke-linecap: round;
  stroke-dasharray: 301.6;
  stroke-dashoffset: calc(301.6 - (301.6 * var(--score) / 100));
  filter: drop-shadow(0 0 10px rgba(14, 165, 233, 0.34));
  transition: stroke-dashoffset 0.75s cubic-bezier(0.16, 1, 0.3, 1);
}

.health-orbit div {
  position: relative;
  display: grid;
  justify-items: center;
  gap: 4px;
}

.health-orbit strong {
  color: #0f172a;
  font-size: 2.2rem;
  font-weight: 1000;
}

.health-orbit span,
.health-card p {
  color: #64748b;
  font-size: 0.72rem;
  font-weight: 850;
  text-align: center;
}

.mini-line-chart {
  min-height: 172px;
  display: grid;
  align-content: end;
  gap: 8px;
}

.mini-line-chart svg {
  width: 100%;
  height: 142px;
  overflow: visible;
}

.chart-grid {
  fill: none;
  stroke: rgba(203, 213, 225, 0.68);
  stroke-width: 1;
  stroke-dasharray: 5 7;
}

.line-shadow,
.line-main {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.line-shadow {
  stroke: rgba(14, 165, 233, 0.16);
  stroke-width: 12;
}

.line-main {
  stroke: url(#detailLine);
  stroke-width: 4;
  stroke-dasharray: 420;
  stroke-dashoffset: 420;
  animation: drawDetailLine 1.15s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}

@keyframes drawDetailLine { to { stroke-dashoffset: 0; } }

.mini-line-chart circle {
  fill: #ffffff;
  stroke: #38bdf8;
  stroke-width: 2.5;
  filter: drop-shadow(0 0 8px rgba(56, 189, 248, 0.45));
}

.trend-labels {
  display: flex;
  justify-content: space-between;
  color: #64748b;
  font-size: 0.62rem;
  font-weight: 900;
}

.detail-bar-list {
  display: grid;
  gap: 10px;
}

.detail-bar-list button {
  display: grid;
  grid-template-columns: 72px 1fr 34px;
  align-items: center;
  gap: 10px;
  border: 0;
  background: transparent;
  font: inherit;
  color: #334155;
  cursor: pointer;
}

.detail-bar-list span,
.detail-bar-list strong {
  font-size: 0.68rem;
  font-weight: 950;
}

.detail-bar-list i {
  height: 9px;
  border-radius: 999px;
  background: rgba(224, 242, 254, 0.9);
  overflow: hidden;
}

.detail-bar-list em {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #22d3ee, #6366f1);
  transform-origin: left center;
  animation: barGrow 0.72s cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes barGrow {
  from { transform: scaleX(0.08); }
  to { transform: scaleX(1); }
}

.telemetry-bars {
  min-height: 168px;
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(34px, 1fr);
  align-items: end;
  gap: 10px;
}

.telemetry-bars button {
  height: 100%;
  min-width: 0;
  display: grid;
  align-items: end;
  gap: 8px;
  border: 0;
  background: transparent;
  color: #64748b;
  font: inherit;
  font-size: 0.58rem;
  font-weight: 900;
  cursor: pointer;
}

.telemetry-bars i {
  display: block;
  height: var(--height);
  min-height: 24px;
  border-radius: 999px 999px 10px 10px;
  background: linear-gradient(180deg, rgba(34, 211, 238, 0.9), rgba(99, 102, 241, 0.7));
  box-shadow: 0 12px 22px rgba(99, 102, 241, 0.14);
  animation: telemetryRise 0.82s cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes telemetryRise {
  from { transform: scaleY(0.14); opacity: 0.4; }
  to { transform: scaleY(1); opacity: 1; }
}

.telemetry-bars span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  writing-mode: vertical-rl;
  justify-self: center;
  max-height: 58px;
}

.system-detail-grid.compact {
  margin-top: 14px;
}

.chatops-page.quick-returning .topology-shell {
  transform: translate3d(0, 6px, 0) scale(0.986);
  opacity: 0.72;
  transition: transform 0.22s ease, opacity 0.22s ease;
}

.chatops-page.quick-returning .conversation-view {
  animation: quickChatReturn 0.42s cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes quickChatReturn {
  from { opacity: 0; transform: translate3d(0, 16px, 0); }
  to { opacity: 1; transform: translate3d(0, 0, 0); }
}

@media (max-width: 1180px) {
  .system-insight-layout { grid-template-columns: 1fr; }
  .detail-tabs { flex-wrap: wrap; justify-content: flex-end; }
}


/* ---- Canvas-grade system detail charts ---- */
.canvas-card {
  overflow: hidden;
  position: relative;
  isolation: isolate;
}
.canvas-card::after {
  content: '';
  position: absolute;
  inset: auto -18% -42% 12%;
  height: 110px;
  background: radial-gradient(circle, rgba(34, 211, 238, 0.18), transparent 68%);
  pointer-events: none;
  transform: translate3d(0, 0, 0);
}
.canvas-stage {
  position: relative;
  width: 100%;
  min-height: 180px;
  border-radius: 24px;
  background:
    linear-gradient(135deg, rgba(255,255,255,0.94), rgba(241,248,255,0.78)),
    radial-gradient(circle at 18% 18%, rgba(34,211,238,0.14), transparent 34%),
    radial-gradient(circle at 84% 16%, rgba(139,92,246,0.12), transparent 30%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.9), 0 16px 44px rgba(15, 23, 42, 0.06);
  transform: translateZ(0);
}
.canvas-stage canvas {
  display: block;
  width: 100%;
  height: 100%;
  min-height: inherit;
  position: relative;
  z-index: 1;
}
.canvas-stage::after {
  content: "";
  position: absolute;
  inset: -20% auto -20% 0;
  width: 30%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.44), transparent);
  opacity: 0.32;
  transform: translateX(-130%) skewX(-16deg);
  animation: chartSurfaceSweep 5.2s ease-in-out infinite;
  pointer-events: none;
}
.gauge-stage {
  min-height: 220px;
}
.trend-stage,
.mix-stage,
.telemetry-stage {
  min-height: 210px;
}
.canvas-center {
  position: absolute;
  inset: 50% auto auto 50%;
  display: grid;
  place-items: center;
  width: 92px;
  height: 92px;
  border-radius: 999px;
  background: rgba(255,255,255,0.82);
  box-shadow: 0 16px 42px rgba(30, 64, 175, 0.12), inset 0 1px 0 rgba(255,255,255,0.96);
  transform: translate(-50%, -50%);
  pointer-events: none;
}
.canvas-center strong {
  margin-top: 12px;
  color: #0f172a;
  font-size: 24px;
  letter-spacing: -0.04em;
}
.canvas-center span {
  margin-top: -20px;
  color: rgba(71, 85, 105, 0.7);
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.chart-chip-row,
.chart-legend {
  position: relative;
  z-index: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.chart-chip-row span,
.chart-legend button {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 999px;
  background: rgba(255,255,255,0.76);
  color: rgba(51,65,85,0.78);
  font-size: 11px;
  font-weight: 800;
  padding: 7px 10px;
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.04);
}
.chart-chip-row.small span {
  padding: 6px 8px;
  font-size: 10px;
}
.chart-legend.compact button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  cursor: pointer;
  transition: transform 0.22s ease, border-color 0.22s ease, background 0.22s ease;
}
.chart-legend.compact button:hover {
  transform: translateY(-2px);
  border-color: rgba(34, 211, 238, 0.34);
  background: rgba(255,255,255,0.96);
}
.chart-legend.compact i {
  width: 7px;
  height: 7px;
  border-radius: 99px;
  background: linear-gradient(135deg, #22d3ee, #8b5cf6);
}
.chart-legend.compact strong {
  color: #0f172a;
}
.system-visual-card.canvas-card:hover .canvas-stage {
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.95), 0 20px 54px rgba(14, 165, 233, 0.12);
}

/* ---- Compact two-chart system detail layout ---- */
.system-data-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 14px 0 12px;
}
.system-data-card {
  min-width: 0;
  padding: 14px;
  border: 1px solid rgba(186, 230, 253, 0.46);
  border-radius: 18px;
  background:
    radial-gradient(circle at 8% 0, rgba(14, 165, 233, 0.08), transparent 42%),
    rgba(255, 255, 255, 0.78);
  box-shadow: 0 16px 38px rgba(15, 23, 42, 0.055), inset 0 1px 0 rgba(255,255,255,0.86);
}
.system-data-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 0;
  border-top: 1px solid rgba(226, 232, 240, 0.68);
}
.system-data-row:first-of-type { border-top: 0; }
.system-data-row span {
  color: #64748b;
  font-size: 0.68rem;
  font-weight: 900;
}
.system-data-row strong {
  color: #0f172a;
  font-size: 0.86rem;
  font-weight: 1000;
  max-width: 58%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.system-insight-layout.compact-charts {
  grid-template-columns: minmax(0, 1.36fr) minmax(280px, 0.84fr);
  gap: 14px;
  margin: 12px 0 0;
}
.system-insight-layout.compact-charts .system-visual-card {
  padding: 15px;
}
.chart-title-row {
  align-items: baseline;
}
.chart-title-row em {
  margin-left: auto;
  color: rgba(100, 116, 139, 0.64);
  font-style: normal;
  font-size: 0.62rem;
  font-weight: 950;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.chart-title-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin: 6px 0 10px;
  color: rgba(71, 85, 105, 0.74);
  font-size: 0.68rem;
  font-weight: 900;
}

.chart-title-meta span {
  position: relative;
  padding-left: 10px;
  white-space: nowrap;
}

.chart-title-meta span::before {
  content: "";
  position: absolute;
  left: 0;
  top: 50%;
  width: 4px;
  height: 4px;
  border-radius: 99px;
  background: #38bdf8;
  transform: translateY(-50%);
}
.system-chart-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin: 10px 0 12px;
}
.system-chart-summary span {
  min-width: 0;
  padding: 9px 10px;
  border-radius: 14px;
  background: rgba(248, 250, 252, 0.8);
  border: 1px solid rgba(226, 232, 240, 0.72);
}
.system-chart-summary small,
.system-chart-summary strong {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.system-chart-summary small {
  color: #64748b;
  font-size: 0.58rem;
  font-weight: 950;
}
.system-chart-summary strong {
  margin-top: 5px;
  color: #0f172a;
  font-size: 0.85rem;
  font-weight: 1000;
}
.feature-chart.trend-stage,
.feature-chart.telemetry-stage {
  min-height: 214px;
}
.system-insight-layout.compact-charts .metric-card .canvas-stage {
  min-height: 214px;
}
.system-detail-panel {
  padding-top: 48px;
}
@media (max-width: 1180px) {
  .system-data-strip,
  .system-insight-layout.compact-charts {
    grid-template-columns: 1fr;
  }
}


/* --- refined compact system insight --- */
.system-detail-panel {
  scroll-behavior: smooth;
}

.system-data-strip {
  align-items: stretch;
}

.system-data-card,
.system-kpi-card,
.system-visual-card,
.chart-chip-row button,
.system-detail-tabs button {
  transition:
    transform 0.28s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.28s cubic-bezier(0.16, 1, 0.3, 1),
    border-color 0.28s ease,
    background 0.28s ease;
}

.system-data-card:hover,
.system-kpi-card:hover,
.system-visual-card:hover {
  transform: translate3d(0, -4px, 0);
  border-color: rgba(56, 189, 248, 0.58);
  box-shadow:
    0 24px 56px rgba(15, 23, 42, 0.10),
    0 0 0 1px rgba(255, 255, 255, 0.82) inset,
    0 0 32px rgba(34, 211, 238, 0.10);
}

.system-kpi-grid.interactive .system-kpi-card {
  cursor: pointer;
}

.system-kpi-grid.interactive .system-kpi-card.active {
  border-color: rgba(56, 189, 248, 0.72);
  background:
    radial-gradient(circle at 16% 0, rgba(34, 211, 238, 0.16), transparent 42%),
    linear-gradient(145deg, rgba(255,255,255,0.96), rgba(239,248,255,0.78));
}

.system-insight-layout.compact-charts {
  grid-template-columns: minmax(0, 1.14fr) minmax(360px, 0.86fr);
  gap: 16px;
  align-items: stretch;
  margin-top: 12px;
}

.system-visual-card.canvas-card {
  position: relative;
  isolation: isolate;
  min-height: 0;
}

.system-visual-card.canvas-card::before {
  content: "";
  position: absolute;
  inset: -30% auto auto -12%;
  width: 220px;
  height: 220px;
  border-radius: 999px;
  background: radial-gradient(circle, rgba(34, 211, 238, 0.13), transparent 68%);
  pointer-events: none;
  z-index: -1;
}

.system-visual-card.canvas-card::after {
  content: "";
  position: absolute;
  right: -18%;
  bottom: -34%;
  width: 240px;
  height: 240px;
  border-radius: 999px;
  background: radial-gradient(circle, rgba(139, 92, 246, 0.10), transparent 66%);
  pointer-events: none;
  z-index: -1;
}

.system-chart-summary {
  margin-bottom: 14px;
}

.system-chart-summary span {
  cursor: default;
  transition: transform 0.24s ease, background 0.24s ease, border-color 0.24s ease;
}

.system-chart-summary span:hover {
  transform: translateY(-2px);
  border-color: rgba(34, 211, 238, 0.35);
  background: rgba(255, 255, 255, 0.92);
}

.canvas-stage {
  overflow: hidden;
  border: 1px solid rgba(226, 232, 240, 0.72);
  transition:
    transform 0.32s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.32s cubic-bezier(0.16, 1, 0.3, 1),
    border-color 0.32s ease;
}

.canvas-stage:hover {
  transform: translate3d(0, -3px, 0) scale(1.005);
  border-color: rgba(56, 189, 248, 0.42);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,0.92),
    0 22px 56px rgba(15, 23, 42, 0.08),
    0 0 40px rgba(34, 211, 238, 0.12);
}

.trend-stage {
  min-height: 270px;
}

.telemetry-stage {
  min-height: 270px;
}

.chart-chip-row {
  gap: 10px;
}

.chart-chip-row button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid rgba(148, 163, 184, 0.20);
  border-radius: 999px;
  background: rgba(255,255,255,0.78);
  color: rgba(51,65,85,0.84);
  font-size: 11px;
  font-weight: 850;
  padding: 7px 11px;
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.04);
  cursor: pointer;
}

.chart-chip-row button strong {
  color: #0f172a;
  font-size: 10px;
}

.chart-chip-row button:hover {
  transform: translateY(-2px);
  border-color: rgba(34, 211, 238, 0.52);
  background: rgba(240, 249, 255, 0.92);
  box-shadow: 0 16px 32px rgba(14, 165, 233, 0.11);
}

.chart-chip-row.small button {
  padding: 6px 9px;
  font-size: 10px;
}

.system-insight-layout.compact-charts .canvas-stage {
  border: 0;
  border-radius: 16px;
  background: transparent;
  box-shadow: none;
}

.system-insight-layout.compact-charts .canvas-stage::after {
  display: none;
}

.system-insight-layout.compact-charts .canvas-stage:hover {
  transform: none;
  border-color: transparent;
  box-shadow: none;
}

.system-insight-layout.compact-charts .system-visual-card {
  background:
    radial-gradient(circle at 10% 4%, rgba(56, 189, 248, 0.10), transparent 34%),
    linear-gradient(145deg, rgba(255,255,255,0.92), rgba(248,250,252,0.70));
}

.system-insight-layout.compact-charts .metric-card .canvas-stage,
.feature-chart.telemetry-stage {
  min-height: 248px;
}

@keyframes signalRise {
  from { opacity: 0; transform: translate3d(0, 8px, 0); }
  to { opacity: 1; transform: translate3d(0, 0, 0); }
}

@keyframes signalPulse {
  0%, 100% { transform: scale(0.92); opacity: 0.82; }
  50% { transform: scale(1.12); opacity: 1; }
}

@keyframes systemSheen {
  0% { opacity: 0; transform: translateX(-90%); }
  30% { opacity: 0.72; }
  100% { opacity: 0; transform: translateX(95%); }
}

@keyframes chartSurfaceSweep {
  0%, 28% { transform: translateX(-130%) skewX(-16deg); opacity: 0; }
  46% { opacity: 0.32; }
  78%, 100% { transform: translateX(430%) skewX(-16deg); opacity: 0; }
}

@media (max-width: 1180px) {
  .system-signal-rail {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .system-insight-layout.compact-charts {
    grid-template-columns: 1fr;
  }
}

</style>


