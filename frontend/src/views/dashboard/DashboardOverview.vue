<template>
  <section class="network-page">
    <header class="control-ribbon">
      <div class="view-identity">
        <span class="identity-mark" aria-hidden="true"></span>
        <div>
          <p>NETWORK / LIVE FABRIC</p>
          <h1>网络视图</h1>
        </div>
      </div>

      <dl class="ribbon-facts">
        <div>
          <dt>数据源</dt>
          <dd>{{ sourceLabel }}</dd>
        </div>
        <div>
          <dt>刷新周期</dt>
          <dd>1.5 s</dd>
        </div>
        <div>
          <dt>最近采样</dt>
          <dd>{{ lastSampleLabel }}</dd>
        </div>
      </dl>

      <div class="ribbon-actions">
        <span class="sync-state" :class="{ waiting: !hasTopology }">
          <i></i>
          {{ hasTopology ? '实时同步' : '等待控制面' }}
        </span>
        <button
          class="refresh-button"
          type="button"
          aria-label="刷新网络数据"
          title="刷新网络数据"
          :disabled="isRefreshing"
          @click="refreshAll"
        >
          <span :class="{ spinning: isRefreshing }">↻</span>
        </button>
      </div>
    </header>

    <section class="topology-workspace">
      <header class="workspace-bar">
        <div>
          <span class="workspace-dot"></span>
          <strong>实时网络拓扑</strong>
          <small>拖动、缩放并选择节点查看详情</small>
        </div>
        <div class="fabric-facts">
          <span><b>{{ nodeCount }}</b> 节点</span>
          <span><b>{{ linkCount }}</b> 链路</span>
          <span :class="['fabric-health', overallTone]">{{ overallHealth }}</span>
        </div>
      </header>

      <div class="topology-stage">
        <TopologyMap
          minimal
          :nodes="topology.topologyNodes"
          :links="topology.topologyLinks"
          :metrics="telemetry.metrics"
          :selected-node-id="topology.selectedNodeId"
          @select-node="topology.selectNode"
          @quick-intent="handleQuickIntent"
        />

        <div v-if="!hasTopology" class="topology-empty" role="status">
          <span class="loading-ring" aria-hidden="true"></span>
          <strong>正在连接网络控制面</strong>
          <p>后台返回拓扑后，将在此绘制交换机、主机和链路关系。</p>
        </div>
      </div>
    </section>

    <section class="monitor-board">
      <header class="monitor-head">
        <div>
          <p>LIVE TELEMETRY</p>
          <h2>网络运行监控</h2>
        </div>
        <div class="monitor-meta">
          <span>{{ sampleCountLabel }}</span>
          <span>{{ flowCountLabel }}</span>
          <span>{{ telemetryStatusLabel }}</span>
        </div>
      </header>

      <div class="kpi-row">
        <article
          v-for="metric in primaryMetrics"
          :key="metric.label"
          class="kpi-cell"
          :class="[metricTone(metric), { preview: metric.preview }]"
        >
          <div class="kpi-heading">
            <span>{{ metric.label }}</span>
            <i></i>
          </div>
          <strong>{{ metric.value }}</strong>
          <small>{{ metric.change || '等待遥测数据' }}</small>
        </article>
      </div>

      <section class="monitor-section traffic-section">
        <div class="panel-title">
          <div>
            <p>TRAFFIC</p>
            <h3>实时吞吐趋势</h3>
          </div>
          <span>{{ throughputSourceLabel }}</span>
        </div>
        <VChart
          v-if="throughputOption"
          class="wide-chart"
          :option="throughputOption"
          autoresize
        />
        <div v-if="throughputOption" class="telemetry-source-note">
          <span>基础设施聚合吞吐 · ONOS FLOW COUNTER</span>
          <span>业务分类吞吐 · P4 CLASSIFIER · DERIVED</span>
          <small>derived from consecutive real P4 direct-byte counter samples</small>
        </div>
        <div v-else class="board-empty">
          <span></span>
          <strong>等待吞吐量采样</strong>
          <small>等待 ONOS FLOW COUNTER 或 P4 direct-byte counter 样本</small>
        </div>
      </section>

      <div class="analytics-grid">
        <section class="monitor-section quality-panel">
          <div class="panel-title">
            <div>
              <p>QUALITY</p>
              <h3>链路质量趋势</h3>
            </div>
            <span>{{ qualitySourceLabel }}</span>
          </div>
          <VChart
            v-if="qualityOption"
            class="compact-chart"
            :option="qualityOption"
            autoresize
          />
          <div v-else class="board-empty compact">
            <strong>暂无主动探针数据</strong>
            <small>深度采样返回时延或丢包后显示</small>
          </div>
        </section>

        <section class="monitor-section health-panel">
          <div class="panel-title">
            <div>
              <p>HEALTH</p>
              <h3>节点健康分布</h3>
            </div>
            <span>{{ nodeCount }} total</span>
          </div>
          <div v-if="healthOption" class="health-content">
            <VChart class="health-chart" :option="healthOption" autoresize />
            <div class="health-legend">
              <div v-for="item in healthRows" :key="item.key">
                <i :style="{ backgroundColor: item.color }"></i>
                <span>{{ item.label }}</span>
                <b>{{ item.value }}</b>
              </div>
            </div>
          </div>
          <div v-else class="board-empty compact">
            <strong>暂无节点状态</strong>
            <small>等待拓扑数据</small>
          </div>
        </section>

        <section class="monitor-section node-ledger">
          <div class="panel-title">
            <div>
              <p>ENDPOINTS</p>
              <h3>节点状态明细</h3>
            </div>
            <span>实时</span>
          </div>
          <div v-if="nodeRows.length" class="ledger-table">
            <div class="ledger-head">
              <span>节点</span>
              <span>类型</span>
              <span>链路</span>
              <span>状态</span>
            </div>
            <button
              v-for="row in nodeRows"
              :key="row.id"
              type="button"
              class="ledger-row"
              @click="topology.selectNode(row.raw)"
            >
              <strong>{{ row.name }}</strong>
              <span>{{ row.type }}</span>
              <span>{{ row.connections }}</span>
              <em :class="row.tone"><i></i>{{ row.state }}</em>
            </button>
          </div>
          <div v-else class="board-empty compact">
            <strong>暂无节点数据</strong>
            <small>等待拓扑数据</small>
          </div>
        </section>
      </div>

      <section class="intent-impact">
        <header class="impact-head">
          <div>
            <p>INTENT OUTCOMES</p>
            <h3>意图作用视图</h3>
          </div>
          <span>{{ visualIntentCount }} 条可视记录</span>
        </header>

        <div class="impact-grid">
          <section class="monitor-section impact-panel flow-impact">
            <div class="panel-title">
              <div>
                <p>FORWARDING LOAD</p>
                <h3>转发负载画像</h3>
              </div>
              <span>{{ flowScopeLabel }}</span>
            </div>
            <VChart
              v-if="flowProfileOption"
              class="impact-chart"
              :option="flowProfileOption"
              autoresize
            />
            <div v-else class="board-empty impact-empty">
              <strong>暂无转发采样</strong>
              <small>等待 ONOS FLOW COUNTER、P4 direct-byte counter 或 ONOS 包速率样本</small>
            </div>
            <dl class="flow-facts">
              <div>
                <dt>ONOS 活动流表</dt>
                <dd>{{ flowStatValue('total', '条') }}</dd>
              </div>
              <div>
                <dt>ONOS 包速率</dt>
                <dd>{{ flowStatValue('packet_rate_pps', 'pps') }}</dd>
              </div>
              <div>
                <dt>ONOS 累计报文</dt>
                <dd>{{ compactNumber(telemetry.flowStats?.packets) }}</dd>
              </div>
              <div>
                <dt>ONOS 累计字节</dt>
                <dd>{{ formatBytes(telemetry.flowStats?.bytes) }}</dd>
              </div>
            </dl>
          </section>

          <section class="monitor-section impact-panel access-impact">
            <div class="panel-title">
              <div>
                <p>ACCESS BOUNDARY</p>
                <h3>访问边界状态</h3>
              </div>
              <span>{{ accessRows.length }} 条策略</span>
            </div>
            <div v-if="accessRows.length" class="impact-table access-table">
              <div class="impact-table-head">
                <span>作用对象</span>
                <span>策略方式</span>
                <span>结果</span>
              </div>
              <div v-for="row in accessRows" :key="row.id" class="impact-table-row">
                <strong :title="row.target">{{ row.target }}</strong>
                <span>{{ row.mode }}</span>
                <em :class="row.tone"><i></i>{{ row.status }}</em>
              </div>
            </div>
            <div v-else class="board-empty impact-empty">
              <strong>暂无访问边界记录</strong>
              <small>隔离或阻断类意图完成后在此形成策略清单</small>
            </div>
          </section>

          <section class="monitor-section impact-panel qos-impact">
            <div class="panel-title">
              <div>
                <p>SERVICE ENVELOPE</p>
                <h3>服务质量约束</h3>
              </div>
              <span>{{ serviceEnvelopeSourceLabel }}</span>
            </div>

            <div class="service-envelope-body">
              <div class="service-radar-stage">
                <VChart
                  v-if="serviceEnvelopeOption"
                  class="service-radar"
                  :option="serviceEnvelopeOption"
                  autoresize
                />
                <div v-else class="service-radar-empty">
                  NO COMPLETE SAMPLE
                </div>
                <div class="quality-index" aria-label="服务质量相对指数">
                  <strong>{{ serviceEnvelopeScore }}</strong>
                  <span>质量指数</span>
                </div>
              </div>

              <div class="service-metric-grid">
                <article
                  v-for="item in serviceMetricRows"
                  :key="item.key"
                  class="service-metric"
                >
                  <span><i :class="item.tone"></i>{{ item.label }}</span>
                  <strong>{{ item.value }}</strong>
                  <small>{{ item.caption }}</small>
                </article>
              </div>
            </div>

            <div class="policy-ribbon">
              <div class="policy-ribbon-label">
                <span>POLICY DELIVERY</span>
                <strong>策略执行面</strong>
              </div>
              <div v-if="servicePolicyRows.length" class="policy-ribbon-list">
                <article v-for="row in servicePolicyRows" :key="row.id">
                  <i :class="row.tone"></i>
                  <span :title="row.target">{{ row.target }}</span>
                  <strong>{{ row.detail }}</strong>
                  <em :class="row.tone">{{ row.status }}</em>
                </article>
              </div>
              <div v-else class="policy-ribbon-empty">
                尚未下发可量化带宽或访问控制约束，质量画像仍依据当前遥测与拓扑状态生成。
              </div>
            </div>
          </section>
        </div>
      </section>

      <div class="operations-grid">
        <section class="monitor-section operation-panel">
          <div class="panel-title">
            <div>
              <p>INT / P4</p>
              <h3>带内遥测状态</h3>
            </div>
            <span>{{ intSourceLabel }}</span>
          </div>
          <dl class="probe-grid">
            <div>
              <dt>路径跳数</dt>
              <dd>{{ intValue('int_hop_count', 'hops') }}</dd>
            </div>
            <div>
              <dt>路径时延</dt>
              <dd>{{ intValue('int_path_latency_ms', 'ms') }}</dd>
            </div>
            <div>
              <dt>最大队列</dt>
              <dd>{{ intValue('int_max_queue_occupancy', '') }}</dd>
            </div>
            <div>
              <dt>P4 计数</dt>
              <dd>{{ p4CounterLabel }}</dd>
            </div>
          </dl>
          <div class="telemetry-channel-grid">
            <div v-for="channel in telemetryChannels" :key="channel.key">
              <i :class="channel.tone"></i>
              <span>{{ channel.label }}</span>
              <small>{{ channel.detail }}</small>
            </div>
          </div>
        </section>

        <section class="monitor-section operation-panel">
          <div class="panel-title">
            <div>
              <p>EVENTS</p>
              <h3>近期网络事件</h3>
            </div>
          <span>NOT AVAILABLE</span>
          </div>
          <div v-if="recentEvents.length" class="operation-list">
            <div v-for="event in recentEvents" :key="event.id">
              <i :class="event.tone"></i>
              <span>
                <strong>{{ event.title }}</strong>
                <small>{{ event.detail }}</small>
              </span>
              <time>{{ event.time }}</time>
            </div>
          </div>
          <div v-else class="board-empty compact">
            <strong>暂无网络事件</strong>
            <small>EVENT SOURCE NOT AVAILABLE</small>
          </div>
        </section>

        <section class="monitor-section operation-panel">
          <div class="panel-title">
            <div>
              <p>INTENTS</p>
              <h3>近期意图</h3>
            </div>
            <span>{{ recentIntents.length }} 条</span>
          </div>
          <div v-if="intentStatusOption" class="intent-outcome-strip">
            <VChart class="intent-outcome-chart" :option="intentStatusOption" autoresize />
            <div class="intent-outcome-copy">
              <strong>{{ completedIntentCount }}</strong>
              <span>已完成编排</span>
            </div>
          </div>
          <div v-if="recentIntents.length" class="operation-list">
            <button
              v-for="item in recentIntents"
              :key="item.id"
              type="button"
              @click="goToIntent(item)"
            >
              <i :class="intentTone(item)"></i>
              <span>
                <strong>{{ item.title || item.text || '未命名意图' }}</strong>
                <small>{{ statusLabel(item.status) }}</small>
              </span>
              <time>{{ shortId(item.id) }}</time>
            </button>
          </div>
          <div v-else class="intent-empty-state">
            <strong>等待首条网络意图</strong>
            <small>执行后的策略状态与结果分布将在此沉淀</small>
          </div>
        </section>
      </div>
    </section>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart, PieChart, RadarChart } from 'echarts/charts'
