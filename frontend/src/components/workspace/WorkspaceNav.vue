<template>
  <nav class="workspace-nav">
    <RouterLink
      v-for="item in items"
      :key="item.to"
      :to="navTo(item)"
      :class="{ active: isActive(item) }"
    >
      <span>{{ item.index }}</span>
      {{ item.label }}
    </RouterLink>
  </nav>
</template>

<script setup>
import { useRoute } from 'vue-router'
import { useIntentStore } from '../../stores/intentStore'

const route = useRoute()
const intent = useIntentStore()
const items = [
  { index: '01', label: '网络态势', to: '/dashboard' },
  { index: '02', label: '意图输入', to: '/dashboard/intent' },
  { index: '03', label: '智能体执行', to: '/dashboard/run' },
  // { index: '04', label: '自愈与报告', to: '/dashboard/report' },
  { index: '05', label: '态势总览', to: '/dashboard/overview' },
]

function isActive(item) {
  if (item.to === '/dashboard') return route.path === '/dashboard' || route.path === '/dashboard/chat'
  if (item.to === '/dashboard/overview') return route.path === '/dashboard/overview'
  return route.path.startsWith(item.to)
}

function navTo(item) {
  const active = intent.activeIntent
  const executionIds = [
    ...(active?.runIds || []),
    ...Object.keys(active?.intentResults || {}),
  ].filter((id) => /^INT-/i.test(String(id || ''))).map(String)
  const intentId = route.query.intent_id
    || executionIds.at(-1)
    || (/^INT-/i.test(String(active?.id || '')) ? active.id : '')
  if ((item.to === '/dashboard/run' || item.to === '/dashboard/report') && intentId) {
    return { path: item.to, query: { intent_id: intentId } }
  }
  return item.to
}
</script>

<style scoped>
.workspace-nav {
  position: relative;
  display: flex;
  justify-content: center;
  gap: 6px;
  padding: 6px;
  border: 1px solid rgba(125, 211, 252, 0.34);
  border-radius: 999px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.82), rgba(240, 249, 255, 0.62)),
    radial-gradient(circle at 50% 0%, rgba(56, 189, 248, 0.16), transparent 52%);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9), 0 14px 30px rgba(14, 165, 233, 0.06);
  overflow: hidden;
}

.workspace-nav::before {
  content: '';
  position: absolute;
  left: 10px;
  right: 10px;
  top: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(56, 189, 248, 0.68), transparent);
}

a {
  position: relative;
  height: 38px;
  min-width: 116px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid transparent;
  border-radius: 999px;
  color: #64748b;
  background: transparent;
  text-decoration: none;
  font-size: 0.8rem;
  font-weight: 900;
  overflow: hidden;
  transition: color 0.2s ease, border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease;
}

a::after {
  content: '';
  position: absolute;
  left: 26px;
  right: 26px;
  bottom: 6px;
  height: 2px;
  border-radius: 999px;
  background: linear-gradient(90deg, rgba(56, 189, 248, 0.9), rgba(129, 140, 248, 0.88));
  opacity: 0;
  transform: scaleX(0.35);
  transition: opacity 0.2s ease, transform 0.2s ease;
}

a:hover,
a.active {
  color: #0f172a;
  border-color: rgba(56, 189, 248, 0.5);
  background:
    linear-gradient(135deg, rgba(224, 242, 254, 0.9), rgba(238, 242, 255, 0.86)),
    linear-gradient(90deg, rgba(56, 189, 248, 0.2), transparent);
  box-shadow: 0 10px 20px rgba(14, 165, 233, 0.1), inset 0 1px 0 rgba(255,255,255,0.92);
}

a:hover::after,
a.active::after {
  opacity: 1;
  transform: scaleX(1);
}

span {
  color: #0ea5e9;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.66rem;
}

@media (max-width: 1040px) {
  .workspace-nav {
    width: 100%;
    justify-content: flex-start;
    flex-wrap: wrap;
    border-radius: 18px;
  }

  a {
    flex: 1 1 42%;
  }
}
</style>
