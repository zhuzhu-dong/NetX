export function llmRuntimeLabel(mode, status = '') {
  const runtime = String(mode || '').toUpperCase()
  const callStatus = String(status || '').toUpperCase()
  if (runtime === 'REAL_LLM' && callStatus === 'RUNNING') return '正在调用真实大模型'
  if (runtime === 'REAL_LLM' && callStatus === 'SUCCESS') return '真实大模型语义决策完成'
  if (runtime === 'MOCK_LLM') return '测试模式：模拟大模型响应'
  if (runtime === 'DETERMINISTIC_FALLBACK') return '大模型暂不可用，已使用确定性降级'
  if (runtime === 'LLM_UNAVAILABLE') return '大模型不可用'
  return '未使用大模型'
}

export function agentAuthority(agentId, runtimeMode) {
  const id = String(agentId || '')
  if (['intent_execution', 'execution_agent', 'intent_assurance', 'conflict_safety'].includes(id)) {
    return 'DETERMINISTIC'
  }
  if (id === 'intent_planning' && runtimeMode === 'REAL_LLM') {
    return 'REAL_LLM + deterministic validation'
  }
  if (id === 'intent_root_cause' && runtimeMode === 'REAL_LLM') {
    return 'REAL_LLM + fact-based diagnosis'
  }
  if (id === 'intent_understanding' && runtimeMode === 'REAL_LLM') return 'REAL_LLM'
  return runtimeMode || 'DETERMINISTIC'
}
