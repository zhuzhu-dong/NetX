const TERMINAL_LIFECYCLES = new Set([
  'SATISFIED',
  'UNSATISFIABLE',
  'FAILED',
  'WITHDRAWN',
  'NEEDS_CLARIFICATION',
])

const REASON_LABELS = {
  TARGET_PATH_ALREADY_ACTIVE: '目标路径已是当前活动路径',
  BOTTLENECK_STILL_ON_PATH: '候选路径仍经过当前瓶颈',
  SHARED_BOTTLENECK_NOT_AVOIDED: '候选路径未绕开共享瓶颈',
  REPEATED_FAILED_ACTION: '相同运行上下文中该动作已失败',
  NO_PREEMPTIBLE_RESOURCE: '当前瓶颈没有可抢占资源',
  PROTECTED_INTENT: '受保护 Intent 不允许降级',
  INSUFFICIENT_RECOVERABLE_CAPACITY: '可安全回收的带宽不足',
  INSUFFICIENT_EXPECTED_CAPACITY: '候选方案预计容量不足',
  NO_OP_PLAN: '计划不产生数据面变化',
  NO_FEASIBLE_HEALING_ACTION: '不存在合法且有预期收益的修复动作',
  TELEMETRY_INSUFFICIENT: '运行证据不足，禁止自动执行',
}

function samePath(left, right) {
  const a = Array.isArray(left) ? left.map(String) : []
  const b = Array.isArray(right) ? right.map(String) : []
  return a.length > 0 && a.length === b.length && a.every((item, index) => item === b[index])
}

function latestHealing(items) {
  const rows = Array.isArray(items) ? items : []
  return rows[0] || {}
}

export function isCompletedHealingState(value) {
  return TERMINAL_LIFECYCLES.has(String(value || '').toUpperCase())
}

export function candidateEvaluationRows(items, currentPath = []) {
  const latest = latestHealing(items)
  const proposalStatus = String(latest.status || '').toUpperCase()
  const selectedActionId = latest.selected_action?.action_id
  const evaluations = Array.isArray(latest.candidate_evaluations)
    ? latest.candidate_evaluations
    : []

  return evaluations.map((item) => {
    const action = String(item.action || '')
    const targetPath = item.target_path || item.params?.target_path
      || latest.proposed_actions?.find((candidate) => candidate.action_id === item.action_id)?.params?.target_path
      || []
    const serverApplicability = String(item.applicability || 'NOT_APPLICABLE').toUpperCase()
    const redundantCurrentPath = action === 'REROUTE' && samePath(targetPath, currentPath)
    const applicability = redundantCurrentPath ? 'REDUNDANT' : serverApplicability
    const reasonCode = redundantCurrentPath
      ? 'TARGET_PATH_ALREADY_ACTIVE'
      : String(item.reason_code || 'NONE').toUpperCase()
    const selected = Boolean(selectedActionId && selectedActionId === item.action_id)
    const executable = selected
      && applicability === 'APPLICABLE'
      && ['APPROVED', 'EXECUTING', 'VERIFIED'].includes(proposalStatus)

    return {
      id: item.candidate_id || item.action_id,
      action,
      target: item.target || '-',
      targetPath,
      applicability,
      reasonCode,
      reason: REASON_LABELS[reasonCode] || item.reason || '通过适用性校验',
      expectedGainMbps: item.expected_effect?.expected_gain_mbps,
      expectedAvailableMbps: item.expected_effect?.expected_available_mbps,
      bottleneckAvoided: item.expected_effect?.bottleneck_avoided,
      affectedIntents: item.affected_intents || item.expected_effect?.affected_intents || [],
      selected,
      executable,
      phaseLabel: executable ? '计划执行' : applicability === 'APPLICABLE' ? '候选修复建议' : '不可执行',
    }
  })
}

export function healingExecutionRows(items) {
  return (Array.isArray(items) ? items : [])
    .filter((item) => item?.data_plane_changed === true)
    .map((item) => ({
      id: item.healing_id || item.attempt_id,
      action: item.selected_action?.type || '-',
      planId: item.plan_id || '-',
      executionId: item.execution_id || '-',
      status: item.attempt_status || item.status || 'EXECUTED',
      dataPlaneChanged: true,
    }))
}

