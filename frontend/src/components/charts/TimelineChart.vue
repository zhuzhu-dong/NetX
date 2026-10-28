<template>
  <div class="chart-card" :style="{ height: height + 'px' }">
    <p v-if="title" class="chart-card__title">{{ title }}</p>
    <div v-if="!option" class="chart-card__empty">等待数据…</div>
    <VChart
      v-else
      :option="option"
      :autoresize="true"
      :style="{ height: (title ? height - 22 : height) + 'px' }"
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
  height: { type: Number, default: 260 },
})

const emit = defineEmits(['chart-click'])

const option = computed(() => props.data)

function onClick(e) {
  emit('chart-click', e)
}
</script>