import { GraphicComponent, GridComponent, LegendComponent, RadarComponent, TooltipComponent } from 'echarts/components'
import { useTelemetryStore } from '../../stores/telemetryStore'
import { useTopologyStore } from '../../stores/topologyStore'
import { useIntentStore } from '../../stores/intentStore'
import { useNetworkStore } from '../../stores/network'
import TopologyMap from '../../components/dashboard/TopologyMap.vue'

// vue-echarts uses the modular ECharts build; register exactly the renderers,
// chart types and components this page renders so live samples can be drawn.
use([
  CanvasRenderer,
  BarChart,
  LineChart,
  PieChart,
  RadarChart,
  GridComponent,
  GraphicComponent,
  LegendComponent,
  RadarComponent,
  TooltipComponent,
])

const router = useRouter()
const telemetry = useTelemetryStore()
const topology = useTopologyStore()
const intent = useIntentStore()
const network = useNetworkStore()
const isRefreshing = ref(false)

const STATUS_LABELS = {
  normal: '正常',
  healthy: '正常',
  degraded: '降级',
  failed: '故障',
  attack: '异常',
  success: '已完成',
  running: '执行中',
  planning: '编排中',
  pending: '等待中',
  clarification: '待补充',
  pending_clarification: '待补充',
  cancelled: '已取消',
}

const PRIMARY_METRICS = [
  { id: 'latency_ms', label: '核心时延' },
  { id: 'packet_loss_rate', label: '主链路丢包' },
  { id: 'throughput_mbps', label: '基础设施聚合吞吐' },
  { id: 'queue_depth', label: '队列深度' },
]
const INTENT_POLL_MS = 10000
let intentPollTimer = null

const nodeCount = computed(() => topology.topologyNodes.length)
const linkCount = computed(() => topology.topologyLinks.length)
const hasTopology = computed(() => nodeCount.value > 0)
const historyCount = computed(() => telemetry.telemetryHistory.length)

const sourceLabel = computed(() => {
  const source = String(topology.topologySource || '').trim()
  if (!source || source === 'connecting') return 'CONTROL PLANE'
  return source.replaceAll('_', ' ').toUpperCase()
})

const lastSampleLabel = computed(() => {
  const latest = telemetry.telemetryHistory.at(-1)
  if (!latest?.timestamp) return '--:--:--'
  return formatClock(latest.timestamp)
})

const flowCountLabel = computed(() => {
  const count = telemetry.telemetryLatestRaw?.flow_count
  return Number.isFinite(Number(count)) ? `${Number(count)} 条流表` : '流表未采集'
})

const telemetryStatusLabel = computed(() => {
  const metrics = telemetry.metrics || []
  const total = Math.max(metrics.length, PRIMARY_METRICS.length)
  const available = metrics.filter(metricAvailable).length
  if (available === total) return `${available}/${total} 实时指标`
  if (!available) return `NO SAMPLE · 0/${total}`
  return `${available}/${total} 实时 · ${total - available} 无样本`
})

const primaryMetrics = computed(() => {
  const metrics = telemetry.metrics || []
  return PRIMARY_METRICS.map((definition) => {
    const metric = metrics.find((item) => item.id === definition.id || item.label === definition.label)
    return metricAvailable(metric)
      ? { ...metric, label: definition.label, preview: false }
      : { ...definition, value: '-', rawValue: null, change: '等待实际遥测样本', status: 'idle', preview: false }
  })
})

const failedNodes = computed(() =>
  topology.topologyNodes.filter((node) => ['failed', 'attack'].includes(node.state)).length
)

const degradedNodes = computed(() =>
  topology.topologyNodes.filter((node) => node.state === 'degraded').length
)

const overallHealth = computed(() => {
  if (!hasTopology.value) return '状态未就绪'
  if (failedNodes.value) return `${failedNodes.value} 个异常`
  if (degradedNodes.value) return `${degradedNodes.value} 个降级`
  return '运行正常'
})

const overallTone = computed(() => {
  if (failedNodes.value) return 'danger'
  if (degradedNodes.value) return 'warning'
  return hasTopology.value ? 'healthy' : 'neutral'
})

const historyRows = computed(() =>
  (telemetry.telemetryHistory || []).map((item) => ({
    time: formatClock(item.timestamp),
    throughput: finiteNumber(item.throughput_mbps),
    p4DirectRate: finiteNumber(item.p4_direct_counter_mbps),
    latency: finiteNumber(item.latency_ms),
    loss: finiteNumber(item.packet_loss_rate),
    packetRate: finiteNumber(item.flow_packet_rate_pps),
  }))
)

const hasLiveThroughput = computed(() =>
  metricAvailable(metricFor('throughput_mbps'))
  && historyRows.value.some((item) => isMetricValue(item.throughput))
)

const hasLiveQuality = computed(() =>
  (
    metricAvailable(metricFor('latency_ms'))
    || metricAvailable(metricFor('packet_loss_rate'))
  )
  && historyRows.value.some((item) => isMetricValue(item.latency) || isMetricValue(item.loss))
)

const throughputRows = computed(() => historyRows.value)

const qualityRows = computed(() => historyRows.value)

const sampleCountLabel = computed(() =>
  hasLiveThroughput.value || hasLiveQuality.value
    ? `${historyCount.value} 个实时采样点`
    : 'NO SAMPLE'
)

const throughputSourceLabel = computed(() =>
  hasLiveThroughput.value ? 'ONOS FLOW COUNTER · LIVE' : 'NO SAMPLE'
)

const qualitySourceLabel = computed(() =>
  hasLiveQuality.value ? 'Mininet ping · LIVE / CACHE' : 'NO SAMPLE'
)

