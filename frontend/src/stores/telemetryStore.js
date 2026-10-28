/**
 * telemetryStore.js — 遥测、健康检查与 Watcher 状态
 *
 * 从 network.js 拆分。职责：KPI 指标、遥测轮询、系统健康、Watcher。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import * as api from '../api/client'

const TELEMETRY_POLL_MS = 1500
const TELEMETRY_IPERF_INTERVAL_MS = 60000
const DEEP_TELEMETRY_POLL_MS = 5000

function createMetrics() {
  return [
    { id: 'latency_ms', label: '核心时延', value: '-', rawValue: null, change: '等待遥测', trend: 'flat', tone: 'cyan', status: 'idle' },
    { id: 'packet_loss_rate', label: '主链路丢包', value: '-', rawValue: null, change: '等待遥测', trend: 'flat', tone: 'green', status: 'idle' },
    { id: 'throughput_mbps', label: '吞吐量', value: '-', rawValue: null, change: '等待遥测', trend: 'flat', tone: 'indigo', status: 'idle' },
    { id: 'queue_depth', label: '队列深度', value: '-', rawValue: null, change: '等待遥测', trend: 'flat', tone: 'slate', status: 'idle' },
  ]
}

function toMetric(raw) {
  return {
    id: raw.id || raw.key || raw.label || 'metric',
    label: raw.label || raw.id || 'metric',
    value: raw.value ?? '-',
    rawValue: raw.raw_value ?? null,
    change: raw.change || 'live',
    trend: raw.trend || 'flat',
    tone: raw.tone || 'slate',
    status: raw.status || 'unknown',
  }
}

export const useTelemetryStore = defineStore('telemetry', () => {
  /* ---- state ---- */
  const metrics = ref(createMetrics())
  const telemetryMode = ref('idle')
  const healthStatus = ref(null)
  const watcherStatus = ref(null)
  const watcherIncidents = ref([])
  const telemetryHistory = ref([])
  const telemetryLatestRaw = ref(null)
  const flowStats = ref(null)
  const deviceStats = ref(null)
  const intTelemetry = ref(null)
  const telemetryLastSampleAt = ref(null)
  const MAX_TELEMETRY_HISTORY = 20

  let telemetryPollTimer = null
  let deepTelemetryPollTimer = null
  let telemetryErrorLogged = false
  let lastIperfTelemetryAt = Date.now()
  let _lightTelemetryInFlight = false
  let _deepTelemetryInFlight = false
  let lastDirectCounterSample = null

  /* ---- actions ---- */
  async function loadTelemetry(options = {}) {
    // 防抖：上一个请求还没返回就直接跳过
    const isDeepProbe = Boolean(options.includeProbe || options.includeIperf)
    // Keep one request in flight for each cost class. A slow active
    // Mininet/P4 probe must not suppress regular ONOS counter sampling.
    if (isDeepProbe ? _deepTelemetryInFlight : _lightTelemetryInFlight) return null
    if (isDeepProbe) _deepTelemetryInFlight = true
    else _lightTelemetryInFlight = true
    const now = Date.now()
    const includeIperf = options.includeIperf ?? (now - lastIperfTelemetryAt >= TELEMETRY_IPERF_INTERVAL_MS)
    if (includeIperf) lastIperfTelemetryAt = now
    const requestOptions = {
      includeProbe: options.includeProbe ?? false,
      includeIperf,
    }

    try {
      const data = await api.getTelemetry(requestOptions)
      if (Array.isArray(data.metrics)) metrics.value = data.metrics.map(toMetric)
      telemetryMode.value = data?.status === 'ok' ? 'live' : 'unavailable'
      telemetryErrorLogged = false
      if (data.flows) flowStats.value = data.flows
      if (data.devices) deviceStats.value = data.devices
      // A telemetry chart is allowed to contain only server-issued samples.
      // Do not substitute the browser clock when the backend omits a sample
      // timestamp: that would fabricate a point in the historical series.
      const sampleTimestamp = typeof data.timestamp === 'string' && data.timestamp.trim()
        ? data.timestamp
        : null
      if (data.kpi && sampleTimestamp) {
        const previous = telemetryHistory.value.at(-1)
        const sample = {
          timestamp: sampleTimestamp,
          ...data.kpi,
          flow_total: data.flows?.total ?? null,
          flow_added: data.flows?.added ?? null,
          flow_packets: data.flows?.packets ?? null,
          flow_bytes: data.flows?.bytes ?? null,
          flow_packet_rate_pps: data.flows?.packet_rate_pps ?? null,
        }

        // ONOS infrastructure-flow counters and P4 classifier counters have
        // different scopes. Keep the former authoritative as-is, and expose
        // a separately labelled P4 byte-counter rate only when two real,
        // fresh P4 reads make that derived value possible.
        const directCounter = data.raw?.p4_direct_counter
        const directBytes = Number(directCounter?.bytes)
        const sampleEpoch = Date.parse(sampleTimestamp)
        if (
          directCounter?.status === 'MEASURED'
          && directCounter?.cached !== true
          && Number.isFinite(directBytes)
          && Number.isFinite(sampleEpoch)
        ) {
          if (lastDirectCounterSample) {
            const elapsedSeconds = (sampleEpoch - lastDirectCounterSample.timestamp) / 1000
            const byteDelta = directBytes - lastDirectCounterSample.bytes
            if (elapsedSeconds > 0 && byteDelta >= 0) {
              sample.p4_direct_counter_mbps = (byteDelta * 8) / elapsedSeconds / 1_000_000
            }
          }
          lastDirectCounterSample = { timestamp: sampleEpoch, bytes: directBytes }
        }

        // The backend cache may return the same measurement more than once.
        // Keep its one real point instead of making a synthetic-looking trend.
        if (previous?.timestamp !== sample.timestamp) {
          telemetryHistory.value = [...telemetryHistory.value, sample].slice(-MAX_TELEMETRY_HISTORY)
        }
        telemetryLastSampleAt.value = sampleTimestamp
      }
      if (data.raw) {
        telemetryLatestRaw.value = data.raw
      }
      return data
    } catch (e) {
      telemetryMode.value = 'unavailable'
      if (!telemetryErrorLogged) {
        console.error('遥测数据加载失败:', e.message)
        telemetryErrorLogged = true
      }
      return null
    } finally {
      if (isDeepProbe) _deepTelemetryInFlight = false
      else _lightTelemetryInFlight = false
    }
  }

  async function loadHealth() {
    try {
      const res = await fetch('/api/v1/health')
      healthStatus.value = await res.json()
    } catch (_) {
      healthStatus.value = { status: 'unreachable' }
    }
  }

  async function loadIntTelemetry() {
    try {
      // Direct P4 counters require the opt-in probe.  Reuse the normal
      // telemetry ingestion path so the raw P4 data, KPI cards and rolling
      // history always describe the same real backend response.
      const data = await loadTelemetry({ includeProbe: true, includeIperf: false })
      if (data) intTelemetry.value = data
      return data
    } catch (_) { /* INT probe not always available */ }
    return null
  }

  async function loadWatcher() {
    try {
      const [status, incidents] = await Promise.all([
        api.getWatcherStatus(),
        api.getWatcherIncidents(),
      ])
      watcherStatus.value = status
      // /watcher/incidents returns an envelope (`{ items: [...] }`), while
      // older adapters returned the array directly.  Preserve both forms so
      // the dashboard never treats a response object as a fabricated event
      // list or throws while rendering a real empty state.
      watcherIncidents.value = Array.isArray(incidents)
        ? incidents
        : Array.isArray(incidents?.items)
          ? incidents.items
          : []
    } catch (e) {
      console.error('Failed to load watcher state', e)
    }
  }

  function startTelemetryPolling(initialOptions = {}) {
    stopTelemetryPolling()
    loadTelemetry(initialOptions)
    telemetryPollTimer = setInterval(() => {
      loadTelemetry()
    }, TELEMETRY_POLL_MS)
    // Deep Mininet/P4 probes are materially more expensive than the ONOS
    // counter snapshot, so keep them on their own modest cadence.
    loadIntTelemetry()
    deepTelemetryPollTimer = setInterval(() => {
      loadIntTelemetry()
    }, DEEP_TELEMETRY_POLL_MS)
  }

  function stopTelemetryPolling() {
    if (telemetryPollTimer) {
      clearInterval(telemetryPollTimer)
      telemetryPollTimer = null
    }
    if (deepTelemetryPollTimer) {
      clearInterval(deepTelemetryPollTimer)
      deepTelemetryPollTimer = null
    }
  }

  /* ---- cleanup ---- */
  function cleanup() {
    stopTelemetryPolling()
  }

  return {
    metrics, telemetryMode, healthStatus, watcherStatus, watcherIncidents,
    telemetryHistory, telemetryLatestRaw, flowStats, deviceStats, intTelemetry, telemetryLastSampleAt,
    loadTelemetry, loadHealth, loadIntTelemetry, loadWatcher,
    startTelemetryPolling, stopTelemetryPolling,
    toMetric,
    cleanup,
  }
})
