/**
 * ECharts 主题 — 与 NetPulse glassmorphism 风格一致
 *
 * 用法：在图表 option 中通过 `...baseEChartTheme` 展开基础配置，
 *       再按需覆盖具体 series 数据。
 */

/* 读取 CSS 变量值的辅助函数 */
function cssVar(name) {
  if (typeof window === 'undefined') return ''
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** 色板 — 对应 design-tokens.css 中的 --chart-* 变量 */
export const CHART_PALETTE = [
  '#38bdf8', // cyan
  '#6366f1', // indigo
  '#10b981', // green
  '#f59e0b', // orange
  '#8b5cf6', // purple
  '#ef4444', // red
  '#0ea5e9', // sky blue
  '#94a3b8', // slate
]

/** 状态色映射 */
export const STATE_COLOR_MAP = {
  normal: '#10b981',
  degraded: '#f59e0b',
  failed: '#ef4444',
  attack: '#8b5cf6',
}

/** 基础 ECharts option — 所有图表共用 */
export const baseEChartTheme = {
  textStyle: {
    color: '#64748b',
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  },
  tooltip: {
    backgroundColor: 'rgba(255, 255, 255, 0.88)',
    borderColor: 'rgba(226, 232, 240, 0.88)',
    borderWidth: 1,
    borderRadius: 12,
    textStyle: { color: '#0f172a', fontSize: 12 },
    extraCssText: 'backdrop-filter: blur(14px); box-shadow: 0 12px 32px rgba(15,23,42,0.10);',
  },
  animation: true,
  animationDuration: 800,
  animationEasing: 'cubicOut',
  animationDurationUpdate: 400,
  animationEasingUpdate: 'cubicInOut',
}

/** Gauge 仪表盘基础配置 */
export const gaugeBaseOption = {
  series: [{
    type: 'gauge',
    startAngle: 210,
    endAngle: -30,
    center: ['50%', '58%'],
    radius: '88%',
    min: 0,
    max: 100,
    splitNumber: 5,
    axisLine: {
      show: true,
      lineStyle: {
        width: 14,
        color: [
          [0.3, '#ef4444'],
          [0.6, '#f59e0b'],
          [0.85, '#10b981'],
          [1, '#38bdf8'],
        ],
      },
    },
    pointer: {
      icon: 'path://M12.8,0.7l12,40.1H0.7L12.8,0.7z',
      length: '62%',
      width: 6,
      itemStyle: { color: '#0f172a' },
    },
    axisTick: {
      length: 8,
      lineStyle: { color: 'auto', width: 1.5 },
    },
    splitLine: {
      length: 18,
      lineStyle: { color: 'auto', width: 3 },
    },
    axisLabel: {
      color: '#94a3b8',
      fontSize: 10,
      distance: 16,
    },
    title: {
      offsetCenter: [0, '78%'],
      fontSize: 13,
      color: '#475569',
    },
    detail: {
      fontSize: 24,
      fontWeight: 'bold',
      color: '#0f172a',
      offsetCenter: [0, '48%'],
      valueAnimation: true,
      formatter: '{value}',
    },
    animation: true,
    animationDuration: 1000,
  }],
}

/** Donut 环形图基础配置 */
export const donutBaseOption = {
  tooltip: {
    trigger: 'item',
    formatter: '{b}: {c} ({d}%)',
  },
  series: [{
    type: 'pie',
    radius: ['52%', '78%'],
    center: ['50%', '50%'],
    avoidLabelOverlap: false,
    itemStyle: {
      borderRadius: 6,
      borderColor: 'rgba(255,255,255,0.8)',
      borderWidth: 3,
    },
    label: {
      show: true,
      position: 'outside',
      formatter: '{b}\n{d}%',
      color: '#64748b',
      fontSize: 11,
    },
    emphasis: {
      label: { fontSize: 14, fontWeight: 'bold' },
      scaleSize: 8,
    },
    animationType: 'scale',
    animationEasing: 'elasticOut',
  }],
}

/** Radar 雷达图基础配置 */
export const radarBaseOption = {
  radar: {
    center: ['50%', '52%'],
    radius: '65%',
    splitNumber: 4,
    axisName: {
      color: '#64748b',
      fontSize: 11,
      borderRadius: 3,
    },
    splitArea: {
      areaStyle: {
        color: ['rgba(255,255,255,0.3)', 'rgba(248,250,252,0.3)'],
      },
    },
    axisLine: { lineStyle: { color: 'rgba(203,213,225,0.6)' } },
    splitLine: { lineStyle: { color: 'rgba(203,213,225,0.6)' } },
  },
  series: [{
    type: 'radar',
    symbol: 'circle',
    symbolSize: 5,
    lineStyle: { width: 2 },
    areaStyle: { opacity: 0.15 },
    animation: true,
    animationDuration: 800,
  }],
}

/** Line-Area 趋势图基础配置 */
export const lineAreaBaseOption = {
  grid: {
    left: 10,
    right: 18,
    top: 24,
    bottom: 10,
    containLabel: true,
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    axisLine: { lineStyle: { color: 'rgba(203,213,225,0.6)' } },
    axisTick: { show: false },
    axisLabel: { color: '#94a3b8', fontSize: 10 },
  },
  yAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: 'rgba(226,232,240,0.5)' } },
    axisLabel: { color: '#94a3b8', fontSize: 10 },
  },
  series: [{
    type: 'line',
    smooth: 0.4,
    showSymbol: false,
    lineStyle: { width: 2.5 },
    areaStyle: {
      opacity: 0.18,
    },
    emphasis: {
      focus: 'series',
      blurScope: 'coordinateSystem',
    },
  }],
}

