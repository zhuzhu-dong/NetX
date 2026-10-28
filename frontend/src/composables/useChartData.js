/**
 * useChartData.js — Store 数据 → ECharts option 纯函数
 *
 * 每个函数返回完整的 ECharts option 对象，可直接传给图表组件。
 * 返回 null 表示数据不足，调用方应显示占位状态。
 */
import { CHART_PALETTE, STATE_COLOR_MAP, baseEChartTheme, gaugeBaseOption, donutBaseOption, radarBaseOption, lineAreaBaseOption, timelineBaseOption, heatmapBaseOption } from '../styles/echarts-theme'

/* ── helpers ── */

function mergeDeep(target, ...sources) {
  // shallow merge for ECharts option objects (no nested arrays to worry about)
  return Object.assign({}, target, ...sources)
}

/* ── KPI 雷达图统一缩放：将异构量纲映射到 0-100 ── */

/**
 * 将 before/after KPI 原始值缩放到雷达图可比的 0-100 区间。
 * - throughput_mbps:  /100,  cap 100  (10 Gbps → 100)
 * - latency_ms:       原值,  cap 100  (100ms → 100)
 * - packet_loss_rate: 小数→%, cap 100  (10% → 100)
 * - queue_depth:      /5,    cap 100  (500 pkts → 100)
 */
/**
 * 雷达图数据预处理 —— 保持原始 KPI 值不变，不做归一化。
 * 每维度独立设置 max（通过 RADAR_KPI_DIMENSIONS），
 * 图表上显示的是真实物理量，不是抽象分数。
 */
export function scaleKpiForRadar(kpi) {
  if (!kpi || typeof kpi !== 'object') return {}
  // 直接透传原始值，不做变换。max 由 RADAR_KPI_DIMENSIONS 控制。
  const s = {}
  if (typeof kpi.throughput_mbps === 'number') {
    s.throughput_mbps = Math.round(kpi.throughput_mbps)
  }
  if (typeof kpi.latency_ms === 'number') {
    s.latency_ms = Math.round(kpi.latency_ms * 10) / 10
  }
  if (typeof kpi.packet_loss_rate === 'number') {
    const pct = kpi.packet_loss_rate <= 1 ? kpi.packet_loss_rate * 100 : kpi.packet_loss_rate
    s.packet_loss_rate = Math.round(pct * 10) / 10
  }
  if (typeof kpi.queue_depth === 'number') {
    s.queue_depth = Math.round(kpi.queue_depth)
  }
  return s
}

/** 雷达图维度定义——每维度独立 max，显示真实物理量。
 *  时延/jitter/丢包/队列：越低越好（多边形收缩=改善）
 *  吞吐：越高越好（多边形扩张=改善）
 *  两者方向相反，但配合边框数值标签可直观理解。 */
export const RADAR_KPI_DIMENSIONS = [
  { key: 'throughput_mbps',   name: '吞吐(Mbps)', max: 120 },
  { key: 'latency_ms',        name: '时延(ms)',   max: 200 },
  { key: 'packet_loss_rate',  name: '丢包(%)',    max: 10 },
  { key: 'queue_depth',       name: '队列(pkts)', max: 30 },
]

/* ── Gauge: 单指标仪表盘 ── */

export function toGaugeOption(metric) {
  if (!metric || metric.value == null || metric.value === '-') return null

  let raw = typeof metric.rawValue === 'number' ? metric.rawValue : parseFloat(metric.value)
  if (isNaN(raw)) return null

  // 根据 metric 类型决定 max 值
  let max = 100
  const label = (metric.label || '').toLowerCase()
  if (label.includes('时延') || label.includes('latency')) max = 200
  if (label.includes('吞吐') || label.includes('throughput')) max = 2000
  // 置信度/百分比类：值在 0~1 区间时放大到 0~100 显示
  if (label.includes('置信') || label.includes('confidence') || label.includes('评分') || label.includes('score')) {
    if (raw <= 1 && raw > 0) raw = Math.round(raw * 100)
  }

  const option = {
    ...baseEChartTheme,
    series: [{
      ...gaugeBaseOption.series[0],
      max,
      data: [{ value: raw, name: metric.label }],
      title: { ...gaugeBaseOption.series[0].title, text: metric.label },
      detail: {
        ...gaugeBaseOption.series[0].detail,
        formatter: label.includes('丢包') || label.includes('loss') || label.includes('置信') || label.includes('confidence') ? '{value}%' : '{value}',
      },
    }],
  }
  return option
}

/* ── Donut: 分布环形图 ── */