const throughputOption = computed(() => {
  const chartRows = historyRows.value
  const throughputValues = chartRows.map((item) => item.throughput)
  const p4DirectRateValues = chartRows.map((item) => item.p4DirectRate)
  const hasThroughput = throughputValues.some(isMetricValue)
  const hasP4DirectRate = p4DirectRateValues.some(isMetricValue)
  if (!hasThroughput && !hasP4DirectRate) return null
  const series = []
  if (hasThroughput) {
    series.push({
      name: 'ONOS infrastructure', type: 'line', smooth: 0.34,
      showSymbol: allMeasuredValuesAreZero(throughputValues), symbolSize: 6,
      data: throughputValues,
      lineStyle: { width: 2, color: '#2f9ed6' }, itemStyle: { color: '#2f9ed6' },
    })
  }
  if (hasP4DirectRate) {
    series.push({
      name: 'P4 classifier (derived)', type: 'line', smooth: 0.24,
      showSymbol: allMeasuredValuesAreZero(p4DirectRateValues), symbolSize: 6,
      data: p4DirectRateValues,
      lineStyle: { width: 2, color: '#7c74d8' }, itemStyle: { color: '#7c74d8' },
    })
  }
  return {
    animationDuration: 420,
    backgroundColor: 'transparent',
    legend: {
      top: 8,
      left: 'center',
      itemWidth: 14,
      itemHeight: 2,
      itemGap: 18,
      textStyle: { color: '#66778d', fontSize: 10 },
    },
    grid: { left: 22, right: 30, top: 50, bottom: 16, containLabel: true },
    tooltip: chartTooltip('Mbps'),
    xAxis: chartCategoryAxis(chartRows.map((item) => item.time)),
    yAxis: chartValueAxis('Mbps', [...throughputValues, ...p4DirectRateValues]),
    series,
  }
})

const qualityOption = computed(() => {
  const rows = qualityRows.value
  const latencyValues = rows.map((item) => item.latency)
  const lossValues = rows.map((item) => item.loss)
  const hasLatency = latencyValues.some(isMetricValue)
  const hasLoss = lossValues.some(isMetricValue)
  if (!hasLatency && !hasLoss) return null

  const series = []
  if (hasLatency) {
    series.push({
      name: '时延',
      type: 'line',
      yAxisIndex: 0,
      smooth: 0.32,
      showSymbol: allMeasuredValuesAreZero(latencyValues),
      symbolSize: 6,
      connectNulls: false,
      data: latencyValues,
      lineStyle: { width: 2, color: '#2f9ed6' },
      itemStyle: { color: '#2f9ed6' },
    })
  }
  if (hasLoss) {
    series.push({
      name: '丢包',
      type: 'line',
      yAxisIndex: 1,
      smooth: 0.32,
      showSymbol: allMeasuredValuesAreZero(lossValues),
      symbolSize: 6,
      connectNulls: false,
      data: lossValues.map((value) => isMetricValue(value) ? value * 100 : null),
      lineStyle: { width: 2, color: '#d8922b' },
      itemStyle: { color: '#d8922b' },
    })
  }

  return {
    animationDuration: 420,
    backgroundColor: 'transparent',
    legend: {
      top: 8,
      left: 'center',
      itemWidth: 14,
      itemHeight: 2,
      itemGap: 18,
      textStyle: { color: '#66778d', fontSize: 10 },
    },
    grid: { left: 22, right: 48, top: 50, bottom: 16, containLabel: true },
    tooltip: chartTooltip(),
    xAxis: chartCategoryAxis(rows.map((item) => item.time)),
    yAxis: [
      chartValueAxis('ms', latencyValues),
      {
        ...chartValueAxis('%', lossValues.map((value) => isMetricValue(value) ? value * 100 : null)),
        position: 'right',
        splitLine: { show: false },
      },
    ],
    series,
  }
})

const healthRows = computed(() => {
  const counts = { normal: 0, degraded: 0, failed: 0, attack: 0 }
  for (const node of topology.topologyNodes) {
    const state = counts[node.state] == null ? 'normal' : node.state
    counts[state] += 1
  }
  return [
    { key: 'normal', label: '正常', value: counts.normal, color: '#18a978' },
    { key: 'degraded', label: '降级', value: counts.degraded, color: '#d8922b' },
    { key: 'failed', label: '故障', value: counts.failed, color: '#dc5a68' },
    { key: 'attack', label: '异常', value: counts.attack, color: '#7c74d8' },
  ].filter((item) => item.value > 0)
})

const healthOption = computed(() => {
  if (!nodeCount.value) return null
  return {
    animationDuration: 520,
    tooltip: {
      ...chartTooltip(),
      formatter: '{b}: {c}',
    },
    series: [{
      type: 'pie',
      radius: ['60%', '82%'],
      center: ['50%', '50%'],
      silent: false,
      label: { show: false },
      emphasis: { scaleSize: 5 },
      itemStyle: { borderColor: '#f7fbff', borderWidth: 3 },
      data: healthRows.value.map((item) => ({
        name: item.label,
        value: item.value,
        itemStyle: { color: item.color },
      })),
    }],
    graphic: [{
      type: 'text',
      left: 'center',
      top: '43%',
      style: {
        text: String(nodeCount.value),
        fill: '#17243a',
        fontSize: 24,
        fontWeight: 800,
        textAlign: 'center',
      },
    }, {
      type: 'text',
      left: 'center',
      top: '58%',
      style: {
        text: '节点',
        fill: '#718198',
        fontSize: 10,
        textAlign: 'center',
      },
    }],
  }
})

const nodeRows = computed(() => {
  const links = topology.topologyLinks || []
  return (topology.topologyNodes || []).slice(0, 6).map((node) => ({
    id: node.id,
    raw: node,
    name: node.name || node.id,
    type: nodeTypeLabel(node),
    connections: links.filter((link) => link.from === node.id || link.to === node.id).length,
    state: STATUS_LABELS[node.state] || node.state || '未知',
    tone: node.state === 'normal' ? 'healthy' : node.state === 'degraded' ? 'warning' : 'danger',
  }))
})

const conversationSessionIds = computed(() => new Set(
  (intent.conversationHistory || [])
    .filter((record) => {
      const owner = String(record.owner || record.user_id || '').toLowerCase()
      const title = String(record.title || record.text || '')
      return owner !== 'watcher' && !/系统后台\s*Watcher/i.test(title)
    })
    .map((record) => record.sessionId || record.session_id || record.id)
    .filter(Boolean)
))

const conversationLinkedIntents = computed(() => {
  const sessionIds = conversationSessionIds.value
  return (intent.intentHistory || []).filter((record) => {
    const sessionId = record.sessionId || record.session_id
    const recordId = String(record.intent_id || record.id || '')
    const owner = String(record.owner || record.user_id || '').toLowerCase()
    const text = String(record.title || record.text || record.content || '')
    return Boolean(
      sessionId
      && sessionIds.has(sessionId)
      && /^INT-/i.test(recordId)
      && !record.isConversation
      && owner !== 'watcher'
      && !/系统后台\s*Watcher/i.test(text)
    )
  })
})

const intentEffectRows = computed(() =>
  conversationLinkedIntents.value.flatMap((record) => {
    const runtimes = Object.entries(record.intentResults || {})
    if (!runtimes.length) return [record]

    return runtimes.map(([intentId, runtime]) => ({
      ...record,
      ...runtime,
      id: intentId,
      intent_id: intentId,
      parentId: record.id,
      title: runtime.text || userTextForIntent(record, intentId) || record.title,
      text: runtime.text || userTextForIntent(record, intentId) || record.text,
      snapshot: runtime.snapshot || record.snapshot,
      result: runtime.result || record.result,
      status: runtime.status || record.status,
      statusClass: runtime.statusClass || record.statusClass,
    }))
  })
)

const flowIntentRows = computed(() =>
  intentEffectRows.value
    .filter((item) => /(监控.*流量|流量.*监控|实时流表|流量统计|端口状态|流表统计)/i.test(intentText(item)))
    .slice(0, 6)
)

const accessRows = computed(() =>
  intentEffectRows.value
    .filter((item) => /(隔离|阻断|drop|移出.*转发|关闭.*访问)/i.test(intentText(item)))
    .slice(0, 5)
    .map((item, index) => ({
      id: item.id || item.intent_id || `access-${index}`,
      target: extractIntentTarget(item),
      mode: accessMode(item),
      status: displayIntentStatus(item),
      tone: intentTone(item),
    }))
)

const qosRows = computed(() =>
  intentEffectRows.value
    .map((item, index) => {
      const value = extractBandwidthMbps(item)
      if (value == null) return null
      return {
        id: item.id || item.intent_id || `qos-${index}`,
        target: extractIntentTarget(item),
        mode: bandwidthMode(item),
        value,
        status: displayIntentStatus(item),
        tone: intentTone(item),
      }
    })
    .filter(Boolean)
    .slice(0, 4)
)

const visualIntentCount = computed(() => {
  const ids = new Set([
    ...flowIntentRows.value.map((item) => item.id || item.intent_id),
    ...accessRows.value.map((item) => item.id),
    ...qosRows.value.map((item) => item.id),
  ].filter(Boolean))
  return ids.size
})

const reachableNodeCount = computed(() =>
  (topology.topologyNodes || []).filter((node) => node.state !== 'failed' && node.state !== 'attack').length
)

