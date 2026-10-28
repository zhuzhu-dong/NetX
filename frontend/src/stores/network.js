/**
 * network.js — 兼容性重导出
 *
 * 原 monolithic store 已拆分为 5 个聚焦 store。
 * useNetworkStore() 通过 Proxy 透明转发到各子 store，保持完全兼容。
 * 新代码建议直接从特定 store 导入：useTopologyStore / useIntentStore / etc.
 */
export { useTopologyStore } from './topologyStore'
export { useIntentStore } from './intentStore'
export { usePipelineStore } from './pipelineStore'
export { useTelemetryStore } from './telemetryStore'
export { useHealingStore } from './healingStore'

import { useTopologyStore } from './topologyStore'
import { useIntentStore } from './intentStore'
import { usePipelineStore } from './pipelineStore'
import { useTelemetryStore } from './telemetryStore'
import { useHealingStore } from './healingStore'

/** @deprecated 直接使用 useTopologyStore / useIntentStore / ... */
export function useNetworkStore() {
  const topology = useTopologyStore()
  const intent = useIntentStore()
  const pipeline = usePipelineStore()
  const telemetry = useTelemetryStore()
  const healing = useHealingStore()

  // Proxy 转发所有属性访问到子 store，保持 Pinia 响应式
  return new Proxy({}, {
    get(_, key) {
      for (const store of [topology, intent, pipeline, telemetry, healing]) {
        if (key in store) return store[key]
      }
      return undefined
    },
    set(_, key, value) {
      for (const store of [topology, intent, pipeline, telemetry, healing]) {
        if (key in store) {
          store[key] = value
          return true
        }
      }
      return false
    },
    has(_, key) {
      return [topology, intent, pipeline, telemetry, healing].some(s => key in s)
    },
    ownKeys() {
      const keys = new Set()
      for (const store of [topology, intent, pipeline, telemetry, healing]) {
        Object.keys(store).forEach(k => keys.add(k))
      }
      return [...keys]
    },
    getOwnPropertyDescriptor(_, key) {
      for (const store of [topology, intent, pipeline, telemetry, healing]) {
        if (key in store) {
          return { enumerable: true, configurable: true, value: store[key] }
        }
      }
      return undefined
    },
  })
}
