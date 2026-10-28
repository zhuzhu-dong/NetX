import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const dashboard = read('../src/views/dashboard/DashboardOverview.vue')
const telemetryStore = read('../src/stores/telemetryStore.js')
const intentStudio = read('../src/views/dashboard/NetworkOverview.vue')

test('telemetry dashboard never renders the retired synthetic startup baseline', () => {
  assert.doesNotMatch(dashboard, /BASELINE_SAMPLES|BASELINE_METRICS|baselineRows/)
  assert.doesNotMatch(dashboard, /26\.4|60\.8|56\.9 Mbps|8\.1 ms/)
  assert.match(dashboard, /NO SAMPLE/)
})

test('rolling history accepts only timestamped backend samples and retains a bounded window', () => {
  assert.match(telemetryStore, /typeof data\.timestamp === 'string/)
  assert.match(telemetryStore, /previous\?\.timestamp !== sample\.timestamp/)
  assert.match(telemetryStore, /slice\(-MAX_TELEMETRY_HISTORY\)/)
  assert.doesNotMatch(telemetryStore, /data\.timestamp \|\| new Date\(\)\.toISOString\(\)/)
  assert.match(telemetryStore, /p4_direct_counter_mbps/)
  assert.match(telemetryStore, /directCounter\?\.cached !== true/)
})

test('real zero measurements remain valid while null and non-numeric values remain absent', () => {
  const helperSource = dashboard.match(/function isMetricValue\(value\) \{[\s\S]*?\n\}/)?.[0]
  assert.ok(helperSource, 'Dashboard must define isMetricValue')
  const isMetricValue = Function(`${helperSource}; return isMetricValue`)()
  assert.equal(isMetricValue(0), true)
  assert.equal(isMetricValue(0.0), true)
  assert.equal(isMetricValue(null), false)
  assert.equal(isMetricValue(undefined), false)
  assert.equal(isMetricValue(NaN), false)
  assert.match(dashboard, /return isMetricValue\(value\) \? Number\(value\) : null/)
  assert.match(dashboard, /throughputValues\.some\(isMetricValue\)/)
  assert.match(dashboard, /lossValues\.some\(isMetricValue\)/)
  assert.match(dashboard, /packetRateValues\.some\(isMetricValue\)/)
})

test('time-series keep partial real samples and make all-zero telemetry visible without inventing data', () => {
  assert.match(dashboard, /throughput: finiteNumber\(item\.throughput_mbps\)/)
  assert.match(dashboard, /latency: finiteNumber\(item\.latency_ms\)/)
  assert.match(dashboard, /loss: finiteNumber\(item\.packet_loss_rate\)/)
  assert.match(dashboard, /packetRate: finiteNumber\(item\.flow_packet_rate_pps\)/)
  assert.match(dashboard, /isMetricValue\(value\) \? value \* 100 : null/)
  assert.match(dashboard, /showSymbol: allMeasuredValuesAreZero\(throughputValues\)/)
  assert.match(dashboard, /showSymbol: allMeasuredValuesAreZero\(lossValues\)/)
  assert.match(dashboard, /type: allMeasuredValuesAreZero\(packetRateValues\) \? 'line' : 'bar'/)
  assert.match(dashboard, /zeroOnly \? \{ min: 0, max: 1, interval: 1 \} : \{\}/)
  assert.doesNotMatch(dashboard, /BASELINE_SAMPLES|BASELINE_METRICS|new Date\(\)\.toISOString\(\)/)
})

test('light telemetry and P4 probe use independent bounded polling cadences without Watcher polling', () => {
  assert.match(telemetryStore, /const TELEMETRY_POLL_MS = 1500/)
  assert.match(telemetryStore, /const DEEP_TELEMETRY_POLL_MS = 5000/)
  assert.match(telemetryStore, /loadTelemetry\(\{ includeProbe: true, includeIperf: false \}\)/)
  assert.match(telemetryStore, /_lightTelemetryInFlight/)
  assert.match(telemetryStore, /_deepTelemetryInFlight/)
  assert.match(telemetryStore, /clearInterval\(deepTelemetryPollTimer\)/)
  const pollingRegion = telemetryStore.slice(
    telemetryStore.indexOf('function startTelemetryPolling'),
    telemetryStore.indexOf('/* ---- cleanup ---- */'),
  )
  assert.doesNotMatch(pollingRegion, /loadWatcher|watcherPollTimer|WATCHER_POLL_MS/)
  assert.doesNotMatch(dashboard, /telemetry\.loadWatcher\(\)/)
  assert.doesNotMatch(dashboard, /watcherIncidents|watcherStatus|observerChannels/)
  assert.match(dashboard, /暂无网络事件/)
  assert.match(dashboard, /EVENT SOURCE NOT AVAILABLE/)
})

test('ONOS infrastructure throughput and derived P4 classifier throughput remain visibly separate', () => {
  assert.match(dashboard, /基础设施聚合吞吐/)
  assert.match(dashboard, /ONOS FLOW COUNTER/)
  assert.match(dashboard, /业务分类吞吐/)
  assert.match(dashboard, /P4 CLASSIFIER · DERIVED/)
  assert.match(dashboard, /derived from consecutive real P4 direct-byte counter samples/)
  assert.match(dashboard, /name: 'ONOS infrastructure'/)
  assert.match(dashboard, /name: 'P4 classifier \(derived\)'/)
  assert.doesNotMatch(dashboard, /ONOS flow counter \/ P4 direct rate/)
})

test('dashboard starts a lightweight feed, never an automatic iperf run, and releases resources on unmount', () => {
  assert.match(dashboard, /startTelemetryPolling\(\{ includeProbe: false, includeIperf: false \}\)/)
  assert.doesNotMatch(dashboard, /startTelemetryPolling\(\{ includeProbe: true, includeIperf: true \}\)/)
  assert.match(dashboard, /onUnmounted\(\(\) => \{[\s\S]*telemetry\.stopTelemetryPolling\(\)/)
  assert.match(dashboard, /topology\.stopTopologySSE\(\)/)
  assert.match(dashboard, /CanvasRenderer/)
  assert.match(dashboard, /LineChart/)
  assert.match(dashboard, /GraphicComponent/)
})

test('the older intent-studio telemetry helper has no fixed synthetic trend fallback', () => {
  assert.doesNotMatch(intentStudio, /Array\.from\(\{ length: 8 \}/)
})
