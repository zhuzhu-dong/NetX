/**
 * topologyStore.js — 拓扑状态与 SSE 订阅
 *
 * 从 network.js 拆分。职责：拓扑节点/链路、选中节点、路径高亮。
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import * as api from '../api/client'

export const useTopologyStore = defineStore('topology', () => {
  /* ---- state ---- */
  const topologyNodes = ref([])
  const topologyLinks = ref([])
  const topologySource = ref('connecting')
  const selectedNodeId = ref('')
  const _highlightPath = ref([])
  const topologyHighlightPath = computed(() => _highlightPath.value)

  let _topologySseCleanup = null

  /* ---- internal ---- */
  function _hasTopologyChanged(newNodes, newLinks) {
    const oldNodes = topologyNodes.value
    const oldLinks = topologyLinks.value
    if (oldNodes.length !== newNodes.length || oldLinks.length !== newLinks.length) return true
    // Compare node id + state (the two fields that affect rendering)
    for (let i = 0; i < newNodes.length; i++) {
      const old = oldNodes[i]
      const nw = newNodes[i]
      if (!old || old.id !== nw.id || old.state !== nw.state) return true
    }
    // Compare link id + state
    for (let i = 0; i < newLinks.length; i++) {
      const old = oldLinks[i]
      const nw = newLinks[i]
      if (!old || old.id !== nw.id || old.state !== nw.state) return true
    }
    return false
  }

  function _applyTopologyData(data) {
    const nodes = data.frontend?.nodes || []
    const links = data.frontend?.links || []
    if (nodes.length) {
      // Skip reactive update if topology hasn't actually changed
      if (!_hasTopologyChanged(nodes, links)) {
        topologySource.value = data.source || 'dynamic'
        return
      }
      topologyNodes.value = nodes
      topologyLinks.value = links
      topologySource.value = data.source || 'dynamic'
      // 只有当之前已选中了一个节点、但该节点在新拓扑中消失时，才自动选新节点
      if (selectedNodeId.value && !topologyNodes.value.some((node) => node.id === selectedNodeId.value)) {
        const preferred = topologyNodes.value.find((node) => node.type === 'switch') || topologyNodes.value[0]
        selectedNodeId.value = preferred?.id || ''
      }
    }
  }

  /* ---- actions ---- */
  async function loadTopology() {
    try {
      const data = await api.getTopology()
      _applyTopologyData(data)
      return data
    } catch (e) {
      console.error('拓扑加载失败:', e.message)
      return null
    }
  }

  function startTopologySSE() {
    if (_topologySseCleanup) return
    _topologySseCleanup = api.subscribeTopologyEvents({
      onUpdate(data) {
        _applyTopologyData(data)
      },
      onError(_e) {
        // SSE errors are expected on reconnection
      },
    })
  }

  function stopTopologySSE() {
    if (!_topologySseCleanup) return
    try { _topologySseCleanup() } catch (_) { /* ok */ }
    _topologySseCleanup = null
  }

  function selectNode(node) {
    if (!node) {
      selectedNodeId.value = ''
      return
    }
    selectedNodeId.value = node.id
  }

  function highlightTopologyPath(pathNodeIds) {
    _highlightPath.value = pathNodeIds || []
  }

  function clearTopologyHighlight() {
    _highlightPath.value = []
  }

  /* ---- cleanup ---- */
  function cleanup() {
    stopTopologySSE()
  }

  return {
    topologyNodes, topologyLinks, topologySource, selectedNodeId,
    topologyHighlightPath,
    loadTopology, startTopologySSE, stopTopologySSE,
    selectNode, highlightTopologyPath, clearTopologyHighlight,
    cleanup,
  }
})