const serviceMetricRows = computed(() => {
  const metricMap = new Map(primaryMetrics.value.map((item) => [item.id, item]))
  const throughput = metricMap.get('throughput_mbps')
  const latency = metricMap.get('latency_ms')
  const loss = metricMap.get('packet_loss_rate')
  const queue = metricMap.get('queue_depth')
  const availability = nodeCount.value
    ? Math.round((reachableNodeCount.value / nodeCount.value) * 100)
    : 0

  return [
    {
      key: 'throughput',
      label: '基础设施聚合吞吐',
      value: throughput?.value || '-',
      score: metricAvailable(throughput) ? scaleToPercent(throughput?.rawValue, 0, 100) : null,
      tone: metricTone(throughput),
      isLive: metricAvailable(throughput),
      caption: metricAvailable(throughput) ? 'ONOS FLOW COUNTER' : 'NO SAMPLE',
    },
    {
      key: 'latency',
      label: '时延稳定',
      value: latency?.value || '-',
      score: metricAvailable(latency) ? invertToPercent(latency?.rawValue, 0, 50) : null,
      tone: metricTone(latency),
      isLive: metricAvailable(latency),
      caption: metricAvailable(latency) ? '实时采样 · 路径时延' : 'NO SAMPLE',
    },
    {
      key: 'loss',
      label: '丢包控制',
      value: loss?.value || '-',
      score: metricAvailable(loss) ? invertToPercent(loss?.rawValue, 0, 2) : null,
      tone: metricTone(loss),
      isLive: metricAvailable(loss),
      caption: metricAvailable(loss) ? '实时采样 · 主链路丢包' : 'NO SAMPLE',
    },
    {
      key: 'queue',
      label: '队列余量',
      value: queue?.value || '-',
      score: metricAvailable(queue) ? invertToPercent(queue?.rawValue, 0, 100) : null,
      tone: metricTone(queue),
      isLive: metricAvailable(queue),
      caption: metricAvailable(queue) ? '实时采样 · 队列深度' : 'NO SAMPLE',
    },
    {
      key: 'availability',
      label: '节点可达',
      value: nodeCount.value ? `${reachableNodeCount.value}/${nodeCount.value}` : '待拓扑',
      score: availability,
      tone: failedNodes.value ? 'danger' : degradedNodes.value ? 'warning' : hasTopology.value ? 'healthy' : 'neutral',
      isLive: hasTopology.value,
      caption: hasTopology.value ? `${availability}% 可达 · 控制面同步` : '等待控制面拓扑同步',
    },
  ]
})

const serviceEnvelopeScore = computed(() => {
  const scores = serviceMetricRows.value.map((item) => item.score).filter((value) => Number.isFinite(value))
  if (!scores.length) return '--'
  return Math.round(scores.reduce((total, value) => total + value, 0) / scores.length)
})

const serviceEnvelopeSourceLabel = computed(() => {
  const liveCount = serviceMetricRows.value.filter((item) => item.isLive).length
  return serviceEnvelopeHasCompleteSample.value
    ? 'QUALITY MODEL · DERIVED FROM LIVE SAMPLES'
    : `NO COMPLETE SAMPLE · ${liveCount}/5 AVAILABLE`
})

const serviceEnvelopeHasCompleteSample = computed(() =>
  serviceMetricRows.value.every((item) => Number.isFinite(item.score))
)

const serviceEnvelopeOption = computed(() => {
  if (!serviceEnvelopeHasCompleteSample.value) return null
  return {
  animationDuration: 680,
  animationEasing: 'cubicOut',
  tooltip: {
    ...chartTooltip(),
    trigger: 'item',
    formatter() {
      return serviceMetricRows.value.map((item) => (
        `${item.label}<span style="float:right;margin-left:22px;font-weight:700">${item.value}</span><br/>`
      )).join('')
    },
  },
  radar: {
    center: ['50%', '52%'],
    radius: '68%',
    startAngle: 90,
    splitNumber: 4,
    indicator: serviceMetricRows.value.map((item) => ({ name: item.label, max: 100 })),
    axisName: { color: '#62758e', fontSize: 10, fontWeight: 700 },
    axisLine: { lineStyle: { color: 'rgba(91, 158, 230, 0.28)' } },
    splitLine: { lineStyle: { color: 'rgba(91, 158, 230, 0.2)' } },
    splitArea: { areaStyle: { color: ['rgba(238, 249, 255, 0.38)', 'rgba(248, 252, 255, 0.14)'] } },
  },
  series: [{
    type: 'radar',
    symbol: 'circle',
    symbolSize: 5,
    data: [{
      value: serviceMetricRows.value.map((item) => item.score),
      name: '服务质量画像',
      lineStyle: { width: 2, color: '#2f9ed6' },
      itemStyle: { color: '#2f9ed6' },
      areaStyle: { color: 'rgba(47, 158, 214, 0.2)' },
    }],
  }],
  }
})

const servicePolicyRows = computed(() => {
  const rows = [
    ...qosRows.value.map((row) => ({
    id: `qos-${row.id}`,
    target: row.target,
    detail: `${row.mode} ${formatRate(row.value)}`,
    status: row.status,
    tone: row.tone,
    })),
    ...accessRows.value.map((row) => ({
    id: `access-${row.id}`,
    target: row.target,
    detail: row.mode,
    status: row.status,
    tone: row.tone,
    })),
  ]

  const uniqueRows = new Map()
  for (const row of rows) {
    const key = `${row.target}|${row.detail}`.toLowerCase()
    if (!uniqueRows.has(key)) uniqueRows.set(key, row)
  }
  return [...uniqueRows.values()].slice(0, 4)
})

const flowScopeLabel = computed(() => {
  const latest = flowIntentRows.value[0]
  return latest ? extractIntentTarget(latest) : 'ONOS FLOW COUNTER · INFRASTRUCTURE'
})

const flowProfileOption = computed(() => {
  const rows = throughputRows.value
  const throughputValues = rows.map((item) => item.throughput)
  const p4DirectRateValues = rows.map((item) => item.p4DirectRate)
  const packetRateValues = rows.map((item) => item.packetRate)
  const hasThroughput = throughputValues.some(isMetricValue)
  const hasP4DirectRate = p4DirectRateValues.some(isMetricValue)
  const hasPacketRate = packetRateValues.some(isMetricValue)
  if (!hasThroughput && !hasP4DirectRate && !hasPacketRate) return null

  const series = []
  if (hasThroughput) {
    series.push({
      name: 'ONOS infrastructure',
      type: 'line',
      yAxisIndex: 0,
      smooth: 0.34,
      showSymbol: allMeasuredValuesAreZero(throughputValues),
      symbolSize: 6,
      data: throughputValues,
      lineStyle: { width: 2, color: '#2f9ed6' },
      itemStyle: { color: '#2f9ed6' },
      areaStyle: {
        color: {
          type: 'linear',
          x: 0,
          y: 0,
          x2: 0,
          y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(47, 158, 214, 0.2)' },
            { offset: 1, color: 'rgba(47, 158, 214, 0.01)' },
          ],
        },
      },
    })
  }
  if (hasP4DirectRate) {
    series.push({
      name: 'P4 classifier (derived)',
      type: 'line',
      yAxisIndex: 0,
      smooth: 0.24,
      showSymbol: allMeasuredValuesAreZero(p4DirectRateValues),
      symbolSize: 6,
      data: p4DirectRateValues,
      lineStyle: { width: 2, color: '#7c74d8' },
      itemStyle: { color: '#7c74d8' },
    })
  }
  if (hasPacketRate) {
    series.push({
      name: '包速率',
      // A zero-height bar is not visually distinguishable from no series.
      // Preserve the real zero value but use a line with sample markers when
      // every measured packet-rate point is zero.
      type: allMeasuredValuesAreZero(packetRateValues) ? 'line' : 'bar',
      yAxisIndex: 1,
      barMaxWidth: 8,
      showSymbol: allMeasuredValuesAreZero(packetRateValues),
      symbolSize: 6,
      connectNulls: false,
      data: packetRateValues,
      lineStyle: { width: 2, color: '#18a978' },
      itemStyle: {
        color: 'rgba(24, 169, 120, 0.38)',
        borderColor: '#18a978',
        borderWidth: 1,
        borderRadius: [2, 2, 0, 0],
      },
    })
  }

  return {
    animationDuration: 420,
    legend: {
      top: 8,
      left: 'center',
      itemWidth: 14,
      itemHeight: 2,
      itemGap: 18,
      textStyle: { color: '#66778d', fontSize: 10 },
    },
    grid: { left: 22, right: 52, top: 50, bottom: 16, containLabel: true },
    tooltip: chartTooltip(),
    xAxis: chartCategoryAxis(rows.map((item) => item.time)),
    yAxis: [
      chartValueAxis('Mbps', [...throughputValues, ...p4DirectRateValues]),
      {
        ...chartValueAxis('pps', packetRateValues),
        position: 'right',
        splitLine: { show: false },
      },
    ],
    series,
  }
})

const recentEvents = computed(() => [])

const recentIntents = computed(() => conversationLinkedIntents.value.slice(0, 4))

const telemetryChannels = computed(() => [
  {
    key: 'kpi',
    label: 'KPI 采样',
    detail: telemetry.telemetryMode === 'live' ? '实时同步' : 'NO SAMPLE / STALE',
    tone: telemetry.telemetryMode === 'live' ? 'healthy' : 'neutral',
  },
  {
    key: 'flows',
    label: '流表计数',
    detail: telemetry.flowStats?.total != null ? `${compactNumber(telemetry.flowStats.total)} 条流表` : '等待流表增量',
    tone: telemetry.flowStats?.total != null ? 'healthy' : 'neutral',
  },
  {
    key: 'int',
    label: 'INT 探针',
    detail: telemetry.intTelemetry?.kpi ? '探针已回传' : '探针未就绪',
    tone: telemetry.intTelemetry?.kpi ? 'healthy' : 'warning',
  },
  {
    key: 'p4',
    label: 'P4 计数器',
    detail: p4CounterLabel.value === '-' ? '等待读取' : p4CounterLabel.value,
    tone: p4CounterLabel.value === '-' ? 'neutral' : 'healthy',
  },
])


const intentStatusRows = computed(() => {
  const groups = new Map([
    ['healthy', { label: '已完成', value: 0, color: '#18a978' }],
    ['warning', { label: '待补充', value: 0, color: '#d8922b' }],
    ['danger', { label: '失败', value: 0, color: '#dc5a68' }],
    ['active', { label: '处理中', value: 0, color: '#2f9ed6' }],
  ])
  for (const item of conversationLinkedIntents.value) {
    const key = intentTone(item)
    const group = groups.get(key) || groups.get('active')
    group.value += 1
  }
  return [...groups.entries()].map(([key, value]) => ({ key, ...value })).filter((item) => item.value > 0)
})

