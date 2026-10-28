export function sampleStatus(sample) {
  return sample?.status || 'NO_SAMPLE'
}

export function sampleAvailable(sample) {
  return sampleStatus(sample) === 'MEASURED'
}

export function displayValue(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '暂无有效样本'
  if (typeof value === 'number') {
    const rendered = Number.isInteger(value) ? value : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
    return `${rendered}${suffix}`
  }
  return `${value}${suffix}`
}

export function protocolMetricRows(profile = {}, sample = {}) {
  const protocol = String(sample.protocol || profile.protocol || '').toUpperCase()
  const metrics = sample.protocol_metrics || {}
  if (!sampleAvailable(sample)) {
    return [{ key: 'no-sample', label: '业务指标', value: '暂无有效样本' }]
  }
  if (protocol === 'UDP') {
    return [
      { key: 'throughput', label: 'UDP 接收吞吐量', value: displayValue(metrics.received_mbps, ' Mbps') },
      { key: 'loss', label: 'UDP 丢包率', value: displayValue(metrics.loss_percent, '%') },
      { key: 'lost', label: 'UDP lost_packets', value: displayValue(metrics.lost_packets) },
      { key: 'jitter', label: 'UDP 抖动', value: displayValue(metrics.jitter_ms, ' ms') },
    ]
  }
  if (protocol === 'TCP') {
    return [
      { key: 'sender', label: 'TCP sender goodput', value: displayValue(metrics.sender_mbps, ' Mbps') },
      { key: 'receiver', label: 'TCP receiver goodput', value: displayValue(metrics.receiver_mbps, ' Mbps') },
      { key: 'retransmits', label: 'TCP retransmits', value: displayValue(metrics.retransmits) },
      { key: 'rtt', label: 'TCP mean RTT', value: displayValue(metrics.mean_rtt_ms, ' ms') },
    ]
  }
  return [
    { key: 'samples', label: 'ICMP 样本', value: `${displayValue(metrics.received)}/${displayValue(metrics.transmitted)}` },
    { key: 'loss', label: 'ICMP 连通性丢包率', value: displayValue(metrics.loss_percent, '%') },
    { key: 'rtt', label: 'ICMP 平均 RTT', value: displayValue(metrics.rtt_avg_ms, ' ms') },
    { key: 'mdev', label: 'ICMP mdev', value: displayValue(metrics.rtt_mdev_ms, ' ms') },
  ]
}

export function networkHealthRows(networkHealth = {}) {
  const icmp = networkHealth.icmp || {}
  return [
    { key: 'reachability', label: 'ICMP 可达状态', value: icmp.status === 'MEASURED' ? '已测量' : '暂无有效样本' },
    { key: 'samples', label: 'ICMP 样本', value: icmp.received == null ? '暂无有效样本' : `${icmp.received}/${icmp.transmitted}` },
    { key: 'loss', label: 'ICMP 连通性丢包率', value: displayValue(icmp.loss_percent, '%') },
    { key: 'rtt', label: '基础 RTT', value: displayValue(icmp.rtt_avg_ms, ' ms') },
    { key: 'source', label: '数据来源', value: icmp.measurement_source || '暂无有效样本' },
    { key: 'time', label: '测量时间', value: icmp.sample_time || '暂无有效样本' },
  ]
}

export function resourceRows(sample = {}, infrastructure = {}) {
  const counter = sample.p4_counter_evidence || {}
  const tc = sample.tc_evidence || {}
  const delta = tc.delta || {}
  const queues = infrastructure.queues || {}
  return [
    { key: 'classifier', label: 'P4 分类器证据', value: counter.status || 'NO_MATCHING_ENTRY' },
    { key: 'p4-device', label: 'P4 设备 / 端点', value: [counter.device_id, counter.grpc_endpoint].filter(Boolean).join(' · ') || '暂无有效样本' },
    { key: 'p4-table', label: 'P4 table / counter', value: [counter.table_name || counter.table, counter.counter_name].filter(Boolean).join(' · ') || '暂无有效样本' },
    { key: 'p4-selector', label: 'P4 exact selector', value: counter.selector ? JSON.stringify(counter.selector) : '暂无有效样本' },
    { key: 'p4-match', label: 'P4 matching entries', value: displayValue(counter.matching_entry_count) },
    { key: 'counter', label: 'Direct Counter 增量', value: `${displayValue(counter.delta_packets, ' packets')} / ${displayValue(counter.delta_bytes, ' bytes')}` },
    { key: 'tc-binding', label: 'TC 绑定', value: [tc.switch, tc.interface, tc.class_id].filter(Boolean).join(' · ') || '暂无有效样本' },
    { key: 'tc-rate', label: 'TC rate / ceil', value: `${displayValue(tc.rate_mbps, ' Mbps')} / ${displayValue(tc.ceil_mbps, ' Mbps')}` },
    { key: 'tc-packets', label: 'TC class packets 增量', value: displayValue(delta.sent_packets) },
    { key: 'tc-bytes', label: 'TC class bytes 增量', value: displayValue(delta.sent_bytes, ' bytes') },
    { key: 'overlimits', label: 'TC 整形触发次数', value: displayValue(delta.overlimits) },
    { key: 'tc-dropped', label: 'TC dropped', value: displayValue(delta.dropped_packets) },
    { key: 'queue', label: '全局历史队列', value: displayValue(queues.queue_depth, ' pkts') },
  ]
}

export function validationStatusRows(sample = {}) {
  const validation = sample.validation_result || {}
  const primary = validation.primary_sla || {}
  const quality = validation.quality || {}
  const qualityDetail = [
    quality.loss?.measured != null ? `loss ${quality.loss.measured}% (${quality.loss.status})` : null,
    quality.jitter?.measured != null ? `jitter ${quality.jitter.measured} ms (${quality.jitter.status})` : null,
  ].filter(Boolean).join(' · ')
  return [
    { key: 'measurement', label: '测量状态', value: sample.measurement_status || sample.status || 'NO_SAMPLE' },
    {
      key: 'primary-sla',
      label: '主 SLA',
      value: `${primary.status || 'NOT_EVALUATED'}${primary.measured != null ? ` · ${primary.measured} ${primary.operator || ''} ${primary.declared_target ?? primary.target} Mbps` : ''}`,
    },
    { key: 'quality', label: '质量状态', value: `${validation.quality_status || 'NOT_EVALUATED'}${qualityDetail ? ` · ${qualityDetail}` : ''}` },
    { key: 'evidence', label: '证据状态', value: sample.evidence_status || validation.evidence_status || 'UNAVAILABLE' },
    { key: 'overall', label: '最终状态', value: sample.overall_status || validation.verification_state || 'NOT_EVALUATED' },
    { key: 'run', label: 'Validation run', value: sample.validation_run_id || '暂无有效样本' },
  ]
}
