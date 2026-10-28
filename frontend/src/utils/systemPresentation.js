const OK = new Set(['ok', 'healthy', 'ready', 'live', 'alive'])

function stateOf(value) {
  return String(value ?? '').trim().toLowerCase()
}

export function mapBackendReadiness(payload, loading = false) {
  if (loading || !payload) {
    return { status: 'CHECKING', display_status: 'CHECKING', detail: '正在检查后端依赖' }
  }
  const overall = stateOf(payload.status)
  const components = payload.components || {}
  const required = ['onos', 'flow_delivery', 'mininet', 'database', 'telemetry', 'worker']
  const failed = required.filter((name) => {
    const state = stateOf(components[name]?.status)
    return state && !OK.has(state)
  })
  if (overall === 'healthy' && failed.length === 0) {
    return { ...payload, status: 'READY', display_status: 'READY', failed_components: [] }
  }
  if (overall === 'degraded' || failed.length > 0) {
    return {
      ...payload,
      status: 'DEGRADED',
      display_status: 'DEGRADED',
      failed_components: failed,
    }
  }
  return { ...payload, status: 'UNAVAILABLE', display_status: 'UNAVAILABLE', failed_components: failed }
}

export function unavailableBackendReadiness(error) {
  return {
    status: 'UNAVAILABLE',
    display_status: 'UNAVAILABLE',
    detail: error?.message || '后端健康检查不可达',
    failed_components: [],
  }
}

export function infrastructureTrafficPresentation(sample) {
  if (!sample || sample.status !== 'MEASURED' || sample.value_mbps == null) {
    return { value: null, text: '暂无有效样本', measured: false, unit: 'Mbps' }
  }
  const value = Number(sample.value_mbps)
  return {
    value,
    text: `${Number.isFinite(value) ? value.toFixed(2) : '-'} Mbps`,
    measured: Number.isFinite(value),
    unit: 'Mbps',
  }
}

export function validationTrendPoint(serviceId, sample) {
  if (!sample || sample.measurement_status !== 'MEASURED' || !sample.validation_run_id) return null
  const protocol = sample.protocol_metrics || {}
  const throughput = protocol.received_mbps ?? protocol.receiver_mbps
  const tc = sample.tc_evidence || {}
  const delta = tc.delta || {}
  const duration = Number(sample.duration_seconds)
  const ceil = Number(tc.ceil_mbps)
  const tcMbps = duration > 0 && delta.sent_bytes != null
    ? Number(delta.sent_bytes) * 8 / duration / 1_000_000
    : null
  const utilization = Number.isFinite(tcMbps) && ceil > 0 ? tcMbps / ceil * 100 : null
  return {
    service_id: serviceId,
    validation_run_id: sample.validation_run_id,
    timestamp: sample.validation_ended_at || sample.end_time || new Date().toISOString(),
    throughput_mbps: Number.isFinite(Number(throughput)) ? Number(throughput) : null,
    bottleneck_utilization_percent: Number.isFinite(utilization) ? utilization : null,
    throughput_unit: 'Mbps',
    utilization_unit: '%',
  }
}

export function appendValidationTrend(history, point, limit = 20) {
  if (!point) return history || []
  const prior = Array.isArray(history) ? history : []
  const withoutSameRun = prior.filter((item) => item.validation_run_id !== point.validation_run_id)
  return [...withoutSameRun, point].slice(-limit)
}
