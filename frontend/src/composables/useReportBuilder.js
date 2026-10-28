/**
 * useReportBuilder.js — 执行报告 & 对话消息构建
 *
 * 从 NetworkOverview.vue 提取，消除与 AgentRun.vue 中的重复。
 */

import { formatMbps, formatMs, normalizePercent, asArray } from './useFormatters'
import { getRuntimeTimeline, uniqueAgents } from './useDataExtractors'

function cleanText(value) {
  if (value === undefined || value === null) return ''
  const text = typeof value === 'string'
    ? value
    : JSON.stringify(value)
  return String(text || '').replace(/\s+/g, ' ').trim()
}

function pickReason(...values) {
  for (const value of values) {
    const text = cleanText(value)
    if (!text) continue
    if (['none', 'null', 'undefined', '{}', '[]'].includes(text.toLowerCase())) continue
    return text
  }
  return ''
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

function extractFailureReason(active) {
  const result = active?.result || {}
  const decision = result.decision_report || {}
  const verification = decision.verification_result || {}
  const diagnosis = decision.problem_diagnosis || {}
  const fallback = decision.fallback_plan || {}
  const error = result.error

  return pickReason(
    decision.final_conclusion,
    decision.summary,
    result.failure_reason,
    result.error_message,
    result.message,
    typeof error === 'object' ? error.message : error,
    typeof error === 'object' ? error.detail : '',
    result.detail,
    result.details,
    verification.reason,
    verification.error,
    diagnosis.root_cause,
    fallback.if_verification_failed,
  )
}

function buildFailureContent(active) {
  const reason = extractFailureReason(active)
  if (reason) return `执行失败：${reason}`
  return '执行失败：系统未返回明确原因，可能是拓扑不可用、参数不足、策略冲突或策略下发失败。'
}

// ── 执行报告构建 ──────────────────────────────────────────────────────────────

/**
 * 构建分层执行摘要报告。
 * Tier 1: status + conclusion + metrics（始终可见）
 * Tier 2: KPI verification table + path change + key actions（始终可见）
 * Tier 3: diagnosis / candidates / votes / evidence（可折叠详情）
 */
export function buildExecutionSummary(active) {
  const isFinished = active?.statusClass === 'success' || active?.statusClass === 'failed'
  if (!active || !isFinished || !active.result) return null

  const result = active.result || {}
  const decision = result.decision_report || {}
  const observation = decision.network_observation || {}
  const diagnosis = decision.problem_diagnosis || {}
  const verification = decision.verification_result || {}
  const finalAction = decision.final_action || {}
  const collaboration = decision.collaboration || {}
  const networkEvidence = decision.network_evidence || {}
  const timeline = result.a2a_timeline || result.a2a_runtime_evidence?.messages || decision.a2a_runtime_evidence?.messages || []
  const selectedPlan = (decision.candidate_solutions || []).find((item) => item.selected) || decision.candidate_solutions?.[0]
  const decisionPoints = decision.decision_points || []

  const afterKpi = verification.after_kpi || {}
  const afterLoss = normalizePercent(afterKpi.packet_loss_rate)
  const afterLatency = formatMs(afterKpi.latency_ms)
  const throughput = formatMbps(afterKpi.throughput_mbps)

  return {
    // ── Tier 1 ──
    status: verification.passed || verification.intent_achieved || active.statusClass === 'success'
      ? '闭环验证通过' : '需要人工复核',
    conclusion: decision.final_conclusion || decision.summary
      || '本次意图已完成多智能体编排，执行结果已同步到拓扑态势。',
    metrics: [
      { label: '协作消息', value: `${collaboration.message_count ?? timeline.length ?? 0}` },
      { label: '参与智能体', value: `${collaboration.agents?.length ?? uniqueAgents(timeline).length ?? 0}` },
      { label: '验证时延', value: afterLatency || '-' },
      { label: '验证丢包', value: afterLoss || '-' },
      { label: '业务吞吐', value: throughput || '-' },
      { label: '路径切换', value: verification.switched_to_backup ? '已切换'
        : observation.post_heal_path_text ? '已调整' : '未切换' },
    ],

    // ── KPI verification — 透传给 KpiVerifyCard ──
    kpiVerification: Object.keys(verification).length ? verification : null,

    // ── Tier 2 ──
    pathChange: {
      before: observation.pre_heal_path_text || observation.current_path_text || '',
      after: observation.post_heal_path_text || '',
      state: observation.path_state || '',
    },
    keyActions: finalAction.selected_actions || [],

    // ── Tier 3（可折叠）──
    diagnosis: Object.keys(diagnosis).length ? diagnosis : null,
    candidateSolutions: (decision.candidate_solutions || []).slice(0, 3).map((plan) => ({
      id: plan.id,
      title: plan.title || plan.id || '候选方案',
      selected: Boolean(plan.selected || plan.recommended),
      risk: plan.risk_score ?? '-',
      reason: plan.reason || '无补充说明。',
    })),
    agentVotes: (decision.agent_votes || []).map((vote) => ({
      agent: vote.agent_id || vote.agent || 'agent',
      vote: vote.vote || vote.status || 'approve',
    })),
    evidence: [
      selectedPlan?.title ? `选定方案：${selectedPlan.title}` : '',
      networkEvidence.devices !== undefined ? `设备 ${networkEvidence.devices} 台` : '',
      networkEvidence.links !== undefined ? `链路 ${networkEvidence.links} 条` : '',
      ...((decision.evidence_sources || []).slice(0, 4)),
      ...((diagnosis.evidence || []).slice(0, 3)),
      ...((decisionPoints || []).slice(-2).map((item) => `${item.agent_id || item.stage}：${item.status}`)),
    ].filter(Boolean),
  }
}

/**
 * @deprecated 旧版执行报告，由 buildExecutionSummary 取代。
 * 保留以兼容 AgentRun.vue 侧边栏内嵌展示。
 */
export function buildExecutionReport(active) {
  const isFinished = active?.statusClass === 'success' || active?.statusClass === 'failed'
  if (!active || !isFinished || !active.result) return null

  const result = active.result || {}
  const decision = result.decision_report || {}
  const observation = decision.network_observation || {}
  const diagnosis = decision.problem_diagnosis || {}
  const verification = decision.verification_result || {}
  const intent = decision.intent_understanding || {}
  const finalAction = decision.final_action || {}
  const fallback = decision.fallback_plan || {}
  const collaboration = decision.collaboration || {}
  const networkEvidence = decision.network_evidence || {}
  const timeline = result.a2a_timeline || result.a2a_runtime_evidence?.messages || decision.a2a_runtime_evidence?.messages || []
  const afterKpi = verification.after_kpi || {}
  const beforeKpi = observation.kpi || decision.kpi || {}
  const selectedPlan = (decision.candidate_solutions || []).find((item) => item.selected) || decision.candidate_solutions?.[0]
  const decisionPoints = decision.decision_points || []

  const beforeLoss = normalizePercent(beforeKpi.packet_loss_rate)
  const afterLoss = normalizePercent(afterKpi.packet_loss_rate)
  const beforeLatency = formatMs(beforeKpi.latency_ms)
  const afterLatency = formatMs(afterKpi.latency_ms)
  const throughput = formatMbps(afterKpi.throughput_mbps ?? beforeKpi.throughput_mbps)

  return {
    title: '自治执行完成报告',
    subtitle: decision.report_id || decision.intent_id || active.id,
    status: verification.passed || verification.intent_achieved || active.statusClass === 'success' ? '闭环验证通过' : '需要人工复核',
    summary: decision.final_conclusion || decision.summary || '本次意图已完成多智能体编排，执行结果已同步到拓扑态势。',
    metrics: [
      { label: '协作消息', value: `${collaboration.message_count ?? timeline.length ?? 0}` },
      { label: '参与智能体', value: `${collaboration.agents?.length ?? uniqueAgents(timeline).length ?? 0}` },
      { label: '验证时延', value: afterLatency || beforeLatency || '-' },
      { label: '验证丢包', value: afterLoss || beforeLoss || '-' },
      { label: '业务吞吐', value: throughput || '-' },
      { label: '路径切换', value: verification.switched_to_backup ? '已切换' : observation.post_heal_path_text ? '已调整' : '未切换' },
    ],
    sections: [
      {
        title: '意图理解',
        body: [
          intent.business_object ? `业务对象：${intent.business_object}` : '',
          intent.action_type ? `动作类型：${intent.action_type}` : '',
          intent.source_ip || intent.destination_ip ? `流量范围：${intent.source_ip || '任意源'} → ${intent.destination_ip || '任意目的'}` : '',
          intent.protocol || intent.destination_port ? `协议端口：${intent.protocol || '-'} / ${intent.destination_port || '-'}` : '',
        ].filter(Boolean).join('；') || decision.user_intent || active.text || '已解析用户提交的网络意图。',
      },
      {
        title: '问题诊断',
        body: [
          diagnosis.root_cause,
          diagnosis.bottleneck_link ? `瓶颈链路：${diagnosis.bottleneck_link}` : '',
          diagnosis.bottleneck_device ? `瓶颈设备：${diagnosis.bottleneck_device}` : '',
        ].filter(Boolean).join('；') || '未发现阻断性冲突，策略可继续执行。',
      },
      {
        title: '路径变化',
        body: [
          observation.pre_heal_path_text || observation.current_path_text ? `执行前：${observation.pre_heal_path_text || observation.current_path_text}` : '',
          observation.post_heal_path_text ? `执行后：${observation.post_heal_path_text}` : '',
          observation.path_state ? `状态：${observation.path_state}` : '',
        ].filter(Boolean).join('；') || '拓扑与路径状态已完成同步。',
      },
      {
        title: '验证结果',
        body: [
          verification.passed || verification.intent_achieved ? 'SLA 验证通过' : '验证结果需要复核',
          beforeLatency && afterLatency ? `时延 ${beforeLatency} → ${afterLatency}` : '',
          beforeLoss && afterLoss ? `丢包 ${beforeLoss} → ${afterLoss}` : '',
          fallback.if_verification_failed ? `回滚预案：${fallback.if_verification_failed}` : '',
        ].filter(Boolean).join('；') || '验证阶段已结束。',
      },
    ],
    actions: finalAction.selected_actions || [],
    planOptions: (decision.candidate_solutions || []).slice(0, 3).map((plan) => ({
      id: plan.id,
      title: plan.title || plan.id || '候选方案',
      selected: Boolean(plan.selected || plan.recommended),
      risk: plan.risk_score ?? '-',
      reason: plan.reason || '无补充说明。',
    })),
    agentVotes: (decision.agent_votes || []).map((vote) => ({
      agent: vote.agent_id || vote.agent || 'agent',
      vote: vote.vote || vote.status || 'approve',
    })),
    evidence: [
      selectedPlan?.title ? `选定方案：${selectedPlan.title}` : '',
      networkEvidence.devices !== undefined ? `设备 ${networkEvidence.devices} 台` : '',
      networkEvidence.links !== undefined ? `链路 ${networkEvidence.links} 条` : '',
      ...((decision.evidence_sources || []).slice(0, 4)),
      ...((diagnosis.evidence || []).slice(0, 3)),
      ...((decisionPoints || []).slice(-2).map((item) => `${item.agent_id || item.stage}：${item.status}`)),
    ].filter(Boolean),
  }
}

// ── 对话消息构建 ──────────────────────────────────────────────────────────────

export function buildGeneratedMessages(active, includeUser, activeClarificationQuestion) {
  const messages = []
  const seenContents = new Set()
  const id = active?.id || active?.sessionId || active?.activeRunId || 'conversation'
  const runtimeId = active?.activeRunId || active?.intent_id || active?.intentId || id

  // Chat-only records carry their own dialogMessages — don't inject
  // pipeline defaults like "已收到意图..."
  if (!active || active?._chatOnly || active?.result?.chat_only) {
    return messages
  }

  if (includeUser) {
    messages.push({
      id: `${id}-user`,
      role: 'user',
      tag: 'operator',
      source: 'generated',
      intentId: runtimeId,
      content: active.text || active.content || '新的网络意图',
    })
  }

  if (activeClarificationQuestion) {
    messages.push({
      id: `${id}-clarification`,
      role: 'assistant',
      tag: '需要补充信息',
      content: activeClarificationQuestion,
      source: 'generated',
      intentId: runtimeId,
    })
    return messages
  }

  const milestones = (active.milestones || []).filter((item) => item.summary)
  const timeline = getRuntimeTimeline(active).filter((item) => item.output_summary)

  const isFinished = active.statusClass === 'success' || active.statusClass === 'failed'

  if (!milestones.length && !timeline.length && !isFinished) {
    messages.push({
      id: `${id}-thinking`,
      role: 'assistant',
      tag: '深度思考',
      action: 'thinking',
      source: 'generated',
      intentId: runtimeId,
      content: '执行中...',
    })
    return messages
  }

  if (!isFinished) {
    messages.push({
      id: `${id}-thinking`,
      role: 'assistant',
      tag: 'NetPulse',
      content: '执行中...',
      action: 'thinking',
      source: 'generated',
      intentId: runtimeId,
    })
  }

  milestones.forEach((item, index) => {
    const content = item.summary
    if (!content || seenContents.has(content)) return
    seenContents.add(content)

    messages.push({
      id: `${id}-milestone-${item.stage || 'stage'}-${item.status || 'state'}-${index}`,
      role: 'assistant',
      tag: `${item.agent_id || item.stage || 'agent'} · ${item.status || 'running'}`,
      content,
      thoughtOnly: true,
      source: 'generated',
      intentId: runtimeId,
    })
  })

  timeline.forEach((trace, index) => {
    const content = trace.output_summary
    if (!content || seenContents.has(content)) return
    seenContents.add(content)

    messages.push({
      id: `${id}-trace-${trace.id || trace.message_id || trace.message_type || index}`,
      role: 'assistant',
      tag: trace.message_type || trace.source_agent || 'a2a',
      content,
      thoughtOnly: true,
      source: 'generated',
      intentId: trace.intent_id || runtimeId,
    })
  })

  const conclusion = active.result?.decision_report?.final_conclusion
    || active.result?.decision_report?.summary
    || (active.statusClass === 'success' ? '本次网络意图已执行完成，结果已同步到拓扑与系统详情。' : '')
  if (conclusion && active.statusClass === 'success' && !seenContents.has(conclusion)) {
    messages.push({
      id: `${id}-final-${runtimeId}`,
      role: 'assistant',
      tag: 'NetPulse',
      content: conclusion,
      action: 'topology',
      report: active.result,
      intentId: runtimeId,
      source: 'generated',
    })
  } else if (active.statusClass === 'failed') {
    const errMsg = buildFailureContent(active)
    if (!seenContents.has(errMsg)) {
      messages.push({
        id: `${id}-failed-${runtimeId}`,
        role: 'assistant',
        tag: 'NetPulse',
        content: errMsg,
        action: active.result ? 'topology' : '',
        report: active.result || null,
        intentId: runtimeId,
        source: 'generated',
      })
    }
  }

  return messages
}

// ── 消息合并 & 工具附加 ───────────────────────────────────────────────────────

export function mergeConversationMessages(pinnedMessages, generatedMessages) {
  const merged = []
  const seen = new Set()
  const seenSemantic = new Map()

  ;[...pinnedMessages, ...generatedMessages].forEach((message) => {
    const content = String(message.content || '').replace(/\s+/g, ' ').trim()
    const key = message.id || `${message.role}:${message.tag || ''}:${content}`
    if (!content || seen.has(key)) return

    const role = message.role || 'assistant'
    const source = message.source || ''
    const intentId = messageIntentId(message)
    const semanticKey = intentId
      ? `${role}:${intentId}:${content}`
      : `${role}:${content}`
    const duplicateIndex = role === 'user' ? undefined : seenSemantic.get(semanticKey)

    if (duplicateIndex !== undefined) {
      const existing = merged[duplicateIndex]
      const existingSource = existing?.source || ''
      const isCacheDuplicate = source !== existingSource
        && ['cache', 'record', 'generated'].some((item) => item === source || item === existingSource)

      if (isCacheDuplicate) {
        merged[duplicateIndex] = {
          ...existing,
          action: existing.action || message.action || '',
          report: existing.report || message.report || null,
          intentId: existing.intentId || message.intentId || '',
          thoughtOnly: Boolean(existing.thoughtOnly || message.thoughtOnly),
        }
        seen.add(key)
        return
      }
    }

    seen.add(key)
    if (role !== 'user') seenSemantic.set(semanticKey, merged.length)
    merged.push(message)
  })

  return merged
}

export function attachFinalTools(messages, active) {
  if (!active) return messages
  const isFinished = active.statusClass === 'success' || active.statusClass === 'failed'
  if (!isFinished) return messages
  if (!active.result?.decision_report) return messages
  const lastIdx = messages.map((m) => m.role).lastIndexOf('assistant')
  if (lastIdx < 0) return messages
  return messages.map((m, i) =>
    i === lastIdx
      ? {
          ...m,
          action: 'topology',
          report: m.report || active.result,
          intentId: m.intentId || active.activeRunId || active.intent_id || active.id,
        }
      : m
  )
}
