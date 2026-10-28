<template>
  <div class="chart-card" :style="{ height: height + 'px' }">
    <div v-if="title" class="chart-card__head">
      <p class="chart-card__title">{{ title }}</p>
      <span>外圈表示该指标量程上限</span>
    </div>
    <div v-if="!option" class="chart-card__empty">等待数据…</div>
    <VChart
      v-else
      class="chart-card__canvas"
      :option="option"
      :autoresize="true"
      :style="{ height: (title ? height - 38 : height) + 'px' }"
      @click="onClick"
    />
  </div>
</template>

<script setup>
import VChart from 'vue-echarts'
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: null },
  title: { type: String, default: '' },
  height: { type: Number, default: 280 },
})

const emit = defineEmits(['chart-click'])

const option = computed(() => props.data)

function onClick(e) {
  emit('chart-click', e)
}
</script>

<style scoped>
.chart-card { min-width:0; overflow:hidden; }
.chart-card__head { display:flex; align-items:center; justify-content:space-between; gap:16px; height:38px; padding:0 14px; }
.chart-card__title { margin:0; color:#52637a; font:850 .67rem/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; letter-spacing:1px; text-transform:uppercase; }
.chart-card__head span { overflow:hidden; color:#91a0b2; font-size:.58rem; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
.chart-card__canvas { width:100%; min-width:0; }
.chart-card__empty { display:grid; place-items:center; height:calc(100% - 38px); color:#94a3b8; font-size:.72rem; font-weight:700; }
</style>
