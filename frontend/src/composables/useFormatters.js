/**
 * useFormatters.js — 共享格式化函数
 *
 * 从 NetworkOverview / AgentRun / SelfHealingReport 中提取，
 * 消除三处重复定义。
 */

// ── 数值格式化 ────────────────────────────────────────────────────────────────

export function formatMbps(value) {
  if (value === null || value === undefined || value === '') return ''
  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)
  return n >= 1000
    ? `${(n / 1000).toFixed(n % 1000 === 0 ? 0 : 1)}Gbps`
    : `${Math.round(n * 100) / 100} Mbps`
}

export function formatMs(value) {
  if (value === null || value === undefined || value === '') return ''
  const n = Number(value)
  return Number.isFinite(n) ? `${Math.round(n * 100) / 100} ms` : String(value)
}

export function formatPercent(value) {
  if (value === null || value === undefined || value === '') return ''
  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)
  const pct = Math.abs(n) <= 1 ? n * 100 : n
  return `${Math.round(pct * 100) / 100}%`
}

export function formatScore(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return n <= 1 ? `${Math.round(n * 100)}%` : String(Math.round(n * 100) / 100)
}

export function formatSeconds(value) {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? `${Math.round(n)}s` : ''
}

export function formatCounter(counter) {
  if (!counter) return ''
  const packets = counter.packets ?? '-'
  const bytes = counter.bytes ?? '-'
  return `${packets} pkts / ${bytes} bytes`
}

export function formatPlannerCritic(critic) {
  if (!critic || !Object.keys(critic).length) return ''
  const conflict = critic.conflict ? '有差异' : '无差异'
  const missing = (critic.missing_slots || []).join('/')
  return missing ? `${conflict}; 缺槽位 ${missing}` : `${conflict}; ${critic.mode || 'cross_check'}`
}

// ── 百分比归一化 ──────────────────────────────────────────────────────────────

export function normalizePercent(value) {
  if (value === undefined || value === null || value === '') return ''
  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)
  return n <= 1 ? `${(n * 100).toFixed(n < 0.01 ? 2 : 1)}%` : `${n}%`
}

// ── 通用工具 ──────────────────────────────────────────────────────────────────

export function asArray(value) {
  if (!value) return []
  return Array.isArray(value) ? value : [value]
}

export function similarityScore(item = {}) {
  const n = Number(item.similarity ?? item.rerank_score ?? item.score)
  return Number.isFinite(n) ? n : 0
}

export function belongsToCurrentIntent(item, intentId) {
  return Boolean(intentId && item?.intent_id && item.intent_id === intentId)
}

// ── SafetyReview 归一化 ───────────────────────────────────────────────────────

export function normalizeSafetyReview(review = {}) {
  const agent = String(review.agent_id || review.agent || '').toLowerCase()
  if (!agent.includes('history_risk')) return review

  const cases = asArray(review.details?.similar_cases)
  const strongCases = cases.filter((item) => similarityScore(item) >= 0.5)
  if (strongCases.length) {
    return {
      ...review,
      evidence: strongCases
        .slice(0, 3)
        .map((item) => `${item.case_id || 'case'} similarity=${similarityScore(item).toFixed(2)}`),
    }
  }

  return {
    ...review,
    decision: review.decision || review.vote || 'approve',
    reason: '未发现高相似历史风险案例，当前决策主要依据实时遥测和 SafetyCouncil。',
    evidence: ['no_high_similarity_history_case'],
    warnings: [],
  }
}