const completedIntentCount = computed(() =>
  intentStatusRows.value.find((item) => item.key === 'healthy')?.value || 0
)

const intentStatusOption = computed(() => {
  if (!intentStatusRows.value.length) return null
  return {
    animationDuration: 520,
    tooltip: { ...chartTooltip(), formatter: '{b}: {c} 条' },
    series: [{
      type: 'pie',
      radius: ['66%', '88%'],
      center: ['50%', '50%'],
      label: { show: false },
      labelLine: { show: false },
      itemStyle: { borderColor: '#f8fcff', borderWidth: 2 },
      data: intentStatusRows.value.map((item) => ({
        name: item.label,
        value: item.value,
        itemStyle: { color: item.color },
      })),
    }],
  }
})

const intSourceLabel = computed(() => {
  const source = telemetry.intTelemetry?.source
  return source ? String(source).replaceAll('_', ' ') : '未就绪'
})

const p4CounterLabel = computed(() => {
  const value = telemetry.telemetryLatestRaw?.p4_direct_counter?.packets
  return Number.isFinite(Number(value)) ? `${Number(value)} pkts` : '-'
})

async function refreshAll() {
  if (isRefreshing.value) return
  isRefreshing.value = true
  await Promise.allSettled([
    topology.loadTopology(),
    telemetry.loadTelemetry({ includeProbe: true }),
    telemetry.loadIntTelemetry(),
    intent.loadIntents(),
  ])
  isRefreshing.value = false
}

function metricTone(metric) {
  if (metric?.preview) return 'preview'
  const state = String(metric?.status || '').toLowerCase()
  if (['failed', 'error', 'critical', 'unavailable'].includes(state)) return 'danger'
  if (['warning', 'warn', 'degraded'].includes(state)) return 'warning'
  if (['ok', 'healthy', 'success'].includes(state)) return 'healthy'
  return 'neutral'
}

function metricAvailable(metric) {
  if (!metric || !isMetricValue(metric.rawValue) || metric.value === '-') return false
  const status = String(metric.status || '').toLowerCase()
  return !['idle', 'unavailable', 'error', 'failed'].includes(status)
}

function metricFor(id) {
  return (telemetry.metrics || []).find((metric) => metric.id === id)
}

function intentTone(item) {
  const state = String(item?.statusClass || item?.status || '').toLowerCase()
  if (['failed', 'error'].includes(state)) return 'danger'
  if (['success', 'completed'].includes(state)) return 'healthy'
  if (['clarification', 'pending_clarification'].includes(state)) return 'warning'
  return 'active'
}

function statusLabel(status) {
  return STATUS_LABELS[String(status || '').toLowerCase()] || status || '状态未知'
}

function intValue(key, unit) {
  const value = telemetry.intTelemetry?.kpi?.[key]
  if (!Number.isFinite(Number(value))) return '-'
  return `${Number(value)}${unit ? ` ${unit}` : ''}`
}

function finiteNumber(value) {
  return isMetricValue(value) ? Number(value) : null
}

function isMetricValue(value) {
  return value !== null
    && value !== undefined
    && value !== ''
    && Number.isFinite(Number(value))
}

function allMeasuredValuesAreZero(values) {
  const measured = values.filter(isMetricValue).map(Number)
  return measured.length > 0 && measured.every((value) => value === 0)
}

function scaleToPercent(value, minimum, maximum) {
  const number = finiteNumber(value)
  if (number == null || maximum <= minimum) return 0
  return Math.round(Math.min(100, Math.max(0, ((number - minimum) / (maximum - minimum)) * 100)))
}

function invertToPercent(value, minimum, maximum) {
  return 100 - scaleToPercent(value, minimum, maximum)
}

function userTextForIntent(record = {}, intentId = '') {
  const messages = Array.isArray(record.dialogMessages) ? record.dialogMessages : []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    const messageIntentId = message.intentId || message.intent_id || ''
    if (message.role === 'user' && messageIntentId === intentId && message.content) {
      return message.content
    }
  }
  return ''
}

function intentText(item = {}) {
  return [
    item.title,
    item.text,
    item.content,
    item.summary,
    item.name,
    item.snapshot?.text,
    item.snapshot?.summary,
    item.result?.message,
    item.result?.summary,
  ]
    .filter((value) => typeof value === 'string' && value.trim())
    .join(' ')
}

function extractIntentTarget(item = {}) {
  const text = intentText(item)
  const matches = text.match(/(?:[hs]_[a-z0-9_-]+|(?:\d{1,3}\.){3}\d{1,3}(?:\/\d{1,2})?)/gi) || []
  const targets = [...new Set(matches)]
  if (targets.length >= 2) return `${targets[0]} → ${targets[1]}`
  return targets[0] || '策略作用域'
}

function accessMode(item = {}) {
  const text = intentText(item).toLowerCase()
  if (/临时|移出.*转发/.test(text)) return '临时移出'
  if (/阻断|drop|关闭.*访问/.test(text)) return '访问阻断'
  return '完全隔离'
}

function displayIntentStatus(item = {}) {
  return statusLabel(item.statusClass || item.status)
}

function bandwidthMode(item = {}) {
  const text = intentText(item)
  if (/不低于|至少|保障|最低/.test(text)) return '保障下限'
  if (/不超过|最大|上限|限速|限制/.test(text)) return '控制上限'
  return '目标值'
}

const BANDWIDTH_KEYS = new Set([
  'bandwidth_target_mbps',
  'bandwidth_mbps',
  'rate_mbps',
  'max_rate_mbps',
  'min_bandwidth_mbps',
  'requested_bandwidth_mbps',
])

function findBandwidthValue(value, depth = 0) {
  if (!value || typeof value !== 'object' || depth > 5) return null
  for (const [key, nested] of Object.entries(value)) {
    if (BANDWIDTH_KEYS.has(key)) {
      const numeric = finiteNumber(nested)
      if (numeric != null) return numeric
    }
  }
  for (const nested of Object.values(value)) {
    if (!nested || typeof nested !== 'object') continue
    const found = findBandwidthValue(nested, depth + 1)
    if (found != null) return found
  }
  return null
}

function extractBandwidthMbps(item = {}) {
  const structuredValue = findBandwidthValue({
    snapshot: item.snapshot,
    result: item.result,
    finalPresentation: item.finalPresentation,
  })
  if (structuredValue != null) return structuredValue

  const match = intentText(item).match(/(\d+(?:\.\d+)?)\s*(gbps|mbps|kbps)/i)
  if (!match) return null

  let value = Number(match[1])
  const unit = match[2].toLowerCase()
  if (unit === 'gbps') value *= 1000
  if (unit === 'kbps') value /= 1000
  return Number.isFinite(value) ? value : null
}

function compactNumber(value) {
  const number = finiteNumber(value)
  if (number == null) return '-'
  return new Intl.NumberFormat('zh-CN', {
    notation: Math.abs(number) >= 10000 ? 'compact' : 'standard',
    maximumFractionDigits: 1,
  }).format(number)
}

function formatBytes(value) {
  const number = finiteNumber(value)
  if (number == null) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = Math.max(0, number)
  let index = 0
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024
    index += 1
  }
  const precision = index === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2
  return `${amount.toFixed(precision)} ${units[index]}`
}

function formatRate(value) {
  const number = finiteNumber(value)
  if (number == null) return '-'
  return number >= 1000
    ? `${compactNumber(number / 1000)} Gbps`
    : `${compactNumber(number)} Mbps`
}

function flowStatValue(key, unit = '') {
  const number = finiteNumber(telemetry.flowStats?.[key])
  if (number == null) return '-'
  return `${compactNumber(number)}${unit ? ` ${unit}` : ''}`
}

function formatClock(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '--:--:--'
  return date.toLocaleTimeString('zh-CN', { hour12: false })
}

function shortId(value) {
  const text = String(value || '')
  return text.length > 9 ? text.slice(-8) : text
}

function nodeTypeLabel(node) {
  if (node.type === 'host') return '主机'
  if (node.type === 'agent') return '智能体'
  return '交换机'
}

function chartTooltip(unit = '') {
  return {
    trigger: 'axis',
    backgroundColor: 'rgba(255, 255, 255, 0.98)',
    borderColor: '#bfd7e8',
    borderWidth: 1,
    extraCssText: 'box-shadow: 0 12px 30px rgba(40, 76, 106, 0.14); border-radius: 6px;',
    textStyle: { color: '#203047', fontSize: 11 },
    valueFormatter: (value) => value == null ? '-' : `${value}${unit ? ` ${unit}` : ''}`,
  }
}

function chartCategoryAxis(data) {
  return {
    type: 'category',
    boundaryGap: false,
    data,
    axisLine: { lineStyle: { color: '#d2e1ec' } },
    axisTick: { show: false },
    axisLabel: { color: '#718198', fontSize: 9, hideOverlap: true },
  }
}

function chartValueAxis(name, values = []) {
  const zeroOnly = allMeasuredValuesAreZero(values)
  return {
    type: 'value',
    name,
    nameTextStyle: { color: '#718198', fontSize: 9, padding: [0, 0, 0, -10] },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#718198', fontSize: 9 },
    splitLine: { lineStyle: { color: '#e2edf5', type: 'solid' } },
    ...(zeroOnly ? { min: 0, max: 1, interval: 1 } : {}),
  }
}

function handleQuickIntent(payload = {}) {
  if (network.isGlobalIntentLocked) return
  const text = typeof payload === 'string' ? payload : payload.text
  if (!text) return
  network.intentText = text
  if (payload.priority) network.priority = payload.priority
  router.push('/dashboard')
}

