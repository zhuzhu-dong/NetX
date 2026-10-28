<template>
  <section class="topology-panel" :class="{ minimal }">
    <div v-if="!minimal" class="panel-head">
      <div>
        <p>拓扑视图</p>
        <h2>实时网络拓扑</h2>
      </div>
      <div class="head-tools">
        <div v-if="activeNode" class="node-status">
          <span>{{ nodeDisplayName(activeNode) }}</span>
          <strong>{{ nodeStateLabel(activeNode.state) }}</strong>
        </div>
        <div class="incident-pill">{{ topologyHealth }}</div>
      </div>
    </div>

    <div class="topology-workbench">
      <aside class="topology-rail topology-filter-rail" @pointerdown.stop>
        <div class="rail-heading">
          <span class="rail-kicker">FABRIC FILTER</span>
          <strong>拓扑筛选</strong>
          <small>{{ visibleNodes.length }} / {{ nodes.length }} 个节点可见</small>
        </div>

        <label class="rail-search">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6"></circle>
            <path d="M16 16l5 5"></path>
          </svg>
          <input
            ref="searchInputRef"
            v-model="searchText"
            type="search"
            :placeholder="SEARCH_PLACEHOLDER"
            @input="onSearchInput"
            @keydown.enter.prevent="searchNext"
            @keydown.arrow-down.prevent="searchNext"
            @keydown.arrow-up.prevent="searchPrev"
          />
          <button v-if="searchText" type="button" aria-label="清除搜索" @click="clearSearch">×</button>
        </label>

        <div class="rail-section">
          <div class="rail-section-title">
            <span>设备类型</span>
            <button type="button" @click="resetTypeFilters">全部</button>
          </div>
          <button
            v-for="item in deviceTypeOptions"
            :key="item.value"
            type="button"
            class="filter-option"
            :class="{ active: typeFilters.includes(item.value) }"
            @click="toggleTypeFilter(item.value)"
          >
            <span class="filter-icon" :class="item.value">
              <svg v-if="item.value === 'switch'" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="6" width="18" height="12" rx="3"></rect>
                <path d="M7 10h2M11 10h2M15 10h2M7 14h10"></path>
              </svg>
              <svg v-else-if="item.value === 'host'" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="4" width="18" height="13" rx="2"></rect>
                <path d="M8 21h8M12 17v4"></path>
              </svg>
              <svg v-else viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="3"></circle>
                <circle cx="12" cy="12" r="8"></circle>
                <path d="M12 1v3M12 20v3M1 12h3M20 12h3"></path>
              </svg>
            </span>
            <span>
              <strong>{{ item.label }}</strong>
              <small>{{ typeCount(item.value) }} 个</small>
            </span>
            <i></i>
          </button>
        </div>

        <div class="rail-section">
          <div class="rail-section-title"><span>图层控制</span></div>
          <button
            v-for="layer in layerOptions"
            :key="layer.key"
            type="button"
            class="layer-option"
            :class="{ active: layers[layer.key] }"
            @click="layers[layer.key] = !layers[layer.key]"
          >
            <span>{{ layer.label }}</span>
            <i></i>
          </button>
        </div>

        <div class="rail-legend">
          <span><i class="normal"></i>正常</span>
          <span><i class="degraded"></i>拥塞</span>
          <span><i class="failed"></i>异常</span>
        </div>
      </aside>

      <div
        ref="canvasRef"
        class="topology-canvas"
        :class="{
          composing: isComposing,
          panning: isPanning,
          'labels-hidden': !layers.labels,
          'traffic-hidden': !layers.traffic,
        }"
        @pointerdown="startPan"
        @pointermove="handlePointerMove"
        @pointerup="stopPan"
        @pointercancel="stopPan"
        @pointerleave="handlePointerLeave"
        @wheel.prevent="handleWheel"
      >
      <div class="map-grid"></div>

      <svg class="link-layer" :viewBox="viewBox" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <filter id="active-link-glow">
            <feGaussianBlur stdDeviation="0.8" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <g
          v-for="link in renderedLinks"
          :key="link.id"
          :class="[link.tone, link.state, { active: link.active || link.id === selectedLinkId, hover: link.id === hoveredLinkId, dimmed: focusedNodeId && !link.active }]"
          style="pointer-events: auto; cursor: pointer;"
          @pointerdown.stop
          @click.stop="selectLink(link)"
          @mouseenter="hoveredLinkId = link.id"
          @mouseleave="hoveredLinkId = null"
        >
          <!-- Invisible wide hit area for easier link clicking -->
          <line
            class="link-hit"
            :x1="link.from.x"
            :y1="link.from.y"
            :x2="link.to.x"
            :y2="link.to.y"
          />
          <line
            class="link-glow"
            :x1="link.from.x"
            :y1="link.from.y"
            :x2="link.to.x"
            :y2="link.to.y"
          />
          <line
            class="link-main"
            :x1="link.from.x"
            :y1="link.from.y"
            :x2="link.to.x"
            :y2="link.to.y"
          />
          <line
            class="link-sheen"
            :x1="link.from.x"
            :y1="link.from.y"
            :x2="link.to.x"
            :y2="link.to.y"
          />
          <line
            class="link-flow"
            :x1="link.from.x"
            :y1="link.from.y"
            :x2="link.to.x"
            :y2="link.to.y"
          />
        </g>
      </svg>

      <button
        v-for="node in visibleNodes"
        :key="node.id"
        class="topology-node"
        :class="[
          nodeCategory(node),
          node.state,
          {
            active: node.id === selectedNodeId,
            focused: node.id === focusedNodeId,
            related: isRelatedNode(node.id),
            dimmed: isSearchActive ? !matchIds.includes(node.id) : (focusedNodeId && !isRelatedNode(node.id) && node.id !== focusedNodeId),
          },
        ]"
        :style="nodeStyle(node)"
        type="button"
        @pointerdown.stop="(e) => onNodePointerDown(e, node)"
        @pointermove="onNodePointerMove"
        @pointerup="onNodePointerUp"
        @pointercancel="onNodePointerUp"
        @mouseenter="hoverNode = node"
        @mouseleave="hoverNode = null"
      >
        <span v-if="nodeCategory(node) === 'agent'" class="agent-core-visual" aria-hidden="true">
          <span class="agent-orbit orbit-a"></span>
          <span class="agent-orbit orbit-b"></span>
          <span class="agent-wave wave-a"></span>
          <span class="agent-wave wave-b"></span>
          <span class="agent-core-ball"><i></i></span>
        </span>

        <template v-else>
          <span class="device-node" aria-hidden="true">
            <svg v-if="nodeCategory(node) === 'switch'" viewBox="0 0 24 24">
              <rect x="3" y="6" width="18" height="12" rx="3"></rect>
              <path d="M7 10h2M11 10h2M15 10h2M7 14h10"></path>
            </svg>
            <svg v-else viewBox="0 0 24 24">
              <rect x="3" y="4" width="18" height="13" rx="2"></rect>
              <path d="M8 21h8M12 17v4"></path>
            </svg>
            <i></i>
          </span>
          <span class="star-label">
            <strong>{{ nodeDisplayName(node) }}</strong>
            <small>{{ shortRole(node) }}</small>
          </span>
        </template>

        <span v-if="nodeCategory(node) === 'agent'" class="agent-core-label">
          <strong>{{ nodeDisplayName(node) }}</strong>
          <small>{{ shortRole(node) }}</small>
        </span>
      </button>

      <div
        v-if="hoverNode && !activeNode"
        class="node-tooltip"
        :class="{ reverse: projectedNode(hoverNode).x > 68 }"
        :style="tooltipStyle(hoverNode)"
      >
        <div class="tooltip-head">
          <i :class="hoverNode.state"></i>
          <strong>{{ nodeDisplayName(hoverNode) }}</strong>
        </div>
        <p>{{ hoverNode.role || shortRole(hoverNode) }}</p>
        <div class="tooltip-meta">
          <span>{{ nodeStateLabel(hoverNode.state) }}</span>
          <em>{{ hoverNode.kpi || '-' }}</em>
        </div>
      </div>

      <div class="topology-toolbar" @pointerdown.stop>
        <button type="button" aria-label="放大拓扑" @click="zoomBy(1.18)">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6"></circle>
            <path d="M11 8v6M8 11h6M16 16l4 4"></path>
          </svg>
        </button>
        <button type="button" aria-label="缩小拓扑" @click="zoomBy(0.84)">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="6"></circle>
            <path d="M8 11h6M16 16l4 4"></path>
          </svg>
        </button>
        <button type="button" aria-label="适配全部节点" @click="fitView">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"></path>
          </svg>
        </button>
        <button type="button" aria-label="恢复自动布局" @click="resetLayout">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 12a8 8 0 0 1 13.4-5.9M20 12a8 8 0 0 1-13.4 5.9"></path>
            <path d="M17.5 2.8v3.8h-3.8M6.5 21.2v-3.8h3.8"></path>
          </svg>
        </button>
      </div>

      <div class="map-status-strip">
        <span>{{ visibleNodes.length }} 节点</span>
        <span>{{ renderedLinks.length }} 链路</span>
        <span>{{ Math.round(zoom * 100) }}%</span>
      </div>

      <div v-if="!minimal" class="legend-strip">
        <span class="safe">主链路</span>
        <span class="warn">绕行/降级</span>
        <span class="danger">故障/攻击</span>
      </div>
      </div>

      <aside class="topology-rail topology-inspector" @pointerdown.stop>
        <template v-if="activeNode && !activeLink">
          <div class="inspector-heading">
            <div class="inspector-icon" :class="nodeCategory(activeNode)">
              <svg v-if="nodeCategory(activeNode) === 'switch'" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="6" width="18" height="12" rx="3"></rect>
                <path d="M7 10h2M11 10h2M15 10h2M7 14h10"></path>
              </svg>
              <svg v-else-if="nodeCategory(activeNode) === 'host'" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="3" y="4" width="18" height="13" rx="2"></rect>
                <path d="M8 21h8M12 17v4"></path>
              </svg>
              <svg v-else viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="3"></circle>
                <circle cx="12" cy="12" r="8"></circle>
              </svg>
            </div>
            <div>
              <span class="rail-kicker">{{ nodeTypeLabel(activeNode) }}</span>
              <strong>{{ nodeDisplayName(activeNode) }}</strong>
              <small>{{ activeNode.id }}</small>
            </div>
            <button type="button" class="inspector-close" aria-label="关闭详情" @click="clearSelection">×</button>
          </div>

          <div class="inspector-state">
            <span><i :class="activeNode.state"></i>{{ nodeStateLabel(activeNode.state) }}</span>
            <strong>{{ activeNode.kpi || '实时在线' }}</strong>
          </div>

          <dl class="inspector-grid">
            <div><dt>设备角色</dt><dd>{{ activeNode.role || shortRole(activeNode) }}</dd></div>
            <div><dt>连接链路</dt><dd>{{ nodeConnectionCount(activeNode.id) }}</dd></div>
            <div><dt>端口数量</dt><dd>{{ nodePortLabel(activeNode) }}</dd></div>
            <div><dt>流表数量</dt><dd>{{ nodeFlowLabel(activeNode) }}</dd></div>
            <div><dt>实时流量</dt><dd>{{ nodeTrafficLabel(activeNode) }}</dd></div>
            <div><dt>连接状态</dt><dd>{{ nodeLinkSummary(activeNode.id) }}</dd></div>
          </dl>

          <div class="inspector-section">
            <span class="inspector-section-title">相邻设备</span>
            <p>{{ nodePeerNames(activeNode.id) }}</p>
          </div>

          <div v-if="Array.isArray(activeNode.ports) && activeNode.ports.length" class="inspector-section">
            <span class="inspector-section-title">端口状态</span>
            <div class="port-list">
              <div v-for="port in activeNode.ports.slice(0, 6)" :key="port.port ?? port.name">
                <code>#{{ port.port ?? port.name }}</code>
                <span>{{ port.peer || port.name || '--' }}</span>
                <i :class="port.state || 'normal'"></i>
              </div>
            </div>
          </div>

          <div class="inspector-actions">
            <button type="button" @click="emitNodeQuickIntent(activeNode, 'monitor')">流量监控</button>
            <button type="button" @click="emitNodeQuickIntent(activeNode, 'isolate')">隔离主机</button>
            <button type="button" @click="emitNodeQuickIntent(activeNode, 'limit')">限制带宽</button>
          </div>
        </template>

        <template v-else-if="activeLink">
          <div class="inspector-heading">
            <div class="inspector-icon link">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M9 15l6-6M7 17l-2 2a3 3 0 0 1-4-4l4-4a3 3 0 0 1 4 0M17 7l2-2a3 3 0 1 1 4 4l-4 4a3 3 0 0 1-4 0"></path>
              </svg>
            </div>
            <div>
              <span class="rail-kicker">LINK INSPECTOR</span>
              <strong>{{ nodeDisplayName(linkSourceNode || {}) }} → {{ nodeDisplayName(linkTargetNode || {}) }}</strong>
              <small>{{ activeLink.id }}</small>
            </div>
            <button type="button" class="inspector-close" aria-label="关闭详情" @click="selectedLinkId = ''">×</button>
          </div>
          <div class="inspector-state">
            <span><i :class="activeLink.state"></i>{{ nodeStateLabel(activeLink.state) }}</span>
            <strong>{{ linkRoleLabel(activeLink.role) }}</strong>
          </div>
          <dl class="inspector-grid">
            <div><dt>源设备</dt><dd>{{ nodeDisplayName(linkSourceNode || {}) }}</dd></div>
            <div><dt>目标设备</dt><dd>{{ nodeDisplayName(linkTargetNode || {}) }}</dd></div>
            <div><dt>带宽</dt><dd>{{ activeLink.bandwidth_mbps ? `${activeLink.bandwidth_mbps} Mbps` : '--' }}</dd></div>
            <div><dt>链路开销</dt><dd>{{ activeLink.cost ?? '--' }}</dd></div>
            <div><dt>源端口</dt><dd>{{ activeLink.fromPort ?? '--' }}</dd></div>
            <div><dt>目标端口</dt><dd>{{ activeLink.toPort ?? '--' }}</dd></div>
          </dl>
        </template>

        <template v-else>
          <div class="rail-heading">
            <span class="rail-kicker">FABRIC SUMMARY</span>
            <strong>网络结构概览</strong>
            <small>选择节点或链路查看实时详情</small>
          </div>
          <div class="summary-health">
            <span>总体状态</span>
            <strong>{{ topologyHealth }}</strong>
          </div>
          <dl class="summary-grid">
            <div><dt>交换设备</dt><dd>{{ typeCount('switch') }}</dd></div>
            <div><dt>终端主机</dt><dd>{{ typeCount('host') }}</dd></div>
            <div><dt>控制节点</dt><dd>{{ typeCount('agent') }}</dd></div>
            <div><dt>活动链路</dt><dd>{{ links.length }}</dd></div>
          </dl>
          <div class="inspector-section">
            <span class="inspector-section-title">操作提示</span>
            <p>拖动画布进行平移，滚轮缩放视图；拖动节点可固定其位置。</p>
          </div>
        </template>
      </aside>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef, watch } from 'vue'