/** Timeline 时间线图基础配置 */
export const timelineBaseOption = {
  grid: {
    left: 10,
    right: 18,
    top: 14,
    bottom: 10,
    containLabel: true,
  },
  xAxis: {
    type: 'time',
    axisLine: { lineStyle: { color: 'rgba(203,213,225,0.6)' } },
    axisTick: { show: false },
    axisLabel: { color: '#94a3b8', fontSize: 10 },
    splitLine: { show: false },
  },
  yAxis: {
    type: 'category',
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#64748b', fontSize: 10, fontWeight: 600 },
    splitLine: { show: false },
  },
  series: [{
    type: 'scatter',
    symbolSize: 14,
    itemStyle: {
      borderRadius: 7,
      shadowBlur: 6,
      shadowColor: 'rgba(15,23,42,0.08)',
    },
    emphasis: {
      scale: 1.4,
    },
  }],
}

/** Heatmap 热力图基础配置 */
export const heatmapBaseOption = {
  grid: {
    left: 8,
    right: 18,
    top: 8,
    bottom: 8,
  },
  xAxis: {
    type: 'category',
    splitArea: { show: true },
    axisLabel: { color: '#64748b', fontSize: 10, fontWeight: 600 },
    axisLine: { show: false },
    axisTick: { show: false },
  },
  yAxis: {
    type: 'category',
    splitArea: { show: true },
    axisLabel: { color: '#64748b', fontSize: 10, fontWeight: 600 },
    axisLine: { show: false },
    axisTick: { show: false },
  },
  visualMap: {
    min: 0,
    max: 100,
    calculable: true,
    orient: 'horizontal',
    left: 'center',
    bottom: 0,
    textStyle: { color: '#64748b', fontSize: 10 },
    inRange: {
      color: ['rgba(56,189,248,0.08)', '#f59e0b', '#ef4444'],
    },
  },
  series: [{
    type: 'heatmap',
    label: { show: true, color: '#0f172a', fontSize: 11, fontWeight: 700 },
    emphasis: {
      itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' },
    },
  }],
}
