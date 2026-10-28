export function createPollingController(callback, intervalMs, scheduler = globalThis) {
  let timer = null
  return {
    start() {
      callback()
      if (timer !== null) return
      timer = scheduler.setInterval(callback, intervalMs)
    },
    stop() {
      if (timer === null) return
      scheduler.clearInterval(timer)
      timer = null
    },
    isRunning() {
      return timer !== null
    },
  }
}