export function toDonutOption(stats, colorMap = STATE_COLOR_MAP) {
  // stats: [{ name: string, value: number }, ...]
  if (!stats || !stats.length) return null
  const total = stats.reduce((s, i) => s + i.value, 0)
  if (total === 0) return null

  const data = stats.map((s) => ({
    name: s.name,
    value: s.value,
    itemStyle: { color: colorMap[s.key] || CHART_PALETTE[stats.indexOf(s) % CHART_PALETTE.length] },
  }))

  return {
    ...baseEChartTheme,
    ...donutBaseOption,
    series: [{
      ...donutBaseOption.series[0],
      data,
    }],
  }
}

/* ── Radar: before/after 多维对比 ── */

export function toRadarOption(before, after, dimensions) {
  // before: { dim1: val, dim2: val, ... }
  // after:  { dim1: val, dim2: val, ... }
  // dimensions: [{ name, max }, ...]
  if (!before || !after || !dimensions || !dimensions.length) return null

  const indicator = dimensions.map((d) => ({ name: d.name, max: d.max || 100 }))
  const beforeValues = dimensions.map((d) => before[d.key] ?? 0)
  const afterValues = dimensions.map((d) => after[d.key] ?? 0)

  return {
    ...baseEChartTheme,
    radar: {
      ...radarBaseOption.radar,
      indicator,
      center: ['50%', '43%'],
      radius: '54%',
      splitNumber: 4,
      nameGap: 12,
      axisName: {
        color: '#64748b',
        fontSize: 10,
        fontWeight: 700,
        lineHeight: 14,
      },
    },
    legend: {
      data: ['变更前', '变更后'],
      left: 'center',
      bottom: 4,
      itemWidth: 14,
      itemHeight: 7,
      itemGap: 22,
      textStyle: { color: '#64748b', fontSize: 10, fontWeight: 700 },
    },
    series: [
      {
        ...radarBaseOption.series[0],
        name: '变更前',
        data: [{ value: beforeValues, name: '变更前' }],
        lineStyle: { color: CHART_PALETTE[7] }, // slate
        areaStyle: { color: 'rgba(148,163,184,0.12)' },
        itemStyle: { color: CHART_PALETTE[7] },
      },
      {
        ...radarBaseOption.series[0],
        name: '变更后',
        data: [{ value: afterValues, name: '变更后' }],
        lineStyle: { color: CHART_PALETTE[2] }, // green
        areaStyle: { color: 'rgba(16,185,129,0.12)' },
        itemStyle: { color: CHART_PALETTE[2] },
      },
    ],
  }
}

/* ── LineArea: 趋势面积图 ── */

export function toLineAreaOption(points, label) {
  // points: [{ time: string, value: number }, ...]
  if (!points || !points.length) return null

  const xData = points.map((p) => p.time)
  const yData = points.map((p) => p.value)

  return {
    ...baseEChartTheme,
    ...lineAreaBaseOption,
    xAxis: { ...lineAreaBaseOption.xAxis, data: xData },
    yAxis: { ...lineAreaBaseOption.yAxis },
    series: [{
      ...lineAreaBaseOption.series[0],
      name: label || '趋势',
      data: yData,
      itemStyle: { color: CHART_PALETTE[0] },
      lineStyle: { color: CHART_PALETTE[0] },
      areaStyle: {
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(56,189,248,0.32)' },
            { offset: 1, color: 'rgba(56,189,248,0.02)' },
          ],
        },
      },
    }],
  }
}

/* ── Timeline: 事件时间线 ── */

export function toTimelineOption(events) {
  // events: [{ time: Date|string, label: string, agent?: string, color?: string }, ...]
  if (!events || !events.length) return null

  const data = events.map((e) => ({
    value: [e.time instanceof Date ? e.time : new Date(e.time), e.agent || e.label],
    name: e.label,
    itemStyle: { color: e.color || CHART_PALETTE[0] },
  }))

  return {
    ...baseEChartTheme,
    ...timelineBaseOption,
    series: [{
      ...timelineBaseOption.series[0],
      data,
    }],
  }
}

/* ── Heatmap: 冲突热力矩阵 ── */

export function toHeatmapOption(rows, cols, data) {
  // rows: string[] — Y axis labels
  // cols: string[] — X axis labels
  // data: [[rowIdx, colIdx, value], ...]
  if (!rows || !cols || !data || !data.length) return null

  const maxVal = Math.max(...data.map((d) => d[2]), 1)

  return {
    ...baseEChartTheme,
    ...heatmapBaseOption,
    xAxis: { ...heatmapBaseOption.xAxis, data: cols },
    yAxis: { ...heatmapBaseOption.yAxis, data: rows },
    visualMap: {
      ...heatmapBaseOption.visualMap,
      max: maxVal,
    },
    series: [{
      ...heatmapBaseOption.series[0],
      data,
    }],
  }
}
