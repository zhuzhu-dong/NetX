/**
 * useEventBus.js — 应用级事件总线
 *
 * 轻量级发布-订阅，用于 Pinia Store 间解耦通信。
 * 替代跨 Store 直接 import，消除双向依赖。
 *
 * 用法:
 *   import { useEventBus } from '../composables/useEventBus'
 *   const bus = useEventBus()
 *   bus.on('pipeline:stage-change', handler)
 *   bus.emit('pipeline:stage-change', payload)
 */
const listeners = new Map()

/** @returns {{ emit, on, off, once }} */
export function useEventBus() {
  return {
    emit(event, payload) {
      const handlers = listeners.get(event)
      if (!handlers) return
      for (const fn of handlers) {
        try { fn(payload) } catch (_) { /* best-effort */ }
      }
    },

    on(event, handler) {
      if (!listeners.has(event)) listeners.set(event, new Set())
      listeners.get(event).add(handler)
    },

    off(event, handler) {
      const handlers = listeners.get(event)
      if (handlers) handlers.delete(handler)
    },

    once(event, handler) {
      const wrapper = (payload) => {
        handler(payload)
        this.off(event, wrapper)
      }
      this.on(event, wrapper)
    },
  }
}
