<template>
  <section class="intent-queue">
    <div class="queue-head">
      <h2>近期意图</h2>
      <span>{{ intents.length }} active</span>
    </div>

    <div class="intent-list">
      <!-- ── 用户意图 ── -->
      <button
        class="section-header"
        type="button"
        aria-expanded="true"
        aria-controls="iq-user-items"
      >
        <svg class="section-chevron open" viewBox="0 0 24 24" aria-hidden="true">
          <path d="m9 6 6 6-6 6" />
        </svg>
        <span class="section-label">用户意图</span>
        <span class="section-count">{{ userIntents.length }}</span>
      </button>

      <div id="iq-user-items" class="section-items">
        <div
          v-for="intent in userIntents"
          :key="intent.id"
          class="intent-row"
          :class="{ active: intent.id === selectedIntentId }"
        >
          <button class="intent-main" type="button" @click="$emit('select-intent', intent)">
            <div>
              <strong>{{ intent.title }}</strong>
              <small>{{ intent.id }} · {{ intent.owner }}</small>
            </div>
            <span :class="intent.statusClass">{{ intent.status }}</span>
          </button>
          <div class="intent-more" @click.stop>
            <button class="more-btn" type="button" @click.stop="toggleMenu(intent.id)">⋯</button>
            <div v-if="openMenuId === intent.id" class="more-menu">
              <button type="button" @click.stop="handleDelete(intent)">删除</button>
            </div>
          </div>
        </div>
      </div>

      <!-- ── 系统自愈 ── -->
      <!-- 系统自愈历史分组暂时下线，保留代码以便后续恢复。 -->
      <button
        v-if="false && watcherIntents.length"
        class="section-header"
        type="button"
        :aria-expanded="watcherSectionOpen"
        aria-controls="iq-watcher-items"
        @click="watcherSectionOpen = !watcherSectionOpen"
      >
        <svg class="section-chevron" :class="{ open: watcherSectionOpen }" viewBox="0 0 24 24" aria-hidden="true">
          <path d="m9 6 6 6-6 6" />
        </svg>
        <span class="section-label">⚡ 系统自愈</span>
        <span class="section-count">{{ watcherIntents.length }}</span>
      </button>

      <div
        v-if="false && watcherIntents.length"
        id="iq-watcher-items"
        class="section-items"
        :class="{ collapsed: !watcherSectionOpen }"
        role="region"
        :aria-hidden="!watcherSectionOpen"
      >
        <div
          v-for="intent in watcherIntents"
          :key="intent.id"
          class="intent-row watcher-row"
          :class="{ active: intent.id === selectedIntentId }"
        >
          <button class="intent-main" type="button" @click="$emit('select-intent', intent)">
            <div>
              <strong>{{ intent.title }}</strong>
              <small>{{ intent.id }} · {{ intent.owner }}</small>
            </div>
            <span :class="intent.statusClass">{{ intent.status }}</span>
          </button>
          <div class="intent-more" @click.stop>
            <button class="more-btn" type="button" @click.stop="toggleMenu(intent.id)">⋯</button>
            <div v-if="openMenuId === intent.id" class="more-menu">
              <button type="button" @click.stop="handleDelete(intent)">删除</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, onUnmounted } from 'vue'

const props = defineProps({
  intents: { type: Array, default: () => [] },
  selectedIntentId: { type: String, default: '' },
})

const emit = defineEmits(['select-intent', 'delete-intent'])

const openMenuId = ref(null)
const watcherSectionOpen = ref(false)

function _isWatcher(record) {
  if (!record) return false
  return record.owner === 'watcher' || record.user_id === 'watcher'
}

const userIntents = computed(() =>
  props.intents.filter((r) => !_isWatcher(r))
)
const watcherIntents = computed(() =>
  props.intents.filter((r) => _isWatcher(r))
)

function toggleMenu(id) {
  openMenuId.value = openMenuId.value === id ? null : id
}

function handleDelete(intent) {
  openMenuId.value = null
  emit('delete-intent', intent.id)
}

// Close menu on outside click
function onDocClick() { openMenuId.value = null }
onUnmounted(() => document.removeEventListener('click', onDocClick))
if (typeof document !== 'undefined') {
  document.addEventListener('click', onDocClick, { once: false })
}
</script>

<style scoped>
.intent-queue { min-height: 0; }

.queue-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