function goToIntent(intentItem) {
  if (!intentItem?.id) return
  intent.selectIntent(intentItem)
  router.push({ path: '/dashboard/workflow', query: { intent_id: intentItem.id } })
}

onMounted(() => {
  topology.loadTopology()
  topology.startTopologySSE()
  // The 1.5s loop is a lightweight ONOS counter read.  The telemetry store
  // schedules active Mininet/P4 probes separately at five seconds and never
  // starts an iperf run without a service-specific user action.
  telemetry.startTelemetryPolling({ includeProbe: false, includeIperf: false })
  Promise.all([intent.loadConversations(), intent.loadIntents()])
  intentPollTimer = setInterval(() => {
    Promise.all([intent.loadConversations(), intent.loadIntents()])
  }, INTENT_POLL_MS)
})

onUnmounted(() => {
  telemetry.stopTelemetryPolling()
  topology.stopTopologySSE()
  if (intentPollTimer) {
    clearInterval(intentPollTimer)
    intentPollTimer = null
  }
})
</script>

<style scoped>
.network-page {
  width: 100%;
  height: 100%;
  box-sizing: border-box;
  padding: 18px clamp(16px, 2vw, 30px) 34px;
  overflow-y: auto;
  color: #111827;
}

.control-ribbon,
.workspace-bar,
.monitor-head,
.panel-title,
.view-identity,
.ribbon-actions,
.fabric-facts,
.kpi-heading {
  display: flex;
  align-items: center;
}

.control-ribbon {
  min-height: 62px;
  box-sizing: border-box;
  justify-content: space-between;
  gap: 22px;
  padding: 10px 14px 10px 16px;
  border: 1px solid rgba(191, 204, 220, 0.72);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow: 0 12px 30px rgba(23, 36, 56, 0.06);
  backdrop-filter: blur(16px);
}

.view-identity {
  min-width: 210px;
  gap: 11px;
}

.identity-mark {
  width: 5px;
  height: 34px;
  border-radius: 2px;
  background: #31a9e8;
  box-shadow: 0 0 14px rgba(49, 169, 232, 0.42);
}

.view-identity p,
.monitor-head p,
.panel-title p {
  margin: 0;
  color: #738096;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.64rem;
  font-weight: 800;
  letter-spacing: 1.35px;
}

.view-identity h1 {
  margin: 3px 0 0;
  font-size: 1.08rem;
  line-height: 1;
}

.ribbon-facts {
  display: grid;
  grid-template-columns: repeat(3, minmax(110px, 1fr));
  min-width: min(560px, 48vw);
  margin: 0;
}

.ribbon-facts div {
  min-width: 0;
  padding: 0 18px;
  border-left: 1px solid #e3e8ef;
}

.ribbon-facts dt {
  color: #9aa5b4;
  font-size: 0.62rem;
  font-weight: 700;
}

.ribbon-facts dd {
  margin: 4px 0 0;
  color: #263246;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.69rem;
  font-weight: 800;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ribbon-actions {
  gap: 8px;
}

.sync-state {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: #197556;
  font-size: 0.7rem;
  font-weight: 800;
  white-space: nowrap;
}

.sync-state i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #2fc58d;
  box-shadow: 0 0 0 4px rgba(47, 197, 141, 0.12);
}

.sync-state.waiting {
  color: #7b8797;
}

.sync-state.waiting i {
  background: #9aa5b4;
  box-shadow: none;
}

.refresh-button {
  width: 34px;
  height: 34px;
  border: 1px solid #dbe2ea;
  border-radius: 6px;
  background: #f8fafc;
  color: #425168;
  cursor: pointer;
  font-size: 1.05rem;
}

.refresh-button:hover:not(:disabled) {
  border-color: #85c9ec;
  color: #1689c5;
}

.refresh-button:disabled {
  cursor: wait;
  opacity: 0.55;
}

.refresh-button .spinning {
  display: inline-block;
  animation: spin 0.8s linear infinite;
}

.topology-workspace {
  margin-top: 12px;
  overflow: hidden;
  border: 1px solid rgba(191, 204, 220, 0.72);
  border-radius: 8px;
  background: rgba(252, 253, 255, 0.88);
  box-shadow: 0 18px 42px rgba(23, 36, 56, 0.07);
}

.workspace-bar {
  min-height: 46px;
  box-sizing: border-box;
  justify-content: space-between;
  gap: 16px;
  padding: 0 14px;
  border-bottom: 1px solid #e3e8ef;
  background: rgba(247, 249, 252, 0.9);
}

.workspace-bar > div:first-child {
  display: flex;
  align-items: center;
  min-width: 0;
}

.workspace-dot {
  width: 7px;
  height: 7px;
  margin-right: 9px;
  border-radius: 50%;
  background: #35afe8;
}

.workspace-bar strong {
  color: #253146;
  font-size: 0.78rem;
}

.workspace-bar small {
  margin-left: 12px;
  color: #8d99a9;
  font-size: 0.66rem;
}

.fabric-facts {
  gap: 5px;
}

.fabric-facts span {
  min-height: 26px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 9px;
  border-left: 1px solid #dde4ec;
  color: #7a8798;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.64rem;
  font-weight: 700;
}

.fabric-facts b {
  color: #28364b;
}

.fabric-facts .fabric-health {
  color: #718095;
}

