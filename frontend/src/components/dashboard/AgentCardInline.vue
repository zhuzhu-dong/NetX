<template>
  <div class="agent-card-inline" :class="{ active: active }">
    <span class="agent-id">{{ agent.agent_id }}</span>
    <span class="agent-role">{{ agent.role }}</span>
    <span class="agent-perm" :class="agent.permission">{{ agent.permission || 'read_only' }}</span>
  </div>
</template>

<script setup>
defineProps({
  agent: { type: Object, required: true },
  active: { type: Boolean, default: false },
})
</script>

<style scoped>
.agent-card-inline {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 4px 8px;
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid rgba(132, 164, 194, 0.18);
  border-radius: 6px;
  background: rgba(248, 251, 254, 0.78);
}

.agent-card-inline.active {
  border-color: rgba(14, 165, 233, 0.32);
  background: linear-gradient(135deg, rgba(235, 248, 254, 0.9), rgba(245, 247, 255, 0.82));
  box-shadow: inset 2px 0 0 #38bdf8;
}

.agent-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.68rem;
  font-weight: 800;
  color: #0f172a;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-role {
  grid-column: 1 / -1;
  min-width: 0;
  overflow: hidden;
  font-size: 0.65rem;
  font-weight: 700;
  color: #718096;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-perm {
  font-size: 0.6rem;
  font-weight: 800;
  padding: 2px 7px;
  max-width: 112px;
  overflow: hidden;
  border-radius: 4px;
  color: #0369a1;
  background: rgba(224, 242, 254, 0.7);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-perm.read_write {
  color: #b45309;
  background: rgba(254, 243, 199, 0.7);
}
</style>
