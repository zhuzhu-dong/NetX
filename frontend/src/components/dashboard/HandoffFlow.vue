<template>
  <div v-if="groupedEdges.length" class="handoff-flow">
    <div class="handoff-head">
      <span>任务交接链路</span>
      <small>{{ groupedEdges.length }} 条链路 · {{ validEdges.length }} 次交接</small>
    </div>

    <div class="handoff-grid">
      <div v-for="edge in visibleEdges" :key="`${edge.source}-${edge.target}`" class="handoff-edge">
        <span class="handoff-source" :title="edge.source">{{ edge.source }}</span>
        <span class="handoff-route" aria-hidden="true"></span>
        <span class="handoff-target" :title="edge.target">{{ edge.target }}</span>
        <em v-if="edge.count > 1">x{{ edge.count }}</em>
      </div>
    </div>

    <details v-if="hiddenEdges.length" class="handoff-more">
      <summary>展开其余 {{ hiddenEdges.length }} 条链路</summary>
      <div class="handoff-grid handoff-grid-more">
        <div v-for="edge in hiddenEdges" :key="`${edge.source}-${edge.target}`" class="handoff-edge">
          <span class="handoff-source" :title="edge.source">{{ edge.source }}</span>
          <span class="handoff-route" aria-hidden="true"></span>
          <span class="handoff-target" :title="edge.target">{{ edge.target }}</span>
          <em v-if="edge.count > 1">x{{ edge.count }}</em>
        </div>
      </div>
    </details>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  edges: { type: Array, default: () => [] },
})

const validEdges = computed(() => props.edges
  .map((edge) => ({
    ...edge,
    source: edge.source || edge.source_agent,
    target: edge.target || edge.target_agent,
  }))
  .filter((edge) => edge.source && edge.target && edge.source !== edge.target))

const groupedEdges = computed(() => {
  const groups = new Map()
  validEdges.value.forEach((edge) => {
    const key = `${edge.source}->${edge.target}`
    const current = groups.get(key)
    if (current) current.count += 1
    else groups.set(key, { ...edge, count: 1 })
  })
  return [...groups.values()].sort((a, b) => b.count - a.count)
})

const visibleEdges = computed(() => groupedEdges.value.slice(0, 8))
const hiddenEdges = computed(() => groupedEdges.value.slice(8))
</script>

<style scoped>
.handoff-flow { min-width:0; padding-top:12px; border-top:1px solid rgba(122,153,184,.18); }
.handoff-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:9px; }
.handoff-head > span { color:#334155; font-size:.7rem; font-weight:900; }
.handoff-head small { color:#7b8ba1; font-size:.61rem; font-weight:750; }
.handoff-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:7px; }
.handoff-edge { display:grid; grid-template-columns:minmax(0,1fr) 34px minmax(0,1fr) auto; align-items:center; min-width:0; min-height:34px; padding:5px 8px; border:1px solid rgba(132,164,194,.2); border-radius:5px; background:rgba(248,251,254,.74); }
.handoff-source,.handoff-target { overflow:hidden; color:#24324a; font:800 .61rem/1.2 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
.handoff-target { color:#5265b4; text-align:right; }
.handoff-route { position:relative; height:1px; margin:0 6px; background:linear-gradient(90deg,#52b6df,#7d91ed); }
.handoff-route::after { content:''; position:absolute; top:-3px; right:-1px; border-top:3px solid transparent; border-bottom:3px solid transparent; border-left:5px solid #7d91ed; }
.handoff-edge em { margin-left:6px; padding:2px 5px; border-radius:4px; color:#0879b8; background:#e8f6fc; font-size:.56rem; font-style:normal; font-weight:900; }
.handoff-more { margin-top:8px; }
.handoff-more summary { width:max-content; color:#1689ca; font-size:.62rem; font-weight:800; cursor:pointer; }
.handoff-grid-more { margin-top:8px; }
@media (max-width:1320px) { .handoff-grid { grid-template-columns:repeat(3,minmax(0,1fr)); } }
@media (max-width:900px) { .handoff-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media (max-width:560px) { .handoff-grid { grid-template-columns:1fr; } }
</style>
