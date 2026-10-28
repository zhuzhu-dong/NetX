/**
 * pipelineStore.js — 多智能体流水线阶段与执行日志
 *
 * 从 intentStore 解耦：订阅事件总线而非被 intentStore 直接调用。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { useEventBus } from '../composables/useEventBus'
import { STAGE_MAP, stageNameToKey } from '../constants'

function nowTime() {
  return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/** Build initial pipeline stages from STAGE_MAP. */
function buildStages() {
  return Object.values(STAGE_MAP).map((def) => ({
    key: def.key,
    name: def.name,
    agent: def.agent,
    state: 'waiting',
  }))
}

export const usePipelineStore = defineStore('pipeline', () => {
  /* ---- state ---- */
  const pipelineStages = ref(buildStages())
  const selectedStage = ref('S1')
  const agentLogs = ref([])

  /* ---- actions ---- */
  function resetPipeline() {
    pipelineStages.value = buildStages().map((stage, index) => ({
      ...stage,
      state: index === 0 ? 'running' : 'waiting',
    }))
    selectedStage.value = 'S1'
  }

  function selectStage(key) {
    selectedStage.value = key
    const stage = pipelineStages.value.find((item) => item.key === key)
    if (stage) addLog(`已查看 ${stage.name} 阶段。`)
  }

  function addLog(text) {
    agentLogs.value = [
      { id: Date.now(), time: nowTime(), text },
      ...agentLogs.value,
    ].slice(0, 30)
  }

  function _updatePipelineFromMilestones(milestones) {
    for (const m of milestones) {
      const key = stageNameToKey(m.stage)
      if (!key) continue
      const stage = pipelineStages.value.find((s) => s.key === key)
      if (stage) {
        if (m.status === 'success') stage.state = 'complete'
        else if (m.status === 'failed') stage.state = 'failed'
        else if (m.status === 'running') stage.state = 'running'
        // Populate detail from milestone data
        if (m.summary && !stage.output) stage.output = m.summary
        if (m.input_summary && !stage.input) stage.input = m.input_summary
        if (m.evidence && !stage.evidence) stage.evidence = Array.isArray(m.evidence) ? m.evidence.join(', ') : m.evidence
        if (m.duration_ms && !stage.duration_ms) stage.duration_ms = m.duration_ms
      }
    }
  }

  /** Restore pipeline stages from an intent's overall status when milestones are missing. */
  function setPipelineFromStatus(status) {
    const stages = pipelineStages.value
    if (!stages.length) return

    const terminal = String(status || '').toLowerCase()
    if (terminal === 'success') {
      for (const s of stages) s.state = 'complete'
    } else if (terminal === 'failed') {
      // Mark all complete then the last as failed (best guess without milestones)
      for (const s of stages) s.state = 'complete'
      stages[stages.length - 1].state = 'failed'
    } else if (terminal === 'cancelled') {
      for (const s of stages) s.state = 'waiting'
    } else {
      // Running / pending — start fresh
      for (let i = 0; i < stages.length; i++) {
        stages[i].state = i === 0 ? 'running' : 'waiting'
      }
    }
  }

  /** Handle incoming SSE stage_change events. */
  function _onStageChange({ intentId, data }) {
    const key = stageNameToKey(data.stage)
    if (key) {
      const stage = pipelineStages.value.find((s) => s.key === key)
      if (stage) {
        if (data.status === 'running') stage.state = 'running'
        else if (data.status === 'success') stage.state = 'complete'
        else if (data.status === 'failed') stage.state = 'failed'
        // Populate detail fields from SSE event
        if (data.input || data.input_summary) stage.input = data.input || data.input_summary
        if (data.output || data.output_summary) stage.output = data.output || data.output_summary
        if (data.evidence) stage.evidence = Array.isArray(data.evidence) ? data.evidence.join(', ') : data.evidence
        if (data.duration_ms) stage.duration_ms = data.duration_ms
        if (data.summary && !stage.output) stage.output = data.summary
      }
      selectedStage.value = key
    }
    if (data.status === 'success' && data.summary) {
      addLog(`${intentId.slice(-8)} ${data.agent_id || key}: ${data.summary}`)
    }
  }

  /** Enrich pipeline stages from A2A trace messages (for historical intents). */
  function enrichStagesFromTrace(intentId, a2aMessages) {
    if (!Array.isArray(a2aMessages)) return
    for (const msg of a2aMessages) {
      const key = stageNameToKey(msg.stage)
      if (!key) continue
      const stage = pipelineStages.value.find((s) => s.key === key)
      if (!stage) continue
      if (!stage.input && msg.input_summary) stage.input = msg.input_summary
      if (!stage.output && msg.output_summary) stage.output = msg.output_summary
      if (!stage.evidence && msg.evidence_refs) stage.evidence = msg.evidence_refs.join(', ')
      if (!stage.duration_ms && msg.duration_ms) stage.duration_ms = msg.duration_ms
    }
  }

  // ── Subscribe to event bus on store creation ──
  // Only log:add remains on the event bus (genuine 1:N cross-cutting).
  // pipeline:reset / pipeline:milestones-update / pipeline:stage-change
  // are now called directly by intentStore → simpler, greppable, type-safe.
  const _bus = useEventBus()
  _bus.on('log:add', addLog)

  return {
    pipelineStages, selectedStage, agentLogs,
    resetPipeline, selectStage, addLog, _updatePipelineFromMilestones, setPipelineFromStatus,
    enrichStagesFromTrace,
  }
})
