/**
 * useDataExtractors.js — 深层数据访问辅助
 *
 * 封装 result.decision_report.* 的链式回退逻辑，
 * 使视图不直接依赖后台 JSON 结构。
 */

import { asArray } from './useFormatters'

// ── 回退链工具 ────────────────────────────────────────────────────────────────

export function firstNumber(...values) {
  for (const value of values) {
    const n = Number(value)
    if (Number.isFinite(n)) return n
  }
  return null
}

export function firstText(...values) {
  const value = values.find((item) => typeof item === 'string' && item.trim())
  return value ? value.trim() : '-'
}

export function firstNonEmptyArray(...values) {
  return values.find((item) => Array.isArray(item) && item.length) || []
}

// ── 报告数据提取 ──────────────────────────────────────────────────────────────

export function getSafetyReport(active) {
  const result = active?.result || {}
  const report = result.decision_report || {}
  return report.safety_assurance || result.safety_council_report || {}
}

// ── Runtime Timeline 提取（多路径回退）────────────────────────────────────────

export function getRuntimeTimeline(active) {
  const result = active?.result || {}
  const report = result.decision_report || {}
  const evidence = result.a2a_runtime_evidence || report.a2a_runtime_evidence || {}
  return evidence.messages || result.a2a_timeline || []
}

// ── KPI 对比（多源回退）───────────────────────────────────────────────────────

export function firstKpi(...items) {
  return items.find((item) => (
    item
    && typeof item === 'object'
    && Object.values(item).filter((value) => value !== null && value !== undefined).length >= 2
  )) || {}
}

// ── 策略详情辅助 ──────────────────────────────────────────────────────────────

export function formatActionList(value) {
  if (!value) return ''
  return Array.isArray(value) ? value.join(' + ') : String(value)
}

export function uniqueAgents(timeline) {
  return [...new Set((timeline || []).flatMap((item) => [item.source_agent, item.target_agent]).filter(Boolean))]
}

// ── Flow Table 计数 ───────────────────────────────────────────────────────────

export function countPlanEntries(plan = null) {
  if (!plan || typeof plan !== 'object') return null
  if (Number.isFinite(Number(plan.total_entries))) return Number(plan.total_entries)
  if (Array.isArray(plan.table_entries)) return plan.table_entries.length
  if (!Array.isArray(plan.write_requests)) return null
  return plan.write_requests.reduce((total, request) => total + asArray(request.table_entries).length, 0)
}

export function countPolicyResultEntries(items = []) {
  const total = asArray(items).reduce((sum, item) => {
    const count = firstNumber(
      item?.final_action?.flow_entries,
      item?.execution_result?.total_entries_written,
      countPlanEntries(item?.verified_flow_table_plan),
      countPlanEntries(item?.flow_table_plan),
    )
    return sum + (count || 0)
  }, 0)
  return total > 0 ? total : null
}

export function planAffectedDevices(plan = null) {
  if (!plan || typeof plan !== 'object') return []
  if (Array.isArray(plan.affected_devices) && plan.affected_devices.length) return plan.affected_devices
  return asArray(plan.write_requests).map((request) => request.device_id).filter(Boolean)
}

// ── 队列格式化 ────────────────────────────────────────────────────────────────

export function formatQueue(kpi = {}) {
  if (!Object.values(kpi || {}).some((value) => value !== null && value !== undefined)) return ''
  const backlog = kpi.queue_depth ?? 0
  const drops = kpi.queue_drops ?? 0
  const overlimits = kpi.queue_overlimits_delta ?? kpi.queue_overlimits
  const overText = overlimits !== undefined && overlimits !== null ? `overlimits ${overlimits}` : 'overlimits -'
  return `${backlog} pkts / drops ${drops} / ${overText}`
}