export function measurementConfirmationRows(items) {
  return (Array.isArray(items) ? items : [])
    .filter((item) => String(item?.attempt_type || '').toUpperCase() === 'MEASUREMENT_CONFIRMATION')
    .map((item) => ({
      id: item.measurement_id || item.attempt_id || item.healing_id,
      status: item.attempt_status || item.status || 'VERIFIED',
      measurementId: item.measurement_id || '-',
    }))
}

export function buildHealingPresentation({
  items = [],
  currentPath = [],
  lifecycle = '',
  llmRuntimeMode = '',
} = {}) {
  const latest = latestHealing(items)
  const candidates = candidateEvaluationRows(items, currentPath)
  const proposed = Array.isArray(latest.proposed_actions) ? latest.proposed_actions : []
  const bottlenecks = proposed.flatMap((item) => item.params?.bottleneck_link_ids || [])
  const recoverable = candidates
    .filter((item) => ['THROTTLE_LOWER_PRIORITY', 'QOS_REALLOCATION', 'RELEASE_STALE_RESERVATION'].includes(item.action))
    .map((item) => Number(item.expectedGainMbps || 0))
  const state = String(lifecycle || '').toUpperCase()
  const assurance = latest.assurance || latest.verification_report || {}
  const assuranceState = String(assurance.verification_state || assurance.phase || '').toUpperCase()
  const firstSample = Array.isArray(assurance.samples) ? assurance.samples[0] : {}
  const receivedMbps = firstSample?.received_mbps ?? assurance?.kpi?.throughput_mbps
  const thresholdMbps = firstSample?.threshold_mbps ?? assurance?.threshold_mbps
  const firstMeasurement = Number.isFinite(Number(receivedMbps)) && Number.isFinite(Number(thresholdMbps))
    ? `首次性能测量为 ${Number(receivedMbps).toFixed(1)} Mbps，低于有效门槛 ${Number(thresholdMbps).toFixed(1)} Mbps。`
    : '首次性能测量低于有效门槛。'
  const assuranceConclusion = assuranceState === 'CONFIRMING_VIOLATION'
    ? `${firstMeasurement}系统正在进行独立复测，尚未确认持续违反（${assurance.attempt_index || 1}/${assurance.max_attempts || 3}）`
    : assuranceState === 'VIOLATION_CONFIRMED_ROOT_CAUSE_PENDING'
      ? '已确认持续低于目标，但当前根因证据不足，正在补充确定性证据。'
      : assuranceState === 'DEGRADED_ROOT_CAUSE_UNCONFIRMED'
        ? '当前服务处于降级状态；系统已停止重复执行相同策略，等待新的遥测或环境变化后继续诊断。'
      : assuranceState === 'SATISFIED_WITH_TRANSIENT_WARNING'
        ? '后续独立测量已恢复达标；首次下降作为瞬时告警保留。'
        : null

  return {
    currentPath,
    currentPathLabel: currentPath.length ? currentPath.join(' → ') : '尚无可信路径证据',
    bottleneck: [...new Set(bottlenecks)].join(', ') || '-',
    candidates,
    executions: healingExecutionRows(items),
    confirmations: measurementConfirmationRows(items),
    recoverableMbps: recoverable.length ? Math.max(...recoverable) : 0,
    lifecycle: state || 'UNKNOWN',
    completed: isCompletedHealingState(state),
    conclusion: state === 'UNSATISFIABLE'
      ? 'UNSATISFIABLE · 当前没有合法且有预期收益的修复动作'
      : state || '等待诊断',
    llmRuntimeMode,
    assuranceState,
    diagnosticRound: assurance.diagnostic_round,
    maxDiagnosticRounds: assurance.max_diagnostic_rounds,
    diagnosticsRequired: assurance.diagnostics_required === true,
    missingEvidence: assurance.missing_evidence || assurance.root_cause_report?.missing_evidence || [],
    ...(assuranceConclusion ? { conclusion: assuranceConclusion } : {}),
  }
}