h2 { margin: 0; color: #0f172a; font-size: 1rem; }

.queue-head > span {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.72rem;
  font-weight: 850;
}

.intent-list {
  display: flex;
  flex-direction: column;
}

/* ── Section headers ── */
.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px 8px 6px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.66rem;
  font-weight: 950;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  text-align: left;
  cursor: pointer;
  user-select: none;
  transition: background 0.18s ease, color 0.18s ease;
}

.section-header:hover {
  background: rgba(241, 245, 249, 0.64);
  color: #334155;
}

.section-chevron {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  transition: transform 0.25s ease;
  color: #94a3b8;
}

.section-chevron.open {
  transform: rotate(90deg);
}

.section-label {
  flex: 0 0 auto;
}

.section-count {
  margin-left: auto;
  min-width: 22px;
  height: 20px;
  display: inline-grid;
  place-items: center;
  border-radius: 999px;
  background: rgba(203, 213, 225, 0.44);
  color: #475569;
  font-size: 0.62rem;
  font-weight: 900;
  letter-spacing: 0;
}

/* ── Collapsible section items ── */
.section-items {
  display: grid;
  gap: 8px;
  overflow: hidden;
  max-height: 2000px;
  transition: max-height 0.35s cubic-bezier(0.16, 1, 0.3, 1),
              opacity 0.25s ease,
              margin-bottom 0.3s ease;
  opacity: 1;
  margin-bottom: 14px;
}

.section-items.collapsed {
  max-height: 0;
  opacity: 0;
  margin-bottom: 0;
}

/* ── Watcher row accent ── */
.watcher-row::before {
  content: '';
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 3px;
  border-radius: 0 2px 2px 0;
  background: #f59e0b;
  opacity: 0.55;
}

.intent-row {
  position: relative;
  display: flex;
  align-items: center;
  min-height: 62px;
  border: 1px solid rgba(203, 213, 225, 0.76);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.66);
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
  overflow: visible;
}

.intent-row:hover,
.intent-row.active {
  transform: translateY(-1px);
  border-color: rgba(56, 189, 248, 0.5);
  box-shadow: 0 14px 26px rgba(14, 165, 233, 0.12);
}

.intent-main {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 62px;
  padding: 0 4px 0 14px;
  border: 0;
  border-radius: 14px;
  color: inherit;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.intent-main strong,
.intent-main small { display: block; }

.intent-main strong {
  color: #0f172a;
  font-size: 0.84rem;
}

.intent-main small {
  margin-top: 5px;
  color: #64748b;
  font-size: 0.7rem;
  font-weight: 750;
}

.intent-main > span {
  flex: 0 0 auto;
  min-width: 70px;
  height: 26px;
  display: inline-grid;
  place-items: center;
  border-radius: 999px;
  color: #475569;
  background: #f1f5f9;
  font-size: 0.7rem;
  font-weight: 900;
}

.intent-main > span.success { color: #047857; background: rgba(209, 250, 229, 0.84); }
.intent-main > span.running { color: #0369a1; background: rgba(224, 242, 254, 0.86); }
.intent-main > span.pending { color: #4338ca; background: rgba(238, 242, 255, 0.9); }
.intent-main > span.clarification { color: #b45309; background: rgba(254, 243, 199, 0.9); }
.intent-main > span.failed { color: #b91c1c; background: rgba(254, 226, 226, 0.84); }

/* ── More menu ───────────────────────────────────────────────────────── */
.intent-more { position: relative; flex: 0 0 auto; padding-right: 6px; }

.more-btn {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid transparent;
  border-radius: 8px;
  color: #64748b;
  background: transparent;
  font-size: 1rem;
  font-weight: 900;
  cursor: pointer;
  transition: all 0.14s ease;
}
.more-btn:hover {
  color: #0f172a;
  background: rgba(241, 245, 249, 0.8);
  border-color: rgba(203, 213, 225, 0.6);
}

.more-menu {
  position: absolute;
  right: 0;
  top: 100%;
  z-index: 30;
  min-width: 100px;
  padding: 4px;
  border: 1px solid rgba(203, 213, 225, 0.7);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.14);
  backdrop-filter: blur(12px);
  animation: menuIn 0.12s ease;
}

.more-menu button {
  display: block;
  width: 100%;
  padding: 8px 12px;
  border: 0;
  border-radius: 7px;
  color: #b91c1c;
  background: transparent;
  font: inherit;
  font-size: 0.74rem;
  font-weight: 700;
  text-align: left;
  cursor: pointer;
}
.more-menu button:hover { background: rgba(254, 226, 226, 0.7); }

@keyframes menuIn {
  0% { opacity: 0; transform: translateY(-4px); }
  100% { opacity: 1; transform: translateY(0); }
}
</style>
