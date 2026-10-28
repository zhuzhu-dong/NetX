export function infrastructurePresentation(lifecycle, conflict = {}) {
  const type = String(conflict.conflict_type || conflict.type || '').toUpperCase()
  if (lifecycle === 'WAITING_FOR_INFRASTRUCTURE' || type === 'INFRASTRUCTURE_UNAVAILABLE') {
    return {
      tone: 'bad',
      title: '部署状态：WAITING_FOR_INFRASTRUCTURE',
      message: conflict.explanation || '计划依赖的网络设备当前不可用，Intent 暂未部署；设备恢复后将继续当前 Intent。',
    }
  }
  if (type === 'NETWORK_CONTEXT_UNKNOWN' || type === 'NETWORK_CONTEXT_STALE') {
    return {
      tone: 'warn',
      title: '设备状态：正在重新检查',
      message: conflict.explanation || '无法确认设备的实时可用状态，正在刷新控制器与 P4Runtime 证据。',
    }
  }
  return null
}
