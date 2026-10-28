export function consensusPresentation(decision = {}) {
  const reason = decision.reason_code || 'NONE'
  return {
    status: decision.status || 'UNKNOWN',
    blocking: Boolean(decision.blocking),
    reason,
    reasonText: reason === 'INSUFFICIENT_CAPACITY'
      ? '容量不足（INSUFFICIENT_CAPACITY）'
      : reason === 'CAPACITY_ADMISSION_REJECTED'
        ? '容量准入失败（CAPACITY_ADMISSION_REJECTED）'
      : reason === 'DEVICE_UNAVAILABLE'
        ? '设备不可用（DEVICE_UNAVAILABLE）'
        : reason,
    policyPreserved: decision.original_policy_preserved !== false,
    policyPreservedText: decision.original_policy_preserved === false ? '原策略未保留' : '原策略继续生效',
    repairActions: Array.isArray(decision.repair_actions) ? decision.repair_actions : [],
  }
}

export function qosModeLabel(mode) {
  if (String(mode).toUpperCase() === 'MIN_BANDWIDTH') return '最低带宽保障（MIN_BANDWIDTH）'
  if (String(mode).toUpperCase() === 'RATE_LIMIT') return '最高速率限制（RATE_LIMIT）'
  return 'QoS 模式未确定'
}

export function intQualityLabel(intEvidence = {}) {
  const status = String(intEvidence.quality_status || 'UNAVAILABLE').toUpperCase()
  if (status === 'TRUSTED') return 'INT 遥测：可信'
  if (status === 'DEGRADED') return 'INT 遥测：部分字段异常，未参与关键决策'
  if (status === 'UNTRUSTED') return 'INT 遥测：数据格式不一致，已排除'
  return 'INT 遥测：不可用'
}

export function pathChangePresentation(path = {}) {
  const changed = Boolean(path.path_changed)
  const current = path.current_path_id || path.path_id || '未识别'
  return {
    changed,
    changeText: changed ? '路径变化：有' : '路径变化：无',
    currentText: `当前路径：${current}`,
    migrationText: path.qos_resource_migrated ? 'QoS 资源已迁移' : 'QoS 资源未迁移',
    transitionText: changed
      ? `${path.previous_path_id || '未知'} → ${current}`
      : current,
  }
}

export function capacityViews(capacityEvidence = {}, expanded = false) {
  const physical = Array.isArray(capacityEvidence.physical_link_summary)
    ? capacityEvidence.physical_link_summary
    : []
  const raw = Array.isArray(capacityEvidence.raw_directed_links)
    ? capacityEvidence.raw_directed_links
    : []
  const defaultView = capacityEvidence.default_view || {}
  const focused = [
    ...(Array.isArray(defaultView.current_path_physical_links) ? defaultView.current_path_physical_links : []),
    ...(Array.isArray(defaultView.insufficient_physical_links) ? defaultView.insufficient_physical_links : []),
    defaultView.qos_egress,
    defaultView.bottleneck,
  ].filter(Boolean)
  const seen = new Set()
  const defaultItems = focused.filter((item) => {
    const key = item.physical_link_key || item.link_id
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
  return {
    expanded: Boolean(expanded),
    items: expanded ? raw : (defaultItems.length ? defaultItems : physical),
    physicalCount: physical.length,
    directedCount: raw.length,
    bottleneck: defaultView.bottleneck || null,
    blockingDirection: defaultView.blocking_direction || null,
    otherPhysicalLinkCount: Number(defaultView.other_physical_link_count || 0),
    toggleText: expanded ? '收起有向链路证据' : `展开全部有向链路证据（${raw.length}）`,
  }
}

export function policyEvidenceRows(executionPlane = {}, verification = {}) {
  const classifier = executionPlane.classifier || {}
  const counter = executionPlane.p4_counter || {}
  const scheduler = executionPlane.scheduler || {}
  const kpi = executionPlane.competition_kpi || verification || {}
  const databaseStatus = verification.database_status || verification.policy_status || 'UNKNOWN'
  return [
    { id: 'onos_flow', label: 'ONOS Flow', value: classifier.flow_state || 'UNAVAILABLE', trusted: classifier.flow_state === 'ADDED' },
    { id: 'p4_counter', label: 'P4 Direct Counter', value: counter.packets_delta ?? 0, trusted: Number(counter.packets_delta || 0) > 0 },
    { id: 'tc', label: 'TC 配置回读', value: `${scheduler.rate_mbps ?? '-'} / ${scheduler.ceil_mbps ?? '-'} Mbps`, trusted: Boolean(scheduler.readback_verified) },
    { id: 'kpi', label: 'Mininet ping/iperf + 背景竞争流量', value: kpi.verification_state || (kpi.passed ? 'VERIFIED' : 'UNVERIFIED'), trusted: Boolean(kpi.passed) },
    { id: 'database', label: '数据库状态', value: databaseStatus, trusted: databaseStatus === 'ACTIVE' && verification.pending_intent_id == null },
  ]
}