import { NETWORK_STATE_LABEL, LINK_ROLE_LABEL } from '../../constants'

const props = defineProps({
  nodes: {
    type: Array,
    default: () => []
  },
  links: {
    type: Array,
    default: () => []
  },
  metrics: {
    type: Array,
    default: () => []
  },
  selectedNodeId: {
    type: String,
    default: ''
  },
  isComposing: {
    type: Boolean,
    default: false
  },
  minimal: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['select-node', 'select-link', 'quick-intent'])

const canvasRef = ref(null)
const hoverNode = ref(null)
const hoveredLinkId = ref(null)
const selectedLinkId = ref('')
const DEFAULT_ZOOM = 1
const zoom = ref(DEFAULT_ZOOM)
const viewOrigin = ref(centerOriginForZoom(DEFAULT_ZOOM))
const isPanning = ref(false)

// ── Search ──────────────────────────────────────────────────────────────────
const searchText = ref('')
const matchIds = ref([])
const matchIdx = ref(0)
const searchInputRef = ref(null)
const SEARCH_PLACEHOLDER = '搜索设备、角色或标识'

const deviceTypeOptions = [
  { value: 'agent', label: '控制节点' },
  { value: 'switch', label: '交换设备' },
  { value: 'host', label: '终端主机' },
]
const layerOptions = [
  { key: 'links', label: '链路关系' },
  { key: 'labels', label: '设备名称' },
  { key: 'traffic', label: '流量动画' },
]
const typeFilters = ref(deviceTypeOptions.map((item) => item.value))
const layers = reactive({
  links: true,
  labels: true,
  traffic: true,
})

const isSearchActive = computed(() => searchText.value.trim().length > 0)

let panStart = null
let viewTweenFrame = null
let panFrame = null
let pendingPanOrigin = null

/* ═══════════════════════════════════════════════════════════════════════════════
   Stable layered layout state
   ═══════════════════════════════════════════════════════════════════════════════ */

const positions = shallowRef(new Map())
const manualPositionIds = new Set()
let dragTarget = null
let dragStartPos = null
let dragMoved = false
const DRAG_THRESHOLD = 4
let dragFrame = null
let pendingDragPosition = null

const activeNode = computed(() => props.nodes.find((node) => node.id === props.selectedNodeId))
const focusedNodeId = computed(() => hoverNode.value?.id || props.selectedNodeId)
const viewWidth = computed(() => 100 / zoom.value)
const viewHeight = computed(() => 100 / zoom.value)
const viewBox = computed(() => `${viewOrigin.value.x} ${viewOrigin.value.y} ${viewWidth.value} ${viewHeight.value}`)

const nodeMap = computed(() => new Map(props.nodes.map((node) => [node.id, node])))
const visibleNodes = computed(() => props.nodes.filter((node) => typeFilters.value.includes(nodeCategory(node))))
const visibleNodeIds = computed(() => new Set(visibleNodes.value.map((node) => node.id)))

const adjacency = computed(() => {
  const graph = new Map()
  props.nodes.forEach((node) => graph.set(node.id, new Set()))
  props.links.forEach((link) => {
    graph.get(link.from)?.add(link.to)
    graph.get(link.to)?.add(link.from)
  })
  return graph
})

const renderedLinks = computed(() => {
  if (!layers.links) return []
  const focus = focusedNodeId.value
  return props.links
    .filter((link) => visibleNodeIds.value.has(link.from) && visibleNodeIds.value.has(link.to))
    .map((link) => {
      const fromNode = nodeMap.value.get(link.from)
      const toNode = nodeMap.value.get(link.to)
      if (!fromNode || !toNode) return null
      const fromPos = pos(link.from)
      const toPos = pos(link.to)
      return {
        ...link,
        tone: linkTone(link),
        from: { ...fromNode, x: fromPos.x, y: fromPos.y },
        to: { ...toNode, x: toPos.x, y: toPos.y },
        active: Boolean(focus && (link.from === focus || link.to === focus)),
      }
    })
    .filter(Boolean)
})

const telemetryCards = computed(() => {
  const preferred = ['latency_ms', 'packet_loss_rate', 'throughput_mbps', 'queue_depth']
  const fallbackLabel = {
    latency_ms: '时延',
    packet_loss_rate: '丢包',
    throughput_mbps: '吞吐',
    queue_depth: '队列',
  }
  const byId = new Map(props.metrics.map((metric) => [metric.id || metric.key || metric.label, metric]))
  const selected = preferred
    .map((id) => {
      const metric = byId.get(id)
      if (!metric) return null
      return {
        key: id,
        label: fallbackLabel[id] || metric.label || id,
        value: metric.value ?? '-',
        status: metric.status || 'idle',
      }
    })
    .filter(Boolean)

  if (selected.length) return selected

  return props.metrics.slice(0, 4).map((metric, index) => ({
    key: metric.id || metric.key || `${metric.label}-${index}`,
    label: metricLabel(metric),
    value: metric.value ?? '-',
    status: metric.status || 'idle',
  }))
})

const topologyHealth = computed(() => {
  const states = props.nodes.map((node) => String(node.state || '').toLowerCase())
  if (states.some((state) => ['failed', 'attack', 'down', 'error', 'offline', 'abnormal'].includes(state))) {
    return NETWORK_STATE_LABEL.attack
  }
  if (states.some((state) => ['degraded', 'warning', 'congested', 'congestion', 'busy'].includes(state))) {
    return NETWORK_STATE_LABEL.degraded
  }
  return NETWORK_STATE_LABEL.normal
})

// ── Search logic ────────────────────────────────────────────────────────────

function onSearchInput() {
  const q = searchText.value.toLowerCase().trim()
  matchIdx.value = 0
  if (!q) { matchIds.value = []; return }
  matchIds.value = visibleNodes.value
    .filter(n => {
      const fields = [n.name, n.id, n.rawId, n.role, n.detail]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return fields.includes(q)
    })
    .map(n => n.id)
}

function searchNext() {
  if (!matchIds.value.length) return
  matchIdx.value = (matchIdx.value + 1) % matchIds.value.length
  focusMatch()
}
function searchPrev() {
  if (!matchIds.value.length) return
  matchIdx.value = (matchIdx.value - 1 + matchIds.value.length) % matchIds.value.length
  focusMatch()
}
function focusMatch() {
  const nodeId = matchIds.value[matchIdx.value]
  const node = props.nodes.find(n => n.id === nodeId)
  if (node) {
    emit('select-node', node)
    const p = pos(nodeId)
    animateViewTo(p.x - 50, p.y - 50, 1)
  }
}

function clearSearch() {
  searchText.value = ''
  matchIds.value = []
  matchIdx.value = 0
}

function toggleTypeFilter(type) {
  const current = new Set(typeFilters.value)
  if (current.has(type)) {
    if (current.size === 1) return
    current.delete(type)
  } else {
    current.add(type)
  }
  typeFilters.value = deviceTypeOptions
    .map((item) => item.value)
    .filter((item) => current.has(item))
  onSearchInput()
}

function resetTypeFilters() {
  typeFilters.value = deviceTypeOptions.map((item) => item.value)
  onSearchInput()
}

function typeCount(type) {
  return props.nodes.filter((node) => nodeCategory(node) === type).length
}

// ── Link selection ──────────────────────────────────────────────────────────
const activeLink = computed(() => {
  if (!selectedLinkId.value) return null
  return props.links.find(l => l.id === selectedLinkId.value) || null
})
const linkSourceNode = computed(() => {
  if (!activeLink.value) return null
  return props.nodes.find(n => n.id === activeLink.value.from) || null
})
const linkTargetNode = computed(() => {
  if (!activeLink.value) return null
  return props.nodes.find(n => n.id === activeLink.value.to) || null
})

function selectLink(link) {
  selectedLinkId.value = link.id
  emit('select-link', link)
}

function selectNodeEvent(node) {
  selectedLinkId.value = ''
  emit('select-node', node)
}

/* ═══════════════════════════════════════════════════════════════════════════════
   Stable layered layout
   ═══════════════════════════════════════════════════════════════════════════════ */

function pos(nodeId) {
  const p = positions.value.get(nodeId)
  return p || { x: 50, y: 50 }
}

function nodeCategory(node) {
  const type = String(node?.type || '').toLowerCase()
  if (type.includes('agent') || type.includes('controller') || type.includes('control')) return 'agent'
  if (type.includes('host') || type.includes('client') || type.includes('endpoint')) return 'host'
  return 'switch'
}

function isCoreSwitch(node) {
  const signature = `${node?.name || ''} ${node?.id || ''} ${node?.role || ''}`.toLowerCase()
  return /core|spine|核心|骨干/.test(signature)
}

function distributeRow(items, y, next, minX = 14, maxX = 86) {
  if (!items.length) return
  const gap = items.length === 1 ? 0 : (maxX - minX) / (items.length - 1)
  items.forEach((node, index) => {
    next.set(node.id, {
      x: items.length === 1 ? 50 : minX + gap * index,
      y,
    })
  })
}

function buildStableLayout(resetManual = false) {
  if (resetManual) manualPositionIds.clear()
  const previous = positions.value
  const generated = new Map()
  const controllers = props.nodes.filter((node) => nodeCategory(node) === 'agent')
  const switches = props.nodes.filter((node) => nodeCategory(node) === 'switch')
  const coreSwitches = switches.filter(isCoreSwitch)
  const accessSwitches = switches.filter((node) => !isCoreSwitch(node))
  const hosts = props.nodes.filter((node) => nodeCategory(node) === 'host')

  distributeRow(controllers, 14, generated, 38, 62)
  distributeRow(coreSwitches, 38, generated, 22, 78)
  distributeRow(accessSwitches, coreSwitches.length ? 61 : 48, generated, 12, 88)
  distributeRow(hosts, 84, generated, 10, 90)

  const uncategorized = props.nodes.filter((node) => !generated.has(node.id))
  distributeRow(uncategorized, 70, generated, 14, 86)

  const next = new Map()
  props.nodes.forEach((node) => {
    const manual = previous.get(node.id)
    if (!resetManual && manual && manualPositionIds.has(node.id)) {
      next.set(node.id, manual)
      return
    }
    next.set(node.id, generated.get(node.id) || { x: 50, y: 50 })
  })
  positions.value = next
}

function linkTone(link) {
  const state = String(link?.state || '').toLowerCase()
  const tone = String(link?.tone || '').toLowerCase()
  if (['failed', 'attack', 'down', 'abnormal'].includes(state) || ['failed', 'danger'].includes(tone)) return 'failed'
  if (['degraded', 'congested', 'warning'].includes(state) || ['degraded', 'warning'].includes(tone)) return 'degraded'
  return 'primary'
}

function flushDragPosition() {
  if (!pendingDragPosition || !dragTarget) return
  const next = new Map(positions.value)
  next.set(dragTarget.id, pendingDragPosition)
  positions.value = next
  pendingDragPosition = null
}

function scheduleDragPosition(position) {
  pendingDragPosition = position
  if (dragFrame) return
  dragFrame = requestAnimationFrame(() => {
    dragFrame = null
    flushDragPosition()
  })
}

/* ── Node drag ─────────────────────────────────────────────────────────────── */

function onNodePointerDown(event, node) {
  // Only left button
  if (event.button !== 0) return
  const rect = canvasRef.value?.getBoundingClientRect()
  if (!rect) return
  cancelViewAnimation()
  dragTarget = node
  dragMoved = false
  dragStartPos = {
    clientX: event.clientX,
    clientY: event.clientY,
    simX: pos(node.id).x,
    simY: pos(node.id).y,
    width: viewWidth.value,
    height: viewHeight.value,
    originX: viewOrigin.value.x,
    originY: viewOrigin.value.y,
    rect,
  }
  event.currentTarget.setPointerCapture?.(event.pointerId)
}

function onNodePointerMove(event) {
  if (!dragTarget || !dragStartPos) return
  const pixelDx = event.clientX - dragStartPos.clientX
  const pixelDy = event.clientY - dragStartPos.clientY
  const dx = ((event.clientX - dragStartPos.clientX) / dragStartPos.rect.width) * dragStartPos.width
  const dy = ((event.clientY - dragStartPos.clientY) / dragStartPos.rect.height) * dragStartPos.height
  const dist = Math.hypot(pixelDx, pixelDy)

  if (dist > DRAG_THRESHOLD) dragMoved = true
  if (!dragMoved) return

  const worldX = clamp(dragStartPos.simX + dx, 1, 99)
  const worldY = clamp(dragStartPos.simY + dy, 1, 99)
  scheduleDragPosition({ x: worldX, y: worldY })
}

function onNodePointerUp(event) {
  if (!dragTarget) return
  if (dragMoved) {
    flushDragPosition()
    manualPositionIds.add(dragTarget.id)
  } else {
    selectCanvasNode(dragTarget)
  }
  event?.currentTarget?.releasePointerCapture?.(event.pointerId)
  dragTarget = null
  dragStartPos = null
  dragMoved = false
}

function startPan(event) {
  if (event.button !== 0) return
  const rect = canvasRef.value?.getBoundingClientRect()
  if (!rect) return
  cancelViewAnimation()
  isPanning.value = true
  panStart = {
    x: event.clientX,
    y: event.clientY,
    originX: viewOrigin.value.x,
    originY: viewOrigin.value.y,
    width: viewWidth.value,
    height: viewHeight.value,
    rect,
  }
  event.currentTarget.setPointerCapture?.(event.pointerId)
}

function handlePointerMove(event) {
  if (!isPanning.value || !panStart) return
  const dx = ((event.clientX - panStart.x) / panStart.rect.width) * panStart.width
  const dy = ((event.clientY - panStart.y) / panStart.rect.height) * panStart.height
  pendingPanOrigin = {
    x: panStart.originX - dx,
    y: panStart.originY - dy,
  }
  if (panFrame) return
  panFrame = requestAnimationFrame(() => {
    panFrame = null
    if (!pendingPanOrigin) return
    setViewOrigin(pendingPanOrigin.x, pendingPanOrigin.y)
    pendingPanOrigin = null
  })
}

function stopPan(event) {
  if (!isPanning.value) return
  if (pendingPanOrigin) {
    setViewOrigin(pendingPanOrigin.x, pendingPanOrigin.y)
    pendingPanOrigin = null
  }
  isPanning.value = false
  panStart = null
  event?.currentTarget?.releasePointerCapture?.(event.pointerId)
}

function handlePointerLeave(event) {
  if (isPanning.value) stopPan(event)
}

function handleWheel(event) {
  const rect = canvasRef.value?.getBoundingClientRect()
  if (!rect) return
  const nextZoom = clamp(zoom.value * (event.deltaY < 0 ? 1.12 : 0.9), 0.25, 3.8)
  zoomAt(nextZoom, (event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height)
}

function zoomBy(factor) {
  zoomAt(clamp(zoom.value * factor, 0.25, 3.8), 0.5, 0.5)
}

function zoomAt(nextZoom, ratioX, ratioY) {
  cancelViewAnimation()
  const worldX = viewOrigin.value.x + ratioX * viewWidth.value
  const worldY = viewOrigin.value.y + ratioY * viewHeight.value
  zoom.value = nextZoom
  setViewOrigin(worldX - ratioX * viewWidth.value, worldY - ratioY * viewHeight.value)
}

function resetView() {
  emit('select-node', null)
  fitView()
}

function fitView(animated = true) {
  const nodeList = visibleNodes.value
  if (!nodeList.length) {
    animateViewToCenter(DEFAULT_ZOOM)
    return
  }

  const xs = nodeList.map((node) => pos(node.id).x)
  const ys = nodeList.map((node) => pos(node.id).y)
  const minX = Math.min(...xs)
  const maxX = Math.max(...xs)
  const minY = Math.min(...ys)
  const maxY = Math.max(...ys)
  const rangeX = Math.max(24, maxX - minX + 22)
  const rangeY = Math.max(24, maxY - minY + 24)
  const targetZoom = clamp(Math.min(100 / rangeX, 100 / rangeY), 0.72, 1.32)
  const targetWidth = 100 / targetZoom
  const targetHeight = 100 / targetZoom
  const targetX = (minX + maxX) / 2 - targetWidth / 2
  const targetY = (minY + maxY) / 2 - targetHeight / 2

  if (animated) {
    animateViewTo(targetX, targetY, targetZoom)
    return
  }
  zoom.value = targetZoom
  viewOrigin.value = clampViewOrigin(targetX, targetY, targetWidth, targetHeight)
}

function resetLayout() {
  buildStableLayout(true)
  emit('select-node', null)
  selectedLinkId.value = ''
  requestAnimationFrame(() => fitView())
}

function centerOriginForZoom(targetZoom = DEFAULT_ZOOM) {
  const width = 100 / targetZoom
  const height = 100 / targetZoom
  return clampViewOrigin(50 - width / 2, 50 - height / 2, width, height)
}

function animateViewToCenter(targetZoom = DEFAULT_ZOOM) {
  const width = 100 / targetZoom
  const height = 100 / targetZoom
  animateViewTo(50 - width / 2, 50 - height / 2, targetZoom)
}

function applyCenteredInitialView() {
  cancelViewAnimation()
  zoom.value = DEFAULT_ZOOM
  viewOrigin.value = centerOriginForZoom(DEFAULT_ZOOM)
}

function setViewOrigin(x, y) {
  viewOrigin.value = clampViewOrigin(x, y, viewWidth.value, viewHeight.value)
}

function clampViewOrigin(x, y, width, height) {
  const marginX = Math.max(50, width * 0.5)
  const marginY = Math.max(50, height * 0.5)
  return {
    x: clamp(x, -marginX, 100 - width + marginX),
    y: clamp(y, -marginY, 100 - height + marginY),
  }
}

function selectCanvasNode(node) {
  selectedLinkId.value = ''
  emit('select-node', node)
  focusNode(node)
}

function focusNode(node) {
  const p = pos(node.id)
  animateViewTo(p.x - 50, p.y - 50, 1)
}

function animateViewTo(targetX, targetY, targetZoom = 1) {
  cancelViewAnimation()
  const startZoom = zoom.value
  const startOrigin = { ...viewOrigin.value }
  const targetWidth = 100 / targetZoom
  const targetHeight = 100 / targetZoom
  const targetOrigin = clampViewOrigin(targetX, targetY, targetWidth, targetHeight)
  const startedAt = performance.now()
  const duration = 560

  const step = (now) => {
    const progress = clamp((now - startedAt) / duration, 0, 1)
    const eased = easeOutCubic(progress)
    const nextZoom = startZoom + (targetZoom - startZoom) * eased
    const nextX = startOrigin.x + (targetOrigin.x - startOrigin.x) * eased
    const nextY = startOrigin.y + (targetOrigin.y - startOrigin.y) * eased

    zoom.value = nextZoom
    viewOrigin.value = clampViewOrigin(nextX, nextY, 100 / nextZoom, 100 / nextZoom)

    if (progress < 1) {
      viewTweenFrame = requestAnimationFrame(step)
      return
    }

    zoom.value = targetZoom
    viewOrigin.value = targetOrigin
    viewTweenFrame = null
  }

  viewTweenFrame = requestAnimationFrame(step)
}

function cancelViewAnimation() {
  if (!viewTweenFrame) return
  cancelAnimationFrame(viewTweenFrame)
  viewTweenFrame = null
}

function easeOutCubic(value) {
  return 1 - Math.pow(1 - value, 3)
}

onMounted(() => {
  buildStableLayout(true)
  requestAnimationFrame(() => fitView(false))
})
onUnmounted(() => {
  cancelViewAnimation()
  if (dragFrame) cancelAnimationFrame(dragFrame)
  if (panFrame) cancelAnimationFrame(panFrame)
})
watch(
  () => props.nodes.map((node) => node.id).join('|'),
  () => {
    buildStableLayout()
    requestAnimationFrame(() => fitView(false))
  },
)
watch(typeFilters, () => requestAnimationFrame(() => fitView()), { deep: true })

function centerForEntry() {
  fitView(false)
}

defineExpose({
  resetView,
  fitView,
  centerForEntry,
})

function clearSelection() {
  selectedLinkId.value = ''
  emit('select-node', null)
}

function nodeStyle(node) {
  const p = pos(node.id)
  const projected = {
    x: ((p.x - viewOrigin.value.x) / viewWidth.value) * 100,
    y: ((p.y - viewOrigin.value.y) / viewHeight.value) * 100,
  }
  return {
    left: `${projected.x}%`,
    top: `${projected.y}%`,
  }
}

// Card dimensions for boundary clamping
const CARD_W = 320
const CARD_H = 280
const CARD_OFFSET = 18

function detailCardStyle(node) {
  const p = pos(node.id)
  const canvas = canvasRef.value
  if (!canvas) return { display: 'none' }
  const rect = canvas.getBoundingClientRect()
  // Teleported to body → fixed is viewport-relative, safe from ancestor containment
  const px = rect.left + ((p.x - viewOrigin.value.x) / viewWidth.value) * rect.width
  const py = rect.top + ((p.y - viewOrigin.value.y) / viewHeight.value) * rect.height

  // Flip left if card would overflow right edge
  const spaceRight = window.innerWidth - px - CARD_OFFSET
  const toRight = spaceRight >= CARD_W
  const left = toRight ? px + CARD_OFFSET : px - CARD_W - CARD_OFFSET
  const top = Math.max(8, Math.min(window.innerHeight - CARD_H - 8, py - CARD_H / 2))

  return {
    position: 'fixed',
    left: `${left}px`,
    top: `${top}px`,
  }
}

const detailCardReverse = computed(() => {
  if (!activeNode.value) return false
  const p = pos(activeNode.value.id)
  const canvas = canvasRef.value
  if (!canvas) return false
  const rect = canvas.getBoundingClientRect()
  const px = ((p.x - viewOrigin.value.x) / viewWidth.value) * rect.width
  return px + CARD_OFFSET + CARD_W > rect.width - 16
})

function linkDetailStyle(link) {
  const canvas = canvasRef.value
  if (!canvas) return { display: 'none' }
  const fromP = pos(link.from)
  const toP = pos(link.to)
  const midX = (fromP.x + toP.x) / 2
  const midY = (fromP.y + toP.y) / 2
  const rect = canvas.getBoundingClientRect()
  // Teleported to body → fixed is viewport-relative
  const px = rect.left + ((midX - viewOrigin.value.x) / viewWidth.value) * rect.width
  const py = rect.top + ((midY - viewOrigin.value.y) / viewHeight.value) * rect.height

  const spaceRight = window.innerWidth - px - CARD_OFFSET
  const toRight = spaceRight >= CARD_W
  const left = toRight ? px + CARD_OFFSET : px - CARD_W - CARD_OFFSET
  const top = Math.max(8, Math.min(window.innerHeight - CARD_H - 8, py - CARD_H / 2))

  return {
    position: 'fixed',
    left: `${left}px`,
    top: `${top}px`,
  }
}

function tooltipStyle(node) {
  const p = pos(node.id)
  const projected = {
    x: ((p.x - viewOrigin.value.x) / viewWidth.value) * 100,
    y: ((p.y - viewOrigin.value.y) / viewHeight.value) * 100,
  }
  return {
    left: `${projected.x}%`,
    top: `${projected.y}%`,
  }
}

function projectedNode(node) {
  const p = pos(node.id)
  return {
    x: ((p.x - viewOrigin.value.x) / viewWidth.value) * 100,
    y: ((p.y - viewOrigin.value.y) / viewHeight.value) * 100,
  }
}

function isRelatedNode(nodeId) {
  const focus = focusedNodeId.value
  if (!focus) return false
  return nodeId === focus || adjacency.value.get(focus)?.has(nodeId)
}

function shortRole(node) {
  const category = nodeCategory(node)
  if (category === 'host') return '主机'
  if (category === 'agent') return '智能体'
  if ((node.role || '').includes('核心')) return '核心交换机'
  if ((node.role || '').includes('汇聚')) return '汇聚交换机'
  return '交换机'
}

function nodeTypeLabel(node) {
  const category = nodeCategory(node)
  if (category === 'agent') return '智能体核心'
  if (category === 'host') return '终端节点'
  if ((node.role || '').includes('核心')) return '核心节点'
  if ((node.role || '').includes('汇聚')) return '汇聚节点'
  return '网络节点'
}

function nodeDisplayName(node) {
  const nameMap = {
    'Agent Core': '智能体核心',
    'service-v': '视频业务',
    'client-x': '异常主机',
    'edge-01': '边缘交换机 01',
    'edge-02': '边缘交换机 02',
    'spine-01': '核心交换机 01',
    'spine-02': '备用核心 02',
    'dc-core': '数据中心核心',
    s_core: '核心交换机',
    s_agg1: '汇聚交换机 01',
    s_agg2: '汇聚交换机 02',
  }

  return nameMap[node?.name] || node?.name || node?.id || '--'
}

function nodeConnectionCount(nodeId) {
  return props.links.filter((link) => link.from === nodeId || link.to === nodeId).length
}

function nodePortLabel(node) {
  if (Array.isArray(node.ports)) return `${node.ports.length} 个`
  if (Number.isFinite(Number(node.port_count))) return `${Number(node.port_count)} 个`
  if (node.interface) return node.interface
  return '--'
}

function nodeFlowLabel(node) {
  if (Array.isArray(node.flows)) return `${node.flows.length} 条`
  const value = node.flow_count ?? node.flowCount ?? node.flow_entries
  return Number.isFinite(Number(value)) ? `${Number(value)} 条` : '--'
}

function nodeTrafficLabel(node) {
  const value = node.throughput_mbps ?? node.traffic_mbps ?? node.trafficMbps
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} Mbps` : '--'
}

function emitNodeQuickIntent(node, action) {
  const name = node.name || node.id
  const intents = {
    monitor: {
      priority: 'medium',
      text: `持续监控设备 ${name} 的全部端口双向流量，采样周期为 5 秒，持续 10 分钟；展示吞吐量、包速率、端口状态和丢包率，仅采集数据，不修改任何网络策略。`,
    },
    isolate: {
      priority: 'critical',
      text: `立即隔离设备 ${name}，阻断其与全网所有节点的双向业务通信，包括入站和出站流量；保留控制面管理通道，隔离持续 10 分钟，结束后自动恢复。`,
    },
    limit: {
      priority: 'high',
      text: `将设备 ${name} 的全部业务出站流量带宽上限设置为 100 Mbps，策略立即生效并持续 10 分钟；保留控制面流量，超限流量执行整形而不是直接丢弃。`,
    },
  }
  const intent = intents[action]
  if (!intent) return
  emit('quick-intent', intent)
}

function nodePeerNames(nodeId) {
  const peers = props.links
    .filter((link) => link.from === nodeId || link.to === nodeId)
    .map((link) => {
      const peer = nodeMap.value.get(link.from === nodeId ? link.to : link.from)
      return peer ? nodeDisplayName(peer) : ''
    })
    .filter(Boolean)

  if (!peers.length) return '孤立'
  if (peers.length <= 3) return peers.join(' / ')
  return `${peers.slice(0, 3).join(' / ')} +${peers.length - 3}`
}

function nodeLinkSummary(nodeId) {
  const relatedLinks = props.links.filter((link) => link.from === nodeId || link.to === nodeId)
  if (!relatedLinks.length) return '无链路'

  const counts = relatedLinks.reduce((acc, link) => {
    const state = link.state || 'normal'
    acc[state] = (acc[state] || 0) + 1
    return acc
  }, {})

  return Object.entries(counts)
    .map(([state, count]) => `${count} ${nodeStateLabel(state)}`)
    .join(' / ')
}

function nodeLinkRoleSummary(nodeId) {
  const relatedLinks = props.links.filter((link) => link.from === nodeId || link.to === nodeId)
  if (!relatedLinks.length) return '无链路'

  const counts = relatedLinks.reduce((acc, link) => {
    const role = link.role || (link.tone === 'degraded' ? 'backup' : 'primary')
    acc[role] = (acc[role] || 0) + 1
    return acc
  }, {})

  return Object.entries(counts)
    .map(([role, count]) => `${count} ${linkRoleLabel(role)}`)
    .join(' / ')
}

function nodePositionLabel(node) {
  const p = pos(node.id)
  return `${Math.round(p.x)}, ${Math.round(p.y)}`
}

function nodeIdentityLabel(node) {
  if (node.type === 'host') {
    const [ip = '-', mac = '-'] = String(node.detail || '').split('/').map((item) => item.trim())
    return { label: '主机标识', value: `${ip} / ${mac}` }
  }

  if (node.rawId) return { label: '设备标识', value: node.rawId }
  return { label: '拓扑坐标', value: nodePositionLabel(node) }
}

function metricLabel(metric) {
  const labels = {
    latency_ms: '时延',
    packet_loss_rate: '丢包',
    throughput_mbps: '吞吐',
    queue_depth: '队列',
    int_hops: 'INT 跳数',
    p4_direct_counter: 'P4 计数',
  }
  return labels[metric.id] || metric.label || metric.id || '指标'
}

function linkRoleLabel(role) {
  return LINK_ROLE_LABEL[role] || role || '链路'
}

function nodeStateLabel(state) {
  return NETWORK_STATE_LABEL[state] || state || '未知'
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}
</script>

<style scoped>
.topology-panel {
  position: relative;
  min-height: 0;
  height: 100%;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 12px;
}

.topology-panel.minimal {
  grid-template-rows: minmax(0, 1fr);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}

p,
h2 {
  margin: 0;
}

p {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.72rem;
  font-weight: 850;
  letter-spacing: 1.6px;
  text-transform: uppercase;
}

h2 {
  margin-top: 4px;
  color: #0f172a;
  font-size: 1.22rem;
}

.head-tools {
  display: flex;
  align-items: center;
  gap: 10px;
}

.node-status {
  min-width: 148px;
  height: 36px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 12px;
  border: 1px solid rgba(203, 213, 225, 0.76);
  border-radius: 999px;
  background: rgba(248, 250, 252, 0.72);
}

.node-status span {
  color: #64748b;
  font-size: 0.72rem;
  font-weight: 850;
}

.node-status strong {
  color: #0f172a;
  font-size: 0.78rem;
}

.incident-pill {
  height: 32px;
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  color: #075985;
  background: rgba(224, 242, 254, 0.72);
  border: 1px solid rgba(56, 189, 248, 0.28);
  font-size: 0.76rem;
  font-weight: 950;
}

.topology-workbench {
  position: relative;
  min-width: 0;
  min-height: 0;
  height: 100%;
  display: grid;
  grid-template-columns: 214px minmax(420px, 1fr) 278px;
  overflow: hidden;
  border: 1px solid rgba(148, 163, 184, 0.42);
  border-radius: 8px;
  background: rgba(248, 250, 252, 0.92);
  box-shadow:
    0 18px 44px rgba(15, 23, 42, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.9);
}

.topology-rail {
  position: relative;
  z-index: 20;
  min-width: 0;
  min-height: 0;
  box-sizing: border-box;
  overflow-x: hidden;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: rgba(148, 163, 184, 0.46) transparent;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.88), rgba(248, 250, 252, 0.78)),
    rgba(248, 250, 252, 0.86);
}

.topology-filter-rail {
  padding: 18px 14px;
  border-right: 1px solid rgba(203, 213, 225, 0.68);
}

.topology-inspector {
  padding: 18px 16px;
  border-left: 1px solid rgba(203, 213, 225, 0.68);
}

.rail-heading {
  min-width: 0;
  margin-bottom: 16px;
}

.rail-heading strong,
.rail-heading small {
  display: block;
}

.rail-heading strong {
  margin-top: 4px;
  color: #0f172a;
  font-size: 0.94rem;
  font-weight: 900;
  line-height: 1.2;
}

.rail-heading small {
  margin-top: 6px;
  color: #64748b;
  font-size: 0.66rem;
  font-weight: 720;
  line-height: 1.45;
}

.rail-kicker {
  display: block;
  color: #0284c7;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.58rem;
  font-weight: 900;
  letter-spacing: 0.12em;
}

.rail-search {
  height: 36px;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 7px;
  padding: 0 9px;
  border: 1px solid rgba(148, 163, 184, 0.5);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.82);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}

.rail-search:focus-within {
  border-color: rgba(14, 165, 233, 0.72);
  box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.1);
}

.rail-search svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: #64748b;
  stroke-width: 1.8;
  stroke-linecap: round;
}

.rail-search input {
  min-width: 0;
  border: 0;
  outline: 0;
  color: #0f172a;
  background: transparent;
  font-size: 0.7rem;
  font-weight: 700;
}

.rail-search input::placeholder {
  color: #94a3b8;
}

.rail-search button {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 4px;
  color: #64748b;
  background: transparent;
  cursor: pointer;
  font-size: 0.9rem;
}

.rail-search button:hover {
  color: #0f172a;
  background: #f1f5f9;
}

.rail-section {
  margin-top: 20px;
}

.rail-section + .rail-section {
  padding-top: 16px;
  border-top: 1px solid rgba(203, 213, 225, 0.58);
}

.rail-section-title {
  min-height: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.rail-section-title span {
  color: #475569;
  font-size: 0.64rem;
  font-weight: 900;
}

.rail-section-title button {
  border: 0;
  padding: 3px 5px;
  color: #0284c7;
  background: transparent;
  cursor: pointer;
  font-size: 0.62rem;
  font-weight: 850;
}

.filter-option {
  width: 100%;
  min-height: 44px;
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 8px;
  align-items: center;
  gap: 9px;
  padding: 6px 8px;
  border: 0;
  border-radius: 6px;
  color: #475569;
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: color 0.16s ease, background 0.16s ease;
}

.filter-option:hover {
  background: rgba(226, 232, 240, 0.54);
}

.filter-option.active {
  color: #0f172a;
  background: rgba(224, 242, 254, 0.72);
}

.filter-option > span:nth-child(2) {
  min-width: 0;
}

.filter-option strong,
.filter-option small {
  display: block;
}

.filter-option strong {
  font-size: 0.7rem;
  font-weight: 850;
}

.filter-option small {
  margin-top: 2px;
  color: #94a3b8;
  font-size: 0.58rem;
  font-weight: 700;
}

.filter-option > i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #cbd5e1;
}

.filter-option.active > i {
  background: #0ea5e9;
  box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.12);
}

.filter-icon {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(148, 163, 184, 0.34);
  border-radius: 6px;
  color: #475569;
  background: rgba(255, 255, 255, 0.72);
}

.filter-icon.switch {
  color: #4f46e5;
}

.filter-icon.host {
  color: #0284c7;
}

.filter-icon.agent {
  color: #7c3aed;
}

.filter-icon svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.layer-option {
  width: 100%;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 0 8px;
  border: 0;
  color: #64748b;
  background: transparent;
  cursor: pointer;
  font-size: 0.68rem;
  font-weight: 780;
  text-align: left;
}

.layer-option:hover {
  color: #0f172a;
}

.layer-option i {
  position: relative;
  width: 26px;
  height: 14px;
  border-radius: 999px;
  background: #cbd5e1;
  transition: background 0.16s ease;
}

.layer-option i::after {
  content: '';
  position: absolute;
  left: 2px;
  top: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.2);
  transition: transform 0.16s ease;
}

.layer-option.active {
  color: #0f172a;
}

.layer-option.active i {
  background: #0ea5e9;
}

.layer-option.active i::after {
  transform: translateX(12px);
}

.rail-legend {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin-top: 22px;
  padding-top: 14px;
  border-top: 1px solid rgba(203, 213, 225, 0.58);
}

.rail-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #64748b;
  font-size: 0.58rem;
  font-weight: 800;
  white-space: nowrap;
}

.rail-legend i,
.inspector-state i,
.port-list i {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #10b981;
}

.rail-legend i.degraded,
.inspector-state i.degraded,
.port-list i.degraded {
  background: #f59e0b;
}

.rail-legend i.failed,
.inspector-state i.failed,
.port-list i.failed,
.inspector-state i.attack {
  background: #ef4444;
}

.topology-canvas {
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border: 0;
  border-radius: 0;
  contain: layout style paint;
  background:
    radial-gradient(circle at 50% 44%, rgba(14, 165, 233, 0.085), transparent 42%),
    linear-gradient(rgba(100, 116, 139, 0.075) 1px, transparent 1px),
    linear-gradient(90deg, rgba(100, 116, 139, 0.075) 1px, transparent 1px),
    rgba(255, 255, 255, 0.86);
  background-size: auto, 32px 32px, 32px 32px, auto;
  box-shadow: inset 0 0 80px rgba(148, 163, 184, 0.06);
  cursor: grab;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
}

.topology-canvas.panning {
  cursor: grabbing;
}

.topology-canvas.labels-hidden .star-label,
.topology-canvas.labels-hidden .agent-core-label {
  opacity: 0;
}

.topology-canvas.traffic-hidden .link-flow,
.topology-canvas.traffic-hidden .link-sheen,
.topology-canvas.traffic-hidden .link-glow {
  opacity: 0;
  animation: none;
}

.map-grid {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, rgba(255,255,255,0.72), transparent 18%, transparent 82%, rgba(255,255,255,0.72)),
    linear-gradient(180deg, rgba(255,255,255,0.7), transparent 20%, transparent 80%, rgba(255,255,255,0.6));
  z-index: 1;
}

.link-layer {
  position: absolute;
  inset: 0;
  z-index: 2;
  width: 100%;
  height: 100%;
}

.link-layer g {
  transition: opacity 0.16s ease;
}

.link-hit {
  stroke: transparent;
  stroke-width: 14;
  pointer-events: stroke;
  vector-effect: non-scaling-stroke;
}

.link-main,
.link-glow,
.link-sheen,
.link-flow {
  stroke-linecap: round;
  vector-effect: non-scaling-stroke;
  transition: opacity 0.16s ease, stroke 0.16s ease, stroke-width 0.16s ease;
}

.link-main {
  stroke: rgba(71, 100, 132, 0.44);
  stroke-width: 0.52;
  opacity: 0.92;
}

.link-glow {
  stroke: rgba(94, 167, 255, 0.28);
  stroke-width: 1.8;
  opacity: 0.08;
}

.link-sheen {
  stroke: rgba(255, 255, 255, 0.68);
  stroke-width: 0.12;
  opacity: 0.22;
}

/* ── Animated flow layer (circular dots via dash 0 + round linecap) ── */
.link-flow {
  stroke: rgba(56, 189, 248, 0.88);
  stroke-width: 2;
  stroke-dasharray: 0.1 18;
  stroke-dashoffset: 0;
  opacity: 0.78;
  animation: linkDataFlow 1.4s linear infinite;
}

/* ── Link hover ─────────────────────────────────────────────────────────── */
.hover .link-main {
  stroke: rgba(56, 189, 248, 0.9);
  stroke-width: 1.2;
  opacity: 1;
}
.hover .link-glow {
  stroke: rgba(56, 189, 248, 0.45);
  stroke-width: 3.5;
  stroke-dasharray: 0.1 18;
  opacity: 0.28;
  animation: linkFlowSlow 2s linear infinite;
}
.hover .link-sheen {
  stroke: rgba(255, 255, 255, 0.9);
  stroke-width: 1.4;
  stroke-dasharray: 0.1 10;
  opacity: 0.52;
  animation: linkDataFlow 0.6s linear infinite;
}
.hover .link-flow {
  stroke: rgba(56, 189, 248, 1);
  stroke-width: 2.5;
  stroke-dasharray: 0.1 12;
  opacity: 1;
  animation-duration: 0.45s;
}

/* ── Primary (healthy) — 3-layer dot-stream ──────────────────────────── */
.primary .link-main {
  stroke: rgba(71, 100, 132, 0.48);
}

/* Sparse large glow-dots in background, slow drift */
.primary .link-glow {
  stroke: rgba(56, 189, 248, 0.42);
  stroke-width: 2.2;
  stroke-dasharray: 0.1 32;
  stroke-dashoffset: 0;
  opacity: 0.14;
  animation: linkFlowSlow 2.8s linear infinite;
  animation-delay: -0.6s;
}

/* Dense small white sparkle-dots */
.primary .link-sheen {
  stroke: rgba(255, 255, 255, 0.78);
  stroke-width: 1.2;
  stroke-dasharray: 0.1 10;
  stroke-dashoffset: 0;
  opacity: 0.3;
  animation: linkDataFlow 0.85s linear infinite;
  animation-delay: -0.2s;
}

/* Medium blue dot-stream — main visible flow */
.primary .link-flow {
  stroke: rgba(56, 189, 248, 0.92);
  stroke-width: 2;
  stroke-dasharray: 0.1 18;
  stroke-dashoffset: 0;
  opacity: 0.78;
  animation: linkDataFlow 1.4s linear infinite;
}

/* ── Degraded — amber slow-flow dots ──────────────────────────────────── */
.degraded .link-main {
  stroke: rgba(184, 124, 38, 0.7);
  stroke-dasharray: none;
}

.degraded .link-glow {
  stroke: rgba(240, 185, 76, 0.3);
  stroke-width: 2;
  stroke-dasharray: 0.1 36;
  stroke-dashoffset: 0;
  opacity: 0.12;
  animation: linkFlowSlow 3.2s linear infinite;
}

.degraded .link-sheen {
  opacity: 0;
}

.degraded .link-flow {
  stroke: rgba(240, 185, 76, 0.82);
  stroke-width: 1.8;
  stroke-dasharray: 0.1 22;
  opacity: 0.56;
  animation: linkDataFlow 2.4s linear infinite;
}

.failed .link-main,
.attack .link-main {
  stroke: rgba(188, 56, 70, 0.72);
  stroke-dasharray: none;
  animation: linkFaultBlink 0.95s ease-in-out infinite;
}

.failed .link-glow,
.attack .link-glow {
  stroke: rgba(239, 99, 116, 0.26);
  opacity: 0.09;
}

.failed .link-sheen,
.attack .link-sheen {
  opacity: 0;
}

.failed .link-flow,
.attack .link-flow {
  opacity: 0;
  animation: none;
}

.active .link-main {
  stroke: rgba(39, 98, 184, 0.86);
  stroke-width: 0.82;
  filter: url(#active-link-glow);
}

.active .link-glow {
  stroke: rgba(94, 167, 255, 0.45);
  stroke-width: 3;
  stroke-dasharray: 0.1 22;
  opacity: 0.22;
  animation: linkFlowSlow 2s linear infinite;
}

.active .link-sheen {
  stroke: rgba(255, 255, 255, 0.88);
  stroke-width: 1.4;
  stroke-dasharray: 0.1 8;
  opacity: 0.46;
  animation: linkDataFlow 0.6s linear infinite;
}

.active .link-flow {
  stroke-width: 2.5;
  stroke-dasharray: 0.1 14;
  opacity: 0.95;
  animation-duration: 0.6s;
}

.active.degraded .link-main {
  stroke: rgba(184, 124, 38, 0.86);
}

.active.degraded .link-glow {
  stroke: rgba(240, 185, 76, 0.4);
  stroke-width: 2.4;
  stroke-dasharray: 0.1 30;
  opacity: 0.18;
  animation: linkFlowSlow 2.6s linear infinite;
}

.active.degraded .link-sheen {
  opacity: 0;
}

.active.degraded .link-flow {
  stroke-width: 2;
  stroke-dasharray: 0.1 18;
  opacity: 0.76;
  animation-duration: 1.6s;
}

.active.failed .link-main,
.active.attack .link-main {
  stroke: rgba(188, 56, 70, 0.88);
}

.active.failed .link-glow,
.active.attack .link-glow {
  stroke: rgba(239, 99, 116, 0.34);
}

.active.failed .link-sheen,
.active.attack .link-sheen {
  opacity: 0;
}

.active.failed .link-flow,
.active.attack .link-flow {
  opacity: 0;
  animation: none;
}

.dimmed {
  opacity: 0.5;
}

.topology-node {
  --node-accent: #5ea7ff;
  --node-accent-soft: #dbeafe;
  --node-accent-rgb: 94, 167, 255;
  position: absolute;
  z-index: 5;
  width: 96px;
  height: 74px;
  will-change: transform;
  box-sizing: border-box;
  display: grid;
  place-items: center;
  transform: translate(-50%, -50%);
  padding: 0;
  border: 0;
  border-radius: 999px;
  color: inherit;
  background: transparent;
  box-shadow: none;
  font: inherit;
  isolation: isolate;
  text-align: center;
  cursor: pointer;
  transition: opacity 0.16s ease, transform 0.16s ease, filter 0.16s ease;
}

.topology-node:not(.agent)::before {
  content: '';
  position: absolute;
  left: 50%;
  top: 50%;
  z-index: -1;
  width: 88px;
  height: 88px;
  border-radius: 50%;
  pointer-events: none;
  transform: translate(-50%, -50%);
  background:
    radial-gradient(circle, rgba(var(--node-accent-rgb), 0.14) 0%, rgba(var(--node-accent-rgb), 0.055) 42%, transparent 72%);
  filter: blur(2px);
  opacity: 0.64;
  transition: opacity 0.18s ease, transform 0.18s ease, filter 0.18s ease;
}

.topology-node:hover,
.topology-node.active,
.topology-node.focused {
  transform: translate(-50%, -50%) scale(1.045);
  filter: drop-shadow(0 9px 18px rgba(var(--node-accent-rgb), 0.16));
}

.topology-node:hover::before,
.topology-node.active::before,
.topology-node.focused::before {
  opacity: 0.92;
  transform: translate(-50%, -50%) scale(1.08);
  filter: blur(1px);
}

.topology-node.related {
  opacity: 1;
}

.topology-node.dimmed {
  opacity: 0.34;
}

.topology-node.agent {
  min-width: 126px;
  max-width: none;
  width: 126px;
  height: 126px;
  display: grid;
  grid-template-columns: 1fr;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 999px;
  background: transparent;
  box-shadow: none;
}

.topology-node.agent:hover,
.topology-node.agent.active,
.topology-node.agent.focused {
  transform: translate(-50%, -50%) scale(1.035);
  border-color: transparent;
  box-shadow: none;
}

.topology-node.agent.dimmed {
  opacity: 0.42;
}

.agent-core-visual {
  position: relative;
  width: 106px;
  height: 106px;
  display: block;
  pointer-events: none;
  filter: drop-shadow(0 12px 24px rgba(56, 189, 248, 0.2));
  transition: transform 0.24s ease, filter 0.24s ease;
}

.agent-core-visual::before {
  content: '';
  position: absolute;
  inset: 19px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(186, 230, 253, 0.36), rgba(129, 140, 248, 0.1) 42%, transparent 70%);
  filter: blur(2px);
}

.topology-node.agent:hover .agent-core-visual,
.topology-node.agent.active .agent-core-visual,
.topology-node.agent.focused .agent-core-visual,
.topology-canvas.composing .topology-node.agent .agent-core-visual {
  transform: scale(1.06);
  filter: drop-shadow(0 14px 30px rgba(129, 140, 248, 0.26));
}

.agent-orbit,
.agent-wave,
.agent-core-ball {
  position: absolute;
  left: 50%;
  top: 50%;
  border-radius: 50%;
}

.agent-orbit {
  border: 1px dashed rgba(226, 232, 240, 0.56);
  transform: translate(-50%, -50%);
  animation: agentOrbit 24s linear infinite;
}

.agent-orbit.orbit-a {
  width: 98px;
  height: 98px;
  box-shadow: inset 0 0 24px rgba(56, 189, 248, 0.07);
}

.agent-orbit.orbit-b {
  width: 72px;
  height: 72px;
  border-style: solid;
  border-color: rgba(56, 189, 248, 0.18);
  border-top-color: rgba(56, 189, 248, 0.64);
  border-right-color: rgba(129, 140, 248, 0.48);
  animation-duration: 18s;
  animation-direction: reverse;
}

.agent-wave {
  width: 38px;
  height: 38px;
  border: 1px solid rgba(56, 189, 248, 0.28);
  background: radial-gradient(circle, rgba(129, 140, 248, 0.08), transparent 72%);
  transform: translate(-50%, -50%) scale(1);
  animation: agentWave 6.8s cubic-bezier(0.2, 0.6, 0.3, 1) infinite;
}

.agent-wave.wave-b {
  border-color: rgba(129, 140, 248, 0.24);
  animation-delay: 3.4s;
}

.agent-core-ball {
  width: 46px;
  height: 46px;
  display: grid;
  place-items: center;
  overflow: hidden;
  background: radial-gradient(circle at 30% 30%, #ffffff 0%, #bae6fd 30%, #818cf8 70%, #312e81 100%);
  box-shadow:
    0 0 26px rgba(56, 189, 248, 0.42),
    0 0 48px rgba(129, 140, 248, 0.24),
    inset -4px -4px 12px rgba(0, 0, 0, 0.18);
  transform: translate(-50%, -50%) scale(0.96);
  animation: agentCoreBreath 5.4s cubic-bezier(0.4, 0, 0.2, 1) infinite alternate;
}

.agent-core-ball i {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #ffffff;
  box-shadow: 0 0 16px #ffffff, 0 0 32px #bae6fd;
  filter: blur(1.5px);
  transform: translate(-4px, -4px);
}

.agent-core-label {
  position: absolute;
  left: 50%;
  top: calc(50% + 48px);
  width: max-content;
  max-width: 160px;
  transform: translateX(-50%);
  pointer-events: none;
  text-align: center;
  white-space: nowrap;
}

.agent-core-label strong,
.agent-core-label small {
  display: block;
}

.agent-core-label strong {
  color: #0f172a;
  font-size: 0.8rem;
  font-weight: 950;
  line-height: 1.1;
  text-shadow:
    0 1px 0 rgba(255, 255, 255, 0.92),
    0 10px 22px rgba(15, 23, 42, 0.12);
}

.agent-core-label small {
  display: none;
}

.topology-node.agent.failed .agent-core-ball,
.topology-node.agent.attack .agent-core-ball {
  background: radial-gradient(circle at 30% 30%, #ffffff 0%, #fecaca 30%, #fb7185 68%, #7f1d1d 100%);
  box-shadow:
    0 0 24px rgba(239, 68, 68, 0.44),
    0 0 46px rgba(248, 113, 113, 0.24),
    inset -4px -4px 12px rgba(127, 29, 29, 0.22);
}

.topology-node.agent.degraded .agent-core-ball {
  background: radial-gradient(circle at 30% 30%, #ffffff 0%, #bae6fd 30%, #818cf8 70%, #312e81 100%);
  box-shadow:
    0 0 28px rgba(56, 189, 248, 0.48),
    0 0 50px rgba(129, 140, 248, 0.28),
    inset -4px -4px 12px rgba(0, 0, 0, 0.18);
}

.device-node {
  position: relative;
  width: 38px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(var(--node-accent-rgb), 0.5);
  border-radius: 7px;
  color: var(--node-accent);
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(248, 250, 252, 0.88)),
    rgba(var(--node-accent-rgb), 0.05);
  box-shadow:
    0 6px 14px rgba(15, 23, 42, 0.1),
    inset 0 1px 0 rgba(255, 255, 255, 0.98);
  pointer-events: none;
  transition: border-color 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
}

.device-node svg {
  width: 22px;
  height: 22px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.device-node i {
  position: absolute;
  right: -3px;
  top: -3px;
  width: 8px;
  height: 8px;
  border: 2px solid #ffffff;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.18);
}

.topology-node.host .device-node {
  width: 34px;
  height: 34px;
  border-radius: 50%;
}

.topology-node.degraded .device-node i {
  background: #f59e0b;
}

.topology-node.failed .device-node i,
.topology-node.attack .device-node i {
  background: #ef4444;
}

.topology-node:hover .device-node,
.topology-node.active .device-node,
.topology-node.focused .device-node {
  border-color: rgba(var(--node-accent-rgb), 0.8);
  background:
    linear-gradient(180deg, #ffffff, rgba(var(--node-accent-rgb), 0.08)),
    #ffffff;
  box-shadow:
    0 8px 18px rgba(15, 23, 42, 0.12),
    0 0 0 4px rgba(var(--node-accent-rgb), 0.08);
}

.star-ring,
.star-dot {
  position: absolute;
  left: 50%;
  top: 50%;
  border-radius: 999px;
  transform: translate(-50%, -50%);
  pointer-events: none;
}

.star-ring {
  width: 26px;
  height: 26px;
  border: 1px solid rgba(var(--node-accent-rgb), 0.22);
  background:
    radial-gradient(circle, rgba(var(--node-accent-rgb), 0.12), transparent 62%),
    linear-gradient(180deg, rgba(255, 255, 255, 0.52), rgba(255, 255, 255, 0.18));
  box-shadow:
    0 0 0 5px rgba(var(--node-accent-rgb), 0.04),
    0 0 16px rgba(var(--node-accent-rgb), 0.1);
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease, opacity 0.16s ease;
}

.star-ring::before {
  content: '';
  position: absolute;
  inset: 6px;
  border-radius: inherit;
  background: rgba(var(--node-accent-rgb), 0.1);
  opacity: 1;
}

.star-ring::after {
  content: '';
  position: absolute;
  inset: -4px;
  border-radius: inherit;
  border: 1px solid rgba(var(--node-accent-rgb), 0.08);
  opacity: 0;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.star-dot {
  width: 10px;
  height: 10px;
  background:
    radial-gradient(circle at 38% 32%, rgba(255, 255, 255, 0.72), transparent 23%),
    linear-gradient(145deg, var(--node-accent-soft), var(--node-accent) 58%, rgba(15, 23, 42, 0.16));
  border: 2px solid rgba(255, 255, 255, 0.82);
  box-shadow:
    0 1px 4px rgba(15, 23, 42, 0.12),
    0 0 11px rgba(var(--node-accent-rgb), 0.3);
}

.star-dot::after {
  content: none;
}

.topology-node.switch {
  --node-accent: #8b8cff;
  --node-accent-soft: #e0e7ff;
  --node-accent-rgb: 139, 140, 255;
}

.topology-node.host {
  --node-accent: #24c6b8;
  --node-accent-soft: #ccfbf1;
  --node-accent-rgb: 36, 198, 184;
}

.topology-node.degraded {
  --node-accent: #f0b94c;
  --node-accent-soft: #fef3c7;
  --node-accent-rgb: 240, 185, 76;
}

.topology-node.failed,
.topology-node.attack {
  --node-accent: #ef6374;
  --node-accent-soft: #ffe4e6;
  --node-accent-rgb: 239, 99, 116;
}

.topology-node:hover .star-ring,
.topology-node.active .star-ring,
.topology-node.focused .star-ring {
  transform: translate(-50%, -50%) scale(1.14);
  border-color: rgba(var(--node-accent-rgb), 0.38);
  box-shadow:
    0 0 0 7px rgba(var(--node-accent-rgb), 0.06),
    0 0 22px rgba(var(--node-accent-rgb), 0.16);
}

.topology-node:hover .star-ring::after,
.topology-node.active .star-ring::after,
.topology-node.focused .star-ring::after {
  opacity: 1;
  transform: scale(1.08);
}

.topology-node.degraded:hover .star-ring,
.topology-node.degraded.active .star-ring,
.topology-node.degraded.focused .star-ring {
  border-color: rgba(var(--node-accent-rgb), 0.38);
  box-shadow:
    0 0 0 7px rgba(var(--node-accent-rgb), 0.06),
    0 0 22px rgba(var(--node-accent-rgb), 0.16);
}

.topology-node.failed:hover .star-ring,
.topology-node.failed.active .star-ring,
.topology-node.failed.focused .star-ring,
.topology-node.attack:hover .star-ring,
.topology-node.attack.active .star-ring,
.topology-node.attack.focused .star-ring {
  border-color: rgba(var(--node-accent-rgb), 0.38);
  box-shadow:
    0 0 0 7px rgba(var(--node-accent-rgb), 0.06),
    0 0 22px rgba(var(--node-accent-rgb), 0.16);
}

.star-label {
  position: absolute;
  left: 50%;
  top: calc(50% + 23px);
  width: max-content;
  max-width: 150px;
  transform: translateX(-50%);
  pointer-events: none;
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.star-label strong,
.star-label small {
  display: block;
  overflow: hidden;
  white-space: normal;
}

.star-label strong {
  color: #1e293b;
  font-size: 0.7rem;
  font-weight: 850;
  line-height: 1.15;
  text-shadow:
    0 1px 0 rgba(255, 255, 255, 0.9),
    0 8px 18px rgba(15, 23, 42, 0.12);
}

.star-label small {
  display: none;
}

.topology-node:hover .star-label,
.topology-node.active .star-label,
.topology-node.focused .star-label {
  opacity: 1;
  transform: translateX(-50%) translateY(-2px);
}

.device-symbol {
  position: relative;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  color: #0284c7;
  background: rgba(224, 242, 254, 0.78);
}

.topology-node.switch .device-symbol {
  color: #4f46e5;
  background: rgba(238, 242, 255, 0.82);
}

.topology-node.host .device-symbol {
  color: #0284c7;
  background: rgba(224, 242, 254, 0.82);
}

.topology-node.agent .device-symbol {
  color: #7c3aed;
  background: rgba(245, 243, 255, 0.84);
}

.device-symbol svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.device-text {
  min-width: 0;
}

.device-text strong,
.device-text small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.device-text strong {
  color: #0f172a;
  font-size: 0.78rem;
  line-height: 1.1;
}

.device-text small {
  margin-top: 4px;
  color: #64748b;
  font-size: 0.62rem;
  font-weight: 760;
}

.status-dot {
  position: absolute;
  right: 7px;
  top: 7px;
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: #22c55e;
  box-shadow: 0 0 10px rgba(34, 197, 94, 0.72);
}

.topology-node.degraded .status-dot {
  background: #f59e0b;
  box-shadow: 0 0 10px rgba(245, 158, 11, 0.72);
}

.topology-node.failed .status-dot,
.topology-node.attack .status-dot {
  background: #ef4444;
  box-shadow: 0 0 10px rgba(239, 68, 68, 0.72);
}

@keyframes agentOrbit {
  100% {
    transform: translate(-50%, -50%) rotate(360deg);
  }
}

@keyframes linkDataFlow {
  100% {
    stroke-dashoffset: -36;
  }
}

@keyframes linkFlowSlow {
  100% {
    stroke-dashoffset: -64;
  }
}

@keyframes linkFaultBlink {
  0%,
  100% {
    opacity: 0.32;
  }
  50% {
    opacity: 0.92;
  }
}

@keyframes agentWave {
  0% {
    opacity: 0;
    transform: translate(-50%, -50%) scale(0.92);
  }
  16% {
    opacity: 0.42;
  }
  100% {
    opacity: 0;
    transform: translate(-50%, -50%) scale(2.15);
  }
}

@keyframes agentCoreBreath {
  0% {
    transform: translate(-50%, -50%) scale(0.94);
    box-shadow:
      0 0 20px rgba(56, 189, 248, 0.34),
      0 0 38px rgba(129, 140, 248, 0.2),
      inset -4px -4px 12px rgba(0, 0, 0, 0.18);
  }
  100% {
    transform: translate(-50%, -50%) scale(1.04);
    box-shadow:
      0 0 30px rgba(129, 140, 248, 0.46),
      0 0 56px rgba(56, 189, 248, 0.26),
      inset -4px -4px 12px rgba(0, 0, 0, 0.18);
  }
}

@keyframes detailPop {
  0% {
    opacity: 0;
    filter: blur(4px);
  }
  100% {
    opacity: 1;
    filter: blur(0);
  }
}

@keyframes tooltipRise {
  0% {
    opacity: 0;
    filter: blur(3px);
  }
  100% {
    opacity: 1;
    filter: blur(0);
  }
}

.inspector-heading {
  position: relative;
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) 24px;
  align-items: start;
  gap: 10px;
  padding-bottom: 14px;
  border-bottom: 1px solid rgba(203, 213, 225, 0.66);
}

.inspector-heading > div:nth-child(2) {
  min-width: 0;
}

.inspector-heading strong,
.inspector-heading small {
  display: block;
}

.inspector-heading strong {
  margin-top: 4px;
  color: #0f172a;
  font-size: 0.86rem;
  font-weight: 900;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inspector-heading small {
  margin-top: 4px;
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.56rem;
  font-weight: 760;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inspector-icon {
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(79, 70, 229, 0.24);
  border-radius: 7px;
  color: #4f46e5;
  background: rgba(238, 242, 255, 0.78);
}

.inspector-icon.host {
  color: #0284c7;
  border-color: rgba(2, 132, 199, 0.24);
  background: rgba(224, 242, 254, 0.72);
}

.inspector-icon.agent {
  color: #7c3aed;
  border-color: rgba(124, 58, 237, 0.22);
  background: rgba(245, 243, 255, 0.76);
}

.inspector-icon.link {
  color: #0f766e;
  border-color: rgba(13, 148, 136, 0.22);
  background: rgba(240, 253, 250, 0.78);
}

.inspector-icon svg {
  width: 22px;
  height: 22px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.75;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.inspector-close {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 5px;
  color: #64748b;
  background: transparent;
  cursor: pointer;
  font-size: 1rem;
}

.inspector-close:hover {
  color: #0f172a;
  background: #e2e8f0;
}

.inspector-state {
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 14px;
  padding: 0 2px;
}

.inspector-state span {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: #334155;
  font-size: 0.68rem;
  font-weight: 850;
}

.inspector-state strong {
  min-width: 0;
  color: #64748b;
  font-size: 0.62rem;
  font-weight: 800;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inspector-grid,
.summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0;
  margin: 8px 0 0;
  border-top: 1px solid rgba(203, 213, 225, 0.58);
}

.inspector-grid div,
.summary-grid div {
  min-width: 0;
  padding: 10px 8px 10px 2px;
  border-bottom: 1px solid rgba(226, 232, 240, 0.86);
}

.inspector-grid div:nth-child(even),
.summary-grid div:nth-child(even) {
  padding-left: 10px;
  border-left: 1px solid rgba(226, 232, 240, 0.86);
}

.inspector-grid dt,
.summary-grid dt {
  color: #94a3b8;
  font-size: 0.56rem;
  font-weight: 850;
}

.inspector-grid dd,
.summary-grid dd {
  margin: 5px 0 0;
  color: #1e293b;
  font-size: 0.68rem;
  font-weight: 850;
  line-height: 1.3;
  overflow-wrap: anywhere;
}

.inspector-section {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid rgba(203, 213, 225, 0.58);
}

.inspector-section-title {
  display: block;
  color: #475569;
  font-size: 0.62rem;
  font-weight: 900;
}

.inspector-section p {
  margin: 7px 0 0;
  color: #64748b;
  font-family: inherit;
  font-size: 0.66rem;
  font-weight: 700;
  letter-spacing: 0;
  line-height: 1.55;
  text-transform: none;
}

.port-list {
  display: grid;
  gap: 4px;
  margin-top: 8px;
}

.port-list div {
  min-width: 0;
  min-height: 28px;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) 8px;
  align-items: center;
  gap: 7px;
  padding: 0 6px;
  background: rgba(241, 245, 249, 0.72);
}

.port-list code {
  color: #0369a1;
  font-size: 0.58rem;
  font-weight: 850;
}

.port-list span {
  color: #475569;
  font-size: 0.6rem;
  font-weight: 760;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inspector-actions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 18px;
}

.inspector-actions button {
  height: 34px;
  border: 1px solid rgba(14, 165, 233, 0.34);
  border-radius: 6px;
  color: #0369a1;
  background: rgba(240, 249, 255, 0.72);
  cursor: pointer;
  font-size: 0.68rem;
  font-weight: 850;
  transition: color 0.16s ease, border-color 0.16s ease, background 0.16s ease;
}

.inspector-actions button:hover {
  color: #ffffff;
  border-color: #0284c7;
  background: #0284c7;
}

.summary-health {
  min-height: 42px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 2px 10px;
  border-bottom: 1px solid rgba(203, 213, 225, 0.66);
}

.summary-health span {
  color: #64748b;
  font-size: 0.65rem;
  font-weight: 800;
}

.summary-health strong {
  color: #047857;
  font-size: 0.72rem;
  font-weight: 900;
}

.topology-toolbar {
  position: absolute;
  left: 14px;
  bottom: 14px;
  z-index: 12;
  display: flex;
  gap: 2px;
  padding: 4px;
  border: 1px solid rgba(203, 213, 225, 0.68);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.86);
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.08);
  backdrop-filter: blur(10px);
}

.topology-toolbar button {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 4px;
  color: #334155;
  background: transparent;
  cursor: pointer;
}

.topology-toolbar button:hover {
  color: #0369a1;
  background: rgba(224, 242, 254, 0.78);
}

.topology-toolbar svg {
  width: 17px;
  height: 17px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.9;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.search-wrap { display: flex; align-items: center; gap: 4px; }
.search-input { width: 130px; height: 28px; padding: 0 6px; border: 1px solid rgba(203,213,225,0.68); border-radius: 8px; color: #0f172a; background: rgba(255,255,255,0.78); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.7rem; font-weight: 700; outline: none; }
.search-input:focus { border-color: rgba(56,189,248,0.48); }
.search-input::placeholder { color: #94a3b8; }
.search-count { padding: 2px 6px; border-radius: 999px; color: #0284c7; background: rgba(224,242,254,0.78); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.6rem; font-weight: 900; white-space: nowrap; }
.search-clear-btn { width: 18px; height: 18px; display: grid; place-items: center; border: 0; border-radius: 999px; color: #64748b; background: rgba(241,245,249,0.78); cursor: pointer; font-size: 0.74rem; line-height: 1; }
.search-clear-btn:hover { color: #0f172a; background: rgba(226,232,240,0.82); }

.map-status-strip {
  position: absolute;
  right: 14px;
  top: 14px;
  z-index: 12;
  display: flex;
  gap: 8px;
  pointer-events: none;
}

.map-status-strip span {
  padding: 6px 9px;
  border: 1px solid rgba(203, 213, 225, 0.62);
  border-radius: 5px;
  color: #475569;
  background: rgba(255, 255, 255, 0.78);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.62rem;
  font-weight: 850;
}

.node-detail-card {
  position: absolute;
  z-index: 18;
  width: 344px;
  box-sizing: border-box;
  transform: translate(24px, -50%);
  padding: 16px;
  border: 1px solid rgba(148, 163, 184, 0.42);
  border-radius: 20px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.94), rgba(248, 250, 252, 0.78)),
    radial-gradient(circle at 10% 0%, rgba(56, 189, 248, 0.16), transparent 44%),
    radial-gradient(circle at 100% 100%, rgba(99, 102, 241, 0.1), transparent 52%);
  box-shadow:
    0 22px 52px rgba(15, 23, 42, 0.14),
    0 0 0 1px rgba(255, 255, 255, 0.72) inset;
  backdrop-filter: blur(20px) saturate(1.12);
  animation: detailPop 0.18s ease-out;
}

.node-detail-card.reverse {
  transform: translate(calc(-100% - 24px), -50%);
}

.node-detail-card button {
  position: absolute;
  right: 10px;
  top: 10px;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(203, 213, 225, 0.68);
  border-radius: 999px;
  color: #64748b;
  background: rgba(255, 255, 255, 0.68);
  cursor: pointer;
}

.node-detail-card button:hover {
  color: #0f172a;
  border-color: rgba(56, 189, 248, 0.58);
  background: rgba(224, 242, 254, 0.72);
}

.node-detail-card button svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
}

.detail-topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-right: 30px;
}

.detail-topline p {
  margin: 0;
  color: #2563eb;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.64rem;
  font-weight: 950;
  letter-spacing: 1.2px;
  text-transform: uppercase;
}

.detail-state {
  min-width: 66px;
  padding: 4px 8px;
  border: 1px solid rgba(148, 163, 184, 0.36);
  border-radius: 999px;
  color: #2563eb;
  background: rgba(239, 246, 255, 0.76);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.62rem;
  font-weight: 950;
  text-align: center;
}

.detail-state.normal {
  color: #047857;
  background: rgba(236, 253, 245, 0.74);
  border-color: rgba(16, 185, 129, 0.24);
}

.detail-state.degraded {
  color: #b45309;
  background: rgba(255, 251, 235, 0.78);
  border-color: rgba(245, 158, 11, 0.28);
}

.detail-state.failed,
.detail-state.attack {
  color: #b91c1c;
  background: rgba(254, 242, 242, 0.78);
  border-color: rgba(239, 68, 68, 0.26);
}

.node-detail-card h3 {
  margin: 8px 34px 8px 0;
  color: #0f172a;
  font-size: 1.18rem;
  font-weight: 900;
  line-height: 1.15;
}

.detail-desc {
  display: block;
  margin: 0 0 12px;
  color: #475569;
  font-size: 0.75rem;
  font-weight: 650;
  line-height: 1.55;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.detail-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 7px;
  margin: 10px 0 2px;
  padding: 8px;
  border: 1px solid rgba(186, 230, 253, 0.52);
  border-radius: 16px;
  background:
    linear-gradient(135deg, rgba(239, 246, 255, 0.56), rgba(255, 255, 255, 0.42)),
    radial-gradient(circle at 18% 0%, rgba(56, 189, 248, 0.12), transparent 54%);
}

.detail-metrics div {
  min-width: 0;
  padding: 8px 7px;
  border: 1px solid rgba(203, 213, 225, 0.46);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.62);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.74);
}

.detail-metrics div.ok {
  border-color: rgba(16, 185, 129, 0.24);
  background: rgba(240, 253, 250, 0.66);
}

.detail-metrics div.warn {
  border-color: rgba(245, 158, 11, 0.26);
  background: rgba(255, 251, 235, 0.7);
}

.detail-metrics div.unavailable,
.detail-metrics div.idle {
  background: rgba(248, 250, 252, 0.62);
}

.detail-metrics span {
  display: block;
  color: #64748b;
  font-size: 0.56rem;
  font-weight: 900;
  letter-spacing: 0.05em;
}

.detail-metrics strong {
  display: block;
  margin-top: 5px;
  color: #0f172a;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.7rem;
  font-weight: 950;
  line-height: 1.15;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-grid div,
.detail-row {
  min-width: 0;
  box-sizing: border-box;
  padding: 10px 11px;
  border: 1px solid rgba(203, 213, 225, 0.54);
  border-radius: 14px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.62), rgba(248, 250, 252, 0.42));
}

.detail-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
}

.detail-grid span,
.detail-row span {
  display: block;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.58rem;
  font-weight: 900;
  letter-spacing: 0.11em;
}

.detail-grid strong,
.detail-row strong {
  display: block;
  min-width: 0;
  margin-top: 5px;
  color: #0f172a;
  font-size: 0.76rem;
  font-weight: 900;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-row strong {
  margin-top: 0;
  text-align: right;
  white-space: normal;
}

.detail-row.compact strong {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.68rem;
  color: #334155;
}

.node-tooltip {
  position: absolute;
  z-index: 16;
  width: 214px;
  box-sizing: border-box;
  transform: translate(16px, -112%);
  padding: 11px 12px 12px;
  border: 1px solid rgba(148, 163, 184, 0.38);
  border-radius: 16px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.9), rgba(248, 250, 252, 0.72)),
    radial-gradient(circle at 8% 0%, rgba(56, 189, 248, 0.12), transparent 48%);
  box-shadow:
    0 18px 38px rgba(15, 23, 42, 0.12),
    0 0 0 1px rgba(255, 255, 255, 0.74) inset;
  backdrop-filter: blur(18px) saturate(1.1);
  pointer-events: none;
  animation: tooltipRise 0.14s ease-out;
}

.node-tooltip.reverse {
  transform: translate(calc(-100% - 16px), -112%);
}

.tooltip-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tooltip-head i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.11);
}

.tooltip-head i.degraded {
  background: #f59e0b;
  box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.12);
}

.tooltip-head i.failed,
.tooltip-head i.attack {
  background: #ef4444;
  box-shadow: 0 0 0 4px rgba(239, 68, 68, 0.12);
}

.tooltip-head strong {
  display: block;
  color: #0f172a;
  font-size: 0.88rem;
  font-weight: 900;
  letter-spacing: 0;
}

.node-tooltip p {
  margin: 7px 0 0;
  color: #64748b;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 0.72rem;
  font-weight: 700;
  line-height: 1.4;
  letter-spacing: 0;
  text-transform: none;
}

.tooltip-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
}

.tooltip-meta span,
.tooltip-meta em {
  min-width: 0;
  padding: 4px 8px;
  border-radius: 999px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.62rem;
  font-weight: 900;
}

.tooltip-meta span {
  color: #2563eb;
  background: rgba(239, 246, 255, 0.78);
}

.tooltip-meta em {
  color: #334155;
  background: rgba(241, 245, 249, 0.78);
  font-style: normal;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.legend-strip {
  position: absolute;
  left: 22px;
  right: 22px;
  bottom: 18px;
  z-index: 12;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  border: 1px solid rgba(203, 213, 225, 0.58);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.62);
  box-shadow: 0 14px 28px rgba(15, 23, 42, 0.06);
  backdrop-filter: blur(10px);
}

.legend-strip span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #475569;
  font-size: 0.68rem;
  font-weight: 900;
}

.legend-strip span::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 999px;
}

.legend-strip .safe::before {
  background: #2563eb;
  box-shadow: 0 0 10px rgba(37, 99, 235, 0.5);
}

.legend-strip .warn::before {
  background: #f59e0b;
  box-shadow: 0 0 10px rgba(245, 158, 11, 0.62);
}

.legend-strip .danger::before {
  background: #ef4444;
  box-shadow: 0 0 10px rgba(239, 68, 68, 0.62);
}

@media (max-width: 1180px) {
  .topology-workbench {
    grid-template-columns: 184px minmax(360px, 1fr) 238px;
  }

  .topology-filter-rail {
    padding-inline: 10px;
  }

  .topology-inspector {
    padding-inline: 12px;
  }

  .rail-legend {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 900px) {
  .topology-workbench {
    grid-template-columns: 156px minmax(340px, 1fr);
  }

  .topology-inspector {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 236px;
    border-left: 1px solid rgba(148, 163, 184, 0.5);
    box-shadow: -14px 0 34px rgba(15, 23, 42, 0.1);
  }

  .topology-inspector:has(.rail-heading) {
    display: none;
  }
}

@media (max-width: 680px) {
  .topology-workbench {
    grid-template-columns: minmax(0, 1fr);
  }

  .topology-filter-rail {
    display: none;
  }

  .topology-canvas {
    min-height: 420px;
  }
}
</style>