.fabric-facts .fabric-health.healthy { color: #2a9d74; }
.fabric-facts .fabric-health.warning { color: #d4912f; }
.fabric-facts .fabric-health.danger { color: #df616b; }

.topology-stage {
  position: relative;
  height: clamp(500px, 59vh, 700px);
  min-height: 500px;
  padding: 12px;
  box-sizing: border-box;
}

.topology-stage :deep(.topology-canvas) {
  border: 0;
  border-radius: 4px;
  background-color: #fbfcfe;
  box-shadow: inset 0 0 0 1px rgba(213, 222, 232, 0.72);
}

.topology-stage :deep(.topology-toolbar) {
  border-radius: 6px;
}

.topology-stage :deep(.map-status-strip) {
  border-radius: 4px;
}

.topology-empty {
  position: absolute;
  inset: 12px;
  z-index: 8;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: rgba(248, 250, 252, 0.9);
  text-align: center;
}

.loading-ring {
  width: 22px;
  height: 22px;
  margin-bottom: 14px;
  border: 2px solid #d7e1eb;
  border-top-color: #35afe8;
  border-radius: 50%;
  animation: spin 0.9s linear infinite;
}

.topology-empty strong {
  color: #2d394d;
  font-size: 0.82rem;
}

.topology-empty p {
  margin: 6px 0 0;
  color: #8b97a8;
  font-size: 0.7rem;
}

.monitor-board {
  margin-top: 14px;
  overflow: hidden;
  border: 1px solid rgba(160, 199, 225, 0.72);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.98), rgba(244, 250, 255, 0.96));
  color: #17243a;
  box-shadow:
    0 24px 56px rgba(55, 103, 139, 0.12),
    inset 0 1px 0 rgba(255, 255, 255, 0.96);
}

.monitor-head {
  min-height: 62px;
  box-sizing: border-box;
  justify-content: space-between;
  gap: 18px;
  padding: 0 18px;
  border-bottom: 1px solid #d7e6f1;
  background:
    linear-gradient(90deg, rgba(235, 247, 255, 0.82), rgba(255, 255, 255, 0.94) 45%, rgba(242, 246, 255, 0.88));
}

.monitor-head h2 {
  margin: 3px 0 0;
  color: #17243a;
  font-size: 1rem;
  letter-spacing: 0;
}

.monitor-meta {
  display: flex;
  align-items: center;
}

.monitor-meta span {
  padding: 0 12px;
  border-left: 1px solid #cdddea;
  color: #687a90;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.62rem;
  font-weight: 700;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-bottom: 1px solid #d7e6f1;
  background: rgba(255, 255, 255, 0.5);
}

.kpi-cell {
  position: relative;
  min-width: 0;
  padding: 16px 18px 15px;
  border-right: 1px solid #dbe8f1;
}

.kpi-cell:last-child {
  border-right: 0;
}

.kpi-cell::after {
  content: '';
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  height: 2px;
  background: #94a8b9;
}

.kpi-cell.healthy::after { background: #18a978; }
.kpi-cell.warning::after { background: #d8922b; }
.kpi-cell.danger::after { background: #dc5a68; }
.kpi-cell.preview::after {
  background: linear-gradient(90deg, #38a9e7, #7b8fe9);
}

.kpi-heading {
  justify-content: space-between;
  gap: 10px;
  color: #66788e;
  font-size: 0.68rem;
  font-weight: 750;
}

.kpi-heading i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #94a8b9;
}

.kpi-cell.healthy .kpi-heading i { background: #18a978; }
.kpi-cell.warning .kpi-heading i { background: #d8922b; }
.kpi-cell.danger .kpi-heading i { background: #dc5a68; }
.kpi-cell.preview .kpi-heading i {
  background: #5b9ee6;
  box-shadow: 0 0 0 4px rgba(91, 158, 230, 0.1);
}

.kpi-cell.preview > small {
  color: #557aa5;
}

.kpi-cell > strong {
  display: block;
  margin-top: 8px;
  color: #152238;
  font-size: 1.42rem;
  line-height: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kpi-cell > small {
  display: block;
  margin-top: 8px;
  color: #8292a6;
  font-size: 0.62rem;
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.monitor-section {
  min-width: 0;
  background: rgba(249, 252, 255, 0.72);
}

.traffic-section {
  min-height: 340px;
  border-bottom: 1px solid #d7e6f1;
}

.panel-title {
  min-height: 48px;
  box-sizing: border-box;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 14px;
  border-bottom: 1px solid #dfebf3;
  background: rgba(255, 255, 255, 0.5);
}

.panel-title h3 {
  margin: 3px 0 0;
  color: #25334a;
  font-size: 0.78rem;
  letter-spacing: 0;
}

.panel-title > span {
  color: #75869b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.6rem;
  font-weight: 700;
  text-align: right;
}

.wide-chart {
  width: 100%;
  height: 310px;
}

.telemetry-source-note {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 14px;
  padding: 0 16px 12px;
  color: #75869b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.61rem;
  font-weight: 700;
}

.telemetry-source-note small {
  flex-basis: 100%;
  color: #8292a6;
  font-size: inherit;
  font-weight: 600;
}

.analytics-grid {
  display: grid;
  grid-template-columns: minmax(560px, 1.55fr) minmax(300px, 0.75fr);
  gap: 12px;
  padding: 12px;
  border-bottom: 1px solid #d7e6f1;
  background: rgba(240, 247, 252, 0.48);
}

.analytics-grid > .monitor-section {
  min-height: 320px;
  overflow: hidden;
  border: 1px solid #d7e6f1;
  border-radius: 6px;
  background: rgba(251, 253, 255, 0.84);
}

.analytics-grid > .node-ledger {
  grid-column: 1 / -1;
  min-height: 0;
}

.compact-chart {
  width: 100%;
  height: 270px;
}

.health-content {
  height: 270px;
  display: grid;
  grid-template-columns: minmax(150px, 1fr) minmax(104px, 0.62fr);
  align-items: center;
  gap: 10px;
  padding: 0 16px 0 12px;
}

.health-chart {
  width: 100%;
  height: 232px;
}

.health-legend {
  padding-right: 8px;
}

.health-legend > div {
  display: grid;
  grid-template-columns: 7px minmax(0, 1fr) auto;
  align-items: center;
  gap: 7px;
  min-height: 26px;
  border-bottom: 1px solid #e0ebf3;
  color: #6c7d92;
  font-size: 0.66rem;
}

.health-legend i {
  width: 7px;
  height: 7px;
  border-radius: 2px;
}

.health-legend b {
  color: #26364d;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.ledger-table {
  padding: 8px 12px 12px;
}

.ledger-head,
.ledger-row {
  display: grid;
  grid-template-columns: minmax(90px, 1.3fr) 0.75fr 0.5fr 0.7fr;
  align-items: center;
  gap: 10px;
  min-height: 31px;
  box-sizing: border-box;
}

.ledger-head {
  color: #8292a6;
  font-size: 0.58rem;
  font-weight: 800;
}

.ledger-row {
  width: 100%;
  padding: 0;
  border: 0;
  border-top: 1px solid #e0ebf3;
  background: transparent;
  color: #6d7f94;
  cursor: pointer;
  font-size: 0.63rem;
  text-align: left;
}

.ledger-row:hover {
  background: rgba(224, 242, 252, 0.72);
}

.ledger-row strong {
  color: #26364d;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ledger-row em {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #708197;
  font-style: normal;
}

.ledger-row em i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #95a7b9;
}

.ledger-row em.healthy i { background: #18a978; }
.ledger-row em.warning i { background: #d8922b; }
.ledger-row em.danger i { background: #dc5a68; }

.intent-impact {
  border-bottom: 1px solid #d7e6f1;
  background:
    linear-gradient(180deg, rgba(247, 252, 255, 0.92), rgba(255, 255, 255, 0.78));
}

.impact-head {
  min-height: 56px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 9px 16px;
  border-bottom: 1px solid #d7e6f1;
  background:
    linear-gradient(90deg, rgba(232, 247, 255, 0.8), rgba(255, 255, 255, 0.82) 52%, rgba(241, 247, 255, 0.78));
}

.impact-head p {
  margin: 0;
  color: #6d8198;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.58rem;
  font-weight: 800;
  letter-spacing: 0;
}

.impact-head h3 {
  margin: 3px 0 0;
  color: #1b2a40;
  font-size: 0.86rem;
}

.impact-head > span {
  color: #70839a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.62rem;
  font-weight: 750;
}

.impact-grid {
  display: grid;
  grid-template-columns: minmax(500px, 1.2fr) minmax(340px, 0.8fr);
  gap: 12px;
  padding: 12px;
  background: rgba(240, 247, 252, 0.48);
}

.impact-panel {
  min-height: 360px;
  overflow: hidden;
  border: 1px solid #d7e6f1;
  border-radius: 6px;
  background: rgba(251, 253, 255, 0.84);
}

.qos-impact {
  grid-column: 1 / -1;
  min-height: 0;
  display: block;
}

.impact-chart {
  width: 100%;
  height: 238px;
}

.impact-empty {
  height: 238px;
}

.service-envelope-body {
  display: grid;
  grid-template-columns: minmax(300px, 0.82fr) minmax(0, 1.48fr);
  min-height: 278px;
  background:
    radial-gradient(circle at 18% 45%, rgba(71, 183, 237, 0.12), transparent 33%),
    linear-gradient(118deg, rgba(246, 252, 255, 0.88), rgba(255, 255, 255, 0.42));
}

.service-radar-stage {
  position: relative;
  min-height: 278px;
  border-right: 1px solid #dce9f2;
}

.service-radar {
  width: 100%;
  height: 278px;
}

.quality-index {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 66px;
  height: 66px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(107, 183, 227, 0.42);
  border-radius: 50%;
  background: rgba(253, 255, 255, 0.88);
  box-shadow: 0 10px 24px rgba(45, 124, 170, 0.12), inset 0 1px 0 #fff;
  transform: translate(-50%, -50%);
  pointer-events: none;
}

.quality-index strong {
  color: #1d4263;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 1.28rem;
  line-height: 1;
  letter-spacing: 0;
}

.quality-index span {
  margin-top: 5px;
  color: #7890a8;
  font-size: 0.55rem;
  font-weight: 800;
}

.service-metric-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  align-content: center;
  gap: 10px;
  padding: 18px;
}

.service-metric {
  min-width: 0;
  min-height: 94px;
  padding: 12px;
  border: 1px solid rgba(205, 225, 238, 0.9);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.73);
  box-shadow: 0 8px 18px rgba(54, 112, 146, 0.045);
}

.service-metric > span {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #6f8399;
  font-size: 0.62rem;
  font-weight: 780;
}

.service-metric > span i {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #93a7ba;
}

.service-metric > span i.healthy { background: #18a978; }
.service-metric > span i.warning { background: #d8922b; }
.service-metric > span i.danger { background: #dc5a68; }
.service-metric > span i.preview { background: #4aaee7; box-shadow: 0 0 0 4px rgba(74, 174, 231, 0.12); }

.service-metric > strong {
  display: block;
  margin-top: 11px;
  overflow: hidden;
  color: #1c324a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 1.02rem;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.service-metric > small {
  display: block;
  margin-top: 8px;
  overflow: hidden;
  color: #8295a8;
  font-size: 0.55rem;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.policy-ribbon {
  display: grid;
  grid-template-columns: 172px minmax(0, 1fr);
  border-top: 1px solid #dce9f2;
  background: linear-gradient(90deg, rgba(235, 248, 255, 0.72), rgba(255, 255, 255, 0.72));
}

.policy-ribbon-label {
  min-height: 84px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 18px;
  border-right: 1px solid #dce9f2;
}

.policy-ribbon-label span {
  color: #7990a6;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.56rem;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.policy-ribbon-label strong {
  margin-top: 7px;
  color: #253b55;
  font-size: 0.75rem;
}

.policy-ribbon-list {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
}

.policy-ribbon-list article {
  min-width: 0;
  display: grid;
  grid-template-columns: 6px minmax(0, 1fr);
  column-gap: 7px;
  padding: 6px 4px;
}

.policy-ribbon-list i {
  width: 6px;
  height: 6px;
  margin-top: 4px;
  border-radius: 50%;
  background: #4aaee7;
}

.policy-ribbon-list i.healthy { background: #18a978; }
.policy-ribbon-list i.warning { background: #d8922b; }
.policy-ribbon-list i.danger { background: #dc5a68; }

.policy-ribbon-list span,
.policy-ribbon-list strong,
.policy-ribbon-list em {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.policy-ribbon-list span {
  color: #4e6279;
  font-size: 0.61rem;
  font-weight: 750;
}

.policy-ribbon-list strong {
  grid-column: 2;
  margin-top: 4px;
  color: #253b55;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.58rem;
}

.policy-ribbon-list em {
  grid-column: 2;
  margin-top: 4px;
  color: #527289;
  font-size: 0.54rem;
  font-style: normal;
  font-weight: 750;
}

.policy-ribbon-list em.healthy { color: #168a63; }
.policy-ribbon-list em.warning { color: #b9751d; }
.policy-ribbon-list em.danger { color: #c24756; }

.policy-ribbon-empty {
  display: flex;
  align-items: center;
  padding: 0 18px;
  color: #7c8fa3;
  font-size: 0.64rem;
  line-height: 1.6;
}

.flow-facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0;
  padding: 0 12px 12px;
}

.flow-facts > div {
  min-width: 0;
  padding: 9px 10px 3px;
  border-top: 1px solid #dce9f2;
  border-right: 1px solid #e2edf4;
}

.flow-facts > div:last-child {
  border-right: 0;
}

.flow-facts dt {
  color: #7e8fa3;
  font-size: 0.57rem;
  font-weight: 700;
}

.flow-facts dd {
  margin: 5px 0 0;
  color: #26364d;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.7rem;
  font-weight: 800;
  white-space: nowrap;
}

.impact-table {
  padding: 8px 14px 14px;
}

.impact-table-head,
.impact-table-row {
  display: grid;
  grid-template-columns: minmax(96px, 1.2fr) minmax(78px, 0.85fr) minmax(76px, 0.7fr);
  align-items: center;
  gap: 10px;
  min-height: 42px;
}

.impact-table-head {
  min-height: 31px;
  color: #8292a6;
  font-size: 0.56rem;
  font-weight: 800;
}

.impact-table-row {
  border-top: 1px solid #dfeaf2;
  color: #66798f;
  font-size: 0.62rem;
}

.impact-table-row strong {
  color: #26364d;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.impact-table-row em {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #61748a;
  font-style: normal;
  font-weight: 750;
}

.impact-table-row em i {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #2f9ed6;
}

.impact-table-row em.healthy i { background: #18a978; }
.impact-table-row em.warning i { background: #d8922b; }
.impact-table-row em.danger i { background: #dc5a68; }

.operations-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.operations-grid > .monitor-section {
  min-height: 220px;
  border-right: 1px solid #d7e6f1;
}

.operations-grid > .monitor-section:last-child {
  border-right: 0;
}

.telemetry-channel-grid,
.observer-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0;
  margin: 0 12px 12px;
  border: 1px solid #dfeaf2;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.56);
}

.telemetry-channel-grid > div,
.observer-grid > div {
  min-width: 0;
  min-height: 48px;
  display: grid;
  grid-template-columns: 7px minmax(0, 1fr);
  align-content: center;
  column-gap: 8px;
  padding: 8px 10px;
  border-right: 1px solid #e0ebf3;
  border-bottom: 1px solid #e0ebf3;
}

.telemetry-channel-grid > div:nth-child(2n),
.observer-grid > div:nth-child(2n) {
  border-right: 0;
}

.telemetry-channel-grid > div:nth-last-child(-n + 2),
.observer-grid > div:nth-last-child(-n + 2) {
  border-bottom: 0;
}

.telemetry-channel-grid i,
.observer-grid i {
  width: 6px;
  height: 6px;
  margin-top: 5px;
  border-radius: 50%;
  background: #4aaee7;
}

.telemetry-channel-grid i.healthy,
.observer-grid i.healthy { background: #18a978; }
.telemetry-channel-grid i.warning,
.observer-grid i.warning { background: #d8922b; }
.telemetry-channel-grid i.danger,
.observer-grid i.danger { background: #dc5a68; }

.telemetry-channel-grid span,
.observer-grid strong {
  overflow: hidden;
  color: #52667d;
  font-size: 0.59rem;
  font-weight: 780;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.telemetry-channel-grid small,
.observer-grid small {
  grid-column: 2;
  display: block;
  margin-top: 4px;
  overflow: hidden;
  color: #8495a9;
  font-size: 0.55rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.observer-grid > div {
  grid-template-columns: 7px minmax(0, 1fr) auto;
}

.observer-grid small { grid-column: 2 / 4; }

.observer-grid em {
  align-self: start;
  color: #6c8196;
  font-size: 0.54rem;
  font-style: normal;
  font-weight: 750;
}

.intent-outcome-strip {
  min-height: 86px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px 2px;
  border-bottom: 1px solid #e0ebf3;
}

.intent-outcome-chart {
  width: 86px;
  height: 78px;
  flex: 0 0 auto;
}

.intent-outcome-copy {
  display: flex;
  flex-direction: column;
}

.intent-outcome-copy strong {
  color: #213a55;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 1.28rem;
  line-height: 1;
}

.intent-outcome-copy span {
  margin-top: 5px;
  color: #71859a;
  font-size: 0.58rem;
  font-weight: 740;
}

.intent-empty-state {
  min-height: 118px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 0 18px;
  text-align: center;
}

.intent-empty-state strong {
  color: #4d6279;
  font-size: 0.68rem;
}

.intent-empty-state small {
  margin-top: 6px;
  color: #8b9aad;
  font-size: 0.56rem;
}

.probe-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0;
  margin: 0;
  padding: 12px;
}

.probe-grid div {
  min-width: 0;
  padding: 12px;
  border-right: 1px solid #e0ebf3;
  border-bottom: 1px solid #e0ebf3;
}

.probe-grid div:nth-child(2n) { border-right: 0; }
.probe-grid div:nth-last-child(-n + 2) { border-bottom: 0; }

.probe-grid dt {
  color: #75869b;
  font-size: 0.61rem;
  font-weight: 700;
}

.probe-grid dd {
  margin: 6px 0 0;
  color: #26364d;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.85rem;
  font-weight: 800;
}

.operation-list {
  padding: 7px 12px 12px;
}

.operation-list > div,
.operation-list > button {
  width: 100%;
  min-height: 37px;
  display: grid;
  grid-template-columns: 7px minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  padding: 0;
  border: 0;
  border-bottom: 1px solid #e0ebf3;
  background: transparent;
  color: inherit;
  text-align: left;
}

.operation-list > button {
  cursor: pointer;
}

.operation-list > button:hover {
  background: rgba(224, 242, 252, 0.72);
}

.operation-list > div > i,
.operation-list > button > i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #2f9ed6;
}

.operation-list i.healthy { background: #18a978; }
.operation-list i.warning { background: #d8922b; }
.operation-list i.danger { background: #dc5a68; }
.operation-list i.active { background: #2f9ed6; }

.operation-list span {
  min-width: 0;
}

.operation-list strong,
.operation-list small {
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.operation-list strong {
  color: #26364d;
  font-size: 0.65rem;
}

.operation-list small {
  margin-top: 2px;
  color: #75869b;
  font-size: 0.57rem;
}

.operation-list time {
  color: #8292a6;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.56rem;
}

.board-empty {
  height: 280px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #8292a6;
}

.board-empty.compact {
  height: 190px;
}

.board-empty > span {
  width: 58%;
  height: 52px;
  margin-bottom: 15px;
  border-bottom: 1px solid #d4e4ef;
  opacity: 0.8;
}

.board-empty strong {
  color: #5f7188;
  font-size: 0.72rem;
}

.board-empty small {
  margin-top: 5px;
  color: #8999aa;
  font-size: 0.61rem;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 1280px) {
  .ribbon-facts {
    min-width: 390px;
  }

  .analytics-grid {
    grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.65fr);
  }

  .impact-grid {
    grid-template-columns: minmax(0, 1.15fr) minmax(320px, 0.85fr);
  }

  .service-envelope-body {
    grid-template-columns: minmax(270px, 0.74fr) minmax(0, 1.26fr);
  }

  .policy-ribbon {
    grid-template-columns: 150px minmax(0, 1fr);
  }

  .policy-ribbon-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .node-ledger {
    grid-column: 1 / -1;
  }
}

@media (max-width: 980px) {
  .control-ribbon {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .ribbon-facts {
    order: 3;
    width: 100%;
    min-width: 0;
    border-top: 1px solid #e3e8ef;
    padding-top: 10px;
  }

  .kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .kpi-cell:nth-child(2) {
    border-right: 0;
  }

  .kpi-cell:nth-child(-n + 2) {
    border-bottom: 1px solid #d7e6f1;
  }

  .analytics-grid,
  .impact-grid,
  .operations-grid {
    grid-template-columns: 1fr;
  }

  .operations-grid > .monitor-section {
    border-right: 0;
  }

  .qos-impact {
    grid-column: auto;
    display: block;
  }

  .service-envelope-body {
    grid-template-columns: 1fr;
  }

  .service-radar-stage {
    border-right: 0;
    border-bottom: 1px solid #dce9f2;
  }

  .policy-ribbon {
    grid-template-columns: 1fr;
  }

  .policy-ribbon-label {
    min-height: 62px;
    border-right: 0;
    border-bottom: 1px solid #dce9f2;
  }
}

@media (max-width: 640px) {
  .network-page {
    padding: 12px 10px 24px;
  }

  .workspace-bar {
    align-items: flex-start;
    flex-direction: column;
    padding: 10px 12px;
  }

  .workspace-bar small {
    display: none;
  }

  .fabric-facts {
    width: 100%;
  }

  .fabric-facts span {
    flex: 1;
    justify-content: center;
  }

  .topology-stage {
    height: 520px;
    min-height: 520px;
    padding: 8px;
  }

  .ribbon-facts {
    grid-template-columns: 1fr;
  }

  .ribbon-facts div {
    display: flex;
    justify-content: space-between;
    padding: 6px 0;
    border-left: 0;
    border-bottom: 1px solid #edf0f4;
  }

  .monitor-head,
  .monitor-meta {
    align-items: flex-start;
    flex-direction: column;
  }

  .monitor-head {
    padding: 12px 14px;
  }

  .monitor-meta {
    gap: 4px;
  }

  .monitor-meta span {
    padding: 0;
    border-left: 0;
  }

  .kpi-row {
    grid-template-columns: 1fr;
  }

  .service-metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 12px;
  }

  .policy-ribbon-list {
    grid-template-columns: 1fr;
  }

  .telemetry-channel-grid,
  .observer-grid {
    grid-template-columns: 1fr;
  }

  .telemetry-channel-grid > div,
  .observer-grid > div,
  .telemetry-channel-grid > div:nth-child(2n),
  .observer-grid > div:nth-child(2n) {
    border-right: 0;
    border-bottom: 1px solid #e0ebf3;
  }

  .telemetry-channel-grid > div:last-child,
  .observer-grid > div:last-child {
    border-bottom: 0;
  }

  .kpi-cell {
    border-right: 0;
    border-bottom: 1px solid #d7e6f1;
  }

  .health-content {
    grid-template-columns: 1fr;
    height: auto;
  }

  .health-legend {
    padding: 0 12px 12px;
  }

  .impact-head {
    align-items: flex-start;
    flex-direction: column;
    padding: 11px 14px;
  }

  .flow-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .flow-facts > div:nth-child(2) {
    border-right: 0;
  }

  .impact-table-head,
  .impact-table-row {
    grid-template-columns: minmax(88px, 1.1fr) minmax(72px, 0.8fr) minmax(70px, 0.7fr);
    gap: 7px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .loading-ring,
  .refresh-button .spinning {
    animation: none;
  }
}
</style>
