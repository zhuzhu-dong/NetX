<template>
  <div class="app-shell">
    <aside class="global-sidebar" :class="{ collapsed: sidebarCollapsed }">
      <!-- Brand -->
      <div class="sidebar-header">
        <div class="brand-block">
          <h1>NetPulse</h1>
          <p>意图驱动 · 多智能体自治网络</p>
        </div>
        <button
          class="sidebar-toggle"
          type="button"
          :aria-label="sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
          @click="sidebarCollapsed = !sidebarCollapsed"
        >
          <span class="toggle-icon" aria-hidden="true">
            <svg class="panel-icon panel-icon-open" viewBox="0 0 24 24">
              <rect x="3" y="4" width="18" height="16" rx="2.5"></rect>
              <path d="M9 4v16"></path>
              <path d="m14 9 3 3-3 3"></path>
            </svg>
            <svg class="panel-icon panel-icon-close" viewBox="0 0 24 24">
              <rect x="3" y="4" width="18" height="16" rx="2.5"></rect>
              <path d="M9 4v16"></path>
              <path d="m17 9-3 3 3 3"></path>
            </svg>
          </span>
        </button>
      </div>

      <div class="sidebar-body">
        <!-- New Intent -->
        <button class="new-intent-btn" type="button" @click="handleNewIntent">
          <span class="btn-icon">+</span>
          <span class="btn-label">新建意图</span>
        </button>

        <!-- Nav items -->
        <nav class="sidebar-nav">
          <RouterLink
            to="/dashboard/workflow"
            class="sidebar-nav-item"
            active-class="active"
            title="查看当前或历史意图的智能体执行详情"
          >
            <span class="nav-icon">⚙</span>
            <span class="nav-label">智能体详情</span>
          </RouterLink>
          <RouterLink to="/dashboard/topology" class="sidebar-nav-item" active-class="active">
            <span class="nav-icon">⌁</span>
            <span class="nav-label">网络视图</span>
          </RouterLink>
        </nav>

        <!-- History -->
        <div class="history-block">
          <!-- User Intents -->
          <button
            class="history-section-header"
            type="button"
            :aria-expanded="historySections.userOpen"
            @click="historySections.userOpen = !historySections.userOpen"
          >
            <svg class="section-chevron" :class="{ open: historySections.userOpen }" viewBox="0 0 24 24" aria-hidden="true">
              <path d="m9 6 6 6-6 6" />
            </svg>
            <span class="section-label">历史对话</span>
          </button>
          <div v-show="historySections.userOpen" class="history-list">
            <article
              v-for="item in historySections.userConversations"
              :key="item.id"
              class="history-item"
              :class="{ active: item.sessionId === intentStore.activeIntent?.sessionId || item.id === intentStore.selectedIntentId }"
              role="button"
              tabindex="0"
              @click="handleHistoryClick(item)"
              @keydown.enter.prevent="handleHistoryClick(item)"
            >
              <i :class="item.statusClass"></i>
              <span class="history-copy">
                <strong :title="item.title">{{ item.title }}</strong>
                <small>{{ item.updatedAt || item.createdAt || item.id }}</small>
              </span>
              <div class="history-more" @click.stop>
                <button
                  class="history-more-btn"
                  type="button"
                  :aria-label="`更多操作：${item.title}`"
                  title="更多操作"
                  @click.stop="historyMenuOpenId = historyMenuOpenId === item.id ? '' : item.id"
                >⋯</button>
                <div v-if="historyMenuOpenId === item.id" class="history-more-menu">
                  <button type="button" @click.stop="handleHistoryDelete(item)">删除</button>
                </div>
              </div>
            </article>
            <p v-if="!historySections.userConversations.length" class="history-empty">暂无历史对话</p>
          </div>

          <!-- System Auto-Healing -->
          <!-- 绯荤粺鑷剤鍏ュ彛鏆傛椂涓嬬嚎锛屼繚鐣欎唬鐮佷互渚垮悗缁仮澶嶃€?-->
          <button v-if="false"
            class="history-section-header"
            type="button"
            :aria-expanded="historySections.watcherOpen"
            @click="historySections.watcherOpen = !historySections.watcherOpen"
          >
            <svg class="section-chevron" :class="{ open: historySections.watcherOpen }" viewBox="0 0 24 24" aria-hidden="true">
              <path d="m9 6 6 6-6 6" />
            </svg>
            <span class="section-label">系统自愈</span>
            <span class="section-count watcher-count">{{ historySections.watcherIntents.length }}</span>
          </button>
          <div v-if="false" v-show="historySections.watcherOpen" class="history-list">
            <article
              v-for="item in historySections.watcherIntents"
              :key="item.id"
              class="history-item watcher"
              :class="{ active: item.id === intentStore.selectedIntentId }"
              role="button"
              tabindex="0"
              @click="handleHistoryClick(item)"
              @keydown.enter.prevent="handleHistoryClick(item)"
            >
              <i :class="item.statusClass"></i>
              <span class="history-copy">
                <strong :title="item.title">{{ item.title }}</strong>
                <small>{{ item.updatedAt || item.createdAt || item.id }}</small>
              </span>
              <div class="history-more" @click.stop>
                <button
                  class="history-more-btn"
                  type="button"
                  :aria-label="`更多操作：${item.title}`"
                  title="更多操作"
                  @click.stop="historyMenuOpenId = historyMenuOpenId === item.id ? '' : item.id"
                >⋯</button>
                <div v-if="historyMenuOpenId === item.id" class="history-more-menu">
                  <button type="button" @click.stop="handleHistoryDelete(item)">删除</button>
                </div>
              </div>
            </article>
            <p v-if="!historySections.watcherIntents.length" class="history-empty">暂无系统自愈记录</p>
          </div>
        </div>

        <!-- Account -->
        <div ref="accountAreaRef" class="account-area">
          <button class="sidebar-profile" type="button" @click="toggleAccountMenu">
            <span class="profile-avatar" :class="{ empty: !userProfile.avatar }">
              <img v-if="userProfile.avatar" :src="userProfile.avatar" alt="" />
              <b v-else>{{ profileInitial }}</b>
            </span>
            <span class="profile-meta">
              <strong>{{ displayName }}</strong>
              <small>{{ userUid }}</small>
            </span>
            <svg class="profile-chevron" viewBox="0 0 24 24" aria-hidden="true">
              <path d="m9 6 6 6-6 6"></path>
            </svg>
          </button>

          <transition name="account-menu-fade">
            <section v-if="accountMenuOpen" class="account-menu-card">
              <div class="account-menu-head">
                <button class="account-avatar-btn" type="button" @click="triggerAvatarPicker">
                  <img v-if="userProfile.avatar" :src="userProfile.avatar" alt="" />
                  <span v-else>{{ profileInitial }}</span>
                  <em>更换头像</em>
                </button>
                <input
                  ref="profileFileInput"
                  class="profile-file-input"
                  type="file"
                  accept="image/*"
                  @change="handleAvatarSelected"
                />
                <div class="account-identity">
                  <input
                    v-if="isEditingName"
                    ref="nameInputEl"
                    v-model.trim="profileDraft.name"
                    class="account-name-input"
                    type="text"
                    maxlength="18"
                    placeholder="未命名"
                    @keydown.enter.prevent="commitProfileName"
                    @keydown.esc.prevent="cancelNameEdit"
                    @blur="commitProfileName"
                  />
                  <button v-else class="account-name-btn" type="button" @click="beginNameEdit">
                    {{ displayName }}
                  </button>
                  <small>{{ userUid }}</small>
                </div>
              </div>
              <div class="account-menu-section model-section">
                <button class="account-menu-item model-entry is-disabled" type="button" disabled>
                  <span class="menu-icon">✓</span>
                  <span>选择模型</span>
                  <strong>{{ selectedModelLabel }}</strong>
                </button>
              </div>
              <div class="account-menu-section danger-section">
                <button class="account-menu-item logout-item" type="button" @click="logout">
                  <span class="menu-icon">↗</span>
                  <span>退出登录</span>
                </button>
              </div>
            </section>
          </transition>
        </div>
      </div>
    </aside>

    <div class="main-stage">
      <router-view v-slot="{ Component }">
        <transition name="view-fade" mode="out-in">
          <keep-alive>
            <component :is="Component" />
          </keep-alive>
        </transition>
      </router-view>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useIntentStore } from '../stores/intentStore'
import { usePipelineStore } from '../stores/pipelineStore'
import { useAuthStore } from '../stores/authStore'

const router = useRouter()
const intentStore = useIntentStore()
const pipelineStore = usePipelineStore()
const authStore = useAuthStore()

const agentDetailIntentId = computed(() => {
  const record = intentStore.activeIntent
  if (!record) return ''

  const orderedRunIds = (record.runIds || [])
    .filter((id) => /^INT-/i.test(String(id || '')))
    .map(String)
  const resultIds = Object.keys(record.intentResults || {})
    .filter((id) => /^INT-/i.test(String(id || '')))
  const availableIds = [...new Set([...orderedRunIds, ...resultIds])]

  if (/^INT-/i.test(String(record.id || ''))) return String(record.id)
  return availableIds.at(-1) || ''
})


/* 鈹€鈹€ Sidebar collapse 鈹€鈹€ */
const sidebarCollapsed = ref(false)

/* 鈹€鈹€ Nav 鈹€鈹€ */
function handleNewIntent() {
  intentStore.startNewIntent()
  pipelineStore.resetPipeline()
  router.push('/dashboard')
}

function handleHistoryClick(item) {
  if (!item?.id) return
  void intentStore.selectConversation(item)
  router.push('/dashboard')
}

/* 鈹€鈹€ Account / Profile 鈹€鈹€ */
const accountAreaRef = ref(null)
const accountMenuOpen = ref(false)
const isEditingName = ref(false)
const profileFileInput = ref(null)
const nameInputEl = ref(null)
const historyMenuOpenId = ref('')

const historySections = reactive({
  userOpen: true,
  watcherOpen: false,
  userConversations: computed(() => intentStore.conversationHistory.filter(i => i.owner !== 'watcher')),
  watcherIntents: computed(() => intentStore.intentHistory.filter(i => i.owner === 'watcher')),
})

const selectedModel = ref('server-managed')

// Authentication is enforced at the reverse proxy.  These labels identify
// the operator workspace only; they are not a client-side login/session.
const userSession = reactive({ email: '', uid: '' })

const userProfile = reactive({
  name: '',
  avatar: '',
})

const profileDraft = reactive({ name: '', avatar: '' })

const selectedModelLabel = computed(() => {
  return selectedModel.value === 'server-managed' ? '由服务端管理' : selectedModel.value
})

const profileInitial = computed(() => {
  const name = displayName.value || 'OP'
  return name.charAt(0).toUpperCase()
})

const displayName = computed(() => authStore.user?.display_name || userProfile.name || '网络运维员')
const userUid = computed(() => authStore.user?.role || userSession.uid || 'OPERATOR')

function toggleAccountMenu() {
  accountMenuOpen.value = !accountMenuOpen.value
  if (accountMenuOpen.value) {
    resetProfileDraft()
    nextTick(() => { /* menu opened */ })
  }
}

function closeAccountMenu() {
  accountMenuOpen.value = false
  isEditingName.value = false
}

function handleAccountOutsideClick(event) {
  if (!accountMenuOpen.value || !accountAreaRef.value) return
  if (!accountAreaRef.value.contains(event.target)) {
    closeAccountMenu()
  }
}

function resetProfileDraft() {
  profileDraft.name = userProfile.name
  profileDraft.avatar = userProfile.avatar
}

function triggerAvatarPicker() {
  profileFileInput.value?.click()
}

function handleAvatarSelected(event) {
  const file = event.target?.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (e) => {
    const dataUrl = e.target?.result
    if (typeof dataUrl === 'string') {
      userProfile.avatar = dataUrl
      persistUserProfile()
    }
  }
  reader.readAsDataURL(file)
}

function beginNameEdit() {
  resetProfileDraft()
  isEditingName.value = true
  nextTick(() => nameInputEl.value?.select())
}

function commitProfileName() {
  isEditingName.value = false
  const name = (profileDraft.name || '').trim()
  if (name && name !== userProfile.name) {
    userProfile.name = name
    persistUserProfile()
  }
}

function cancelNameEdit() {
  isEditingName.value = false
  profileDraft.name = userProfile.name
}

function persistUserProfile() { /* profile persistence is intentionally not an auth mechanism */ }

async function logout() {
  closeAccountMenu()
  await authStore.logout()
  router.replace('/login')
}

async function handleHistoryDelete(intent) {
  if (!intent?.id) return
  historyMenuOpenId.value = ''
  await intentStore.deleteConversation(intent)
}

function onHistoryMenuDocClick() {
  historyMenuOpenId.value = ''
}

onMounted(() => {
  void authStore.refresh()
  intentStore.loadConversations()
  document.addEventListener('pointerdown', handleAccountOutsideClick)
  document.addEventListener('click', onHistoryMenuDocClick)
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', handleAccountOutsideClick)
  document.removeEventListener('click', onHistoryMenuDocClick)
})
</script>

<style scoped>
/* 鈹€鈹€ Shell Layout 鈹€鈹€ */
.app-shell {
  width: 100vw;
  height: 100vh;
  display: flex;
  overflow: hidden;
  color: #0f172a;
  user-select: none;
  -webkit-user-select: none;
}

/* 鈹€鈹€ Sidebar 鈹€鈹€ */
.global-sidebar {
  width: 280px;
  min-width: 280px;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.62);
  border-right: 1px solid rgba(226, 232, 240, 0.78);
  backdrop-filter: blur(14px);
  transition: width 0.28s cubic-bezier(0.4, 0, 0.2, 1),
              min-width 0.28s cubic-bezier(0.4, 0, 0.2, 1),
              transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
  z-index: 20;
  flex-shrink: 0;
}

.global-sidebar.collapsed {
  width: 52px;
  min-width: 52px;
}

.sidebar-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 16px 14px 10px;
}

.global-sidebar.collapsed .brand-block { display: none; }

.brand-block h1 {
  margin: 0;
  font-size: 1.1rem;
  font-weight: 950;
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.brand-block p {
  margin: 2px 0 0;
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.64rem;
  font-weight: 700;
  letter-spacing: 0.8px;
}

.sidebar-toggle {
  width: 28px;
  height: 28px;
  border: 1px solid rgba(203, 213, 225, 0.6);
  border-radius: 8px;
  background: rgba(248, 250, 252, 0.7);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 0;
  transition: background 0.2s ease;
}

.sidebar-toggle:hover { background: rgba(226, 232, 240, 0.6); }

.toggle-icon { display: flex; align-items: center; justify-content: center; }

.panel-icon {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: #64748b;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.panel-icon-close { display: none; }
.global-sidebar.collapsed .panel-icon-open { display: none; }
.global-sidebar.collapsed .panel-icon-close { display: inline; }

/* 鈹€鈹€ Sidebar Body 鈹€鈹€ */
.sidebar-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0 10px;
}

.sidebar-body > :not(.history-block) {
  flex-shrink: 0;
}

.global-sidebar.collapsed .sidebar-body { padding: 0 4px; }
.global-sidebar.collapsed .btn-label,
.global-sidebar.collapsed .nav-label { display: none; }
.global-sidebar.collapsed .new-intent-btn {
  padding: 8px 0;
  border-radius: 10px;
  font-size: 1rem;
  justify-content: center;
}
.global-sidebar.collapsed .sidebar-nav-item {
  justify-content: center;
  padding: 8px 0;
}
.global-sidebar.collapsed .nav-icon { margin: 0; }
.global-sidebar.collapsed .history-block,
.global-sidebar.collapsed .account-area { display: none; }

.new-intent-btn {
  width: 100%;
  padding: 10px 14px;
  border: 1px solid rgba(56, 189, 248, 0.35);
  border-radius: 12px;
  background: linear-gradient(135deg, rgba(56, 189, 248, 0.12), rgba(99, 102, 241, 0.1));
  color: #0ea5e9;
  font-size: 0.82rem;
  font-weight: 800;
  cursor: pointer;
  transition: background 0.2s ease, box-shadow 0.2s ease;
  display: flex;
  align-items: center;
  gap: 6px;
}

.btn-icon {
  font-size: 1rem;
  font-weight: 700;
  flex-shrink: 0;
}

.new-intent-btn:hover {
  background: linear-gradient(135deg, rgba(56, 189, 248, 0.22), rgba(99, 102, 241, 0.18));
  box-shadow: 0 4px 16px rgba(14, 165, 233, 0.12);
}

/* 鈹€鈹€ Sidebar Nav Items 鈹€鈹€ */
.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 10px 0 6px;
  padding: 6px 0;
  border-top: 1px solid rgba(226, 232, 240, 0.5);
  border-bottom: 1px solid rgba(226, 232, 240, 0.5);
  flex-shrink: 0;
}

.sidebar-nav-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 10px;
  color: #64748b;
  text-decoration: none;
  font-size: 0.78rem;
  font-weight: 700;
  transition: background 0.15s ease, color 0.15s ease;
}

.sidebar-nav-item:hover {
  background: rgba(226, 232, 240, 0.5);
  color: #0f172a;
}

.sidebar-nav-item.active {
  background: linear-gradient(135deg, rgba(56, 189, 248, 0.15), rgba(99, 102, 241, 0.1));
  color: #0ea5e9;
  font-weight: 850;
}

.sidebar-nav-item.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.nav-icon {
  font-size: 0.88rem;
  width: 20px;
  text-align: center;
}

/* 鈹€鈹€ History 鈹€鈹€ */
.history-block {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  gap: 2px;
}

/* 鈹€鈹€ Section header buttons 鈹€鈹€ */
.history-section-header {
  display: flex;
  align-items: center;
  gap: 5px;
  width: 100%;
  padding: 4px 6px;
  border: none;
  border-radius: 7px;
  background: transparent;
  cursor: pointer;
  transition: background 0.15s ease;
  flex-shrink: 0;
}

.history-section-header:hover {
  background: rgba(226, 232, 240, 0.3);
}

.section-chevron {
  width: 12px;
  height: 12px;
  fill: none;
  stroke: #94a3b8;
  stroke-width: 2.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  transition: transform 0.18s ease;
  flex-shrink: 0;
}

.section-chevron.open {
  transform: rotate(90deg);
}

.section-label {
  flex: 1;
  text-align: left;
  font-size: 0.66rem;
  font-weight: 800;
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.section-count {
  font-size: 0.6rem;
  font-weight: 900;
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: rgba(226, 232, 240, 0.4);
  padding: 1px 6px;
  border-radius: 999px;
  min-width: 16px;
  text-align: center;
  line-height: 1.4;
}

.section-count.watcher-count {
  color: #b45309;
  background: rgba(254, 243, 199, 0.5);
}

/* 鈹€鈹€ History list 鈹€鈹€ */
.history-list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: 0 0 2px 4px;
  overflow-y: auto;
}

.history-list::-webkit-scrollbar {
  width: 4px;
}

.history-list::-webkit-scrollbar-track {
  background: transparent;
}

.history-list::-webkit-scrollbar-thumb {
  background: rgba(148, 163, 184, 0.25);
  border-radius: 999px;
}

.history-list::-webkit-scrollbar-thumb:hover {
  background: rgba(148, 163, 184, 0.5);
}

.history-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease;
  position: relative;
}

.history-item:hover { background: rgba(226, 232, 240, 0.4); }
.history-item.active { background: rgba(224, 242, 254, 0.7); }

.history-item.watcher {
  border-left: 2px solid rgba(245, 158, 11, 0.3);
  border-radius: 0 8px 8px 0;
}

.history-item i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  background: #94a3b8;
}

.history-item i.success { background: #10b981; }
.history-item i.running { background: #38bdf8; animation: pulse-dot 1.4s ease-in-out infinite; }
.history-item i.failed { background: #ef4444; }
.history-item i.cancelled { background: #94a3b8; }
.history-item i.clarification { background: #f59e0b; }
.history-item i.pending { background: #8b5cf6; }

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(1.8); }
}

.history-copy {
  flex: 1;
  min-width: 0;
}

.history-copy strong {
  display: block;
  font-size: 0.74rem;
  font-weight: 700;
  color: #0f172a;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-copy small {
  font-size: 0.63rem;
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.history-empty {
  padding: 6px 10px;
  margin: 0;
  font-size: 0.66rem;
  color: #94a3b8;
  font-style: italic;
}

/* 鈹€鈹€ History more menu 鈹€鈹€ */
.history-more { position: relative; flex-shrink: 0; }

.history-more-btn {
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #94a3b8;
  font-size: 0.9rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s ease;
}

.history-more-btn:hover { background: rgba(226, 232, 240, 0.5); }

.history-more-menu {
  position: absolute;
  right: 0;
  top: 100%;
  margin-top: 2px;
  background: rgba(255, 255, 255, 0.95);
  border: 1px solid rgba(226, 232, 240, 0.85);
  border-radius: 10px;
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.1);
  backdrop-filter: blur(14px);
  padding: 4px;
  z-index: 30;
  min-width: 64px;
}

.history-more-menu button {
  display: block;
  width: 100%;
  padding: 6px 10px;
  border: none;
  border-radius: 7px;
  background: transparent;
  font-size: 0.72rem;
  font-weight: 700;
  color: #b91c1c;
  cursor: pointer;
  transition: background 0.15s ease;
}

.history-more-menu button:hover { background: rgba(254, 226, 226, 0.5); }

/* 鈹€鈹€ 鏇村鎿嶄綔鑿滃崟锛堝垹闄わ級鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ */
.history-more { position: relative; flex-shrink: 0; }

.history-more-btn {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: rgba(71, 85, 105, 0.6);
  font-size: 0.78rem;
  font-weight: 900;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.18s ease, background 0.18s ease, color 0.18s ease;
}

.history-item:hover .history-more-btn { opacity: 1; }

.history-more-btn:hover {
  background: rgba(239, 68, 68, 0.1);
  color: #dc2626;
}

.history-more-menu {
  position: absolute;
  right: 0;
  top: 100%;
  z-index: 30;
  min-width: 80px;
  padding: 4px;
  border: 1px solid rgba(203, 213, 225, 0.7);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.96);
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.14);
  backdrop-filter: blur(12px);
  animation: menuIn 0.12s ease;
}

.history-more-menu button {
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

.history-more-menu button:hover {
  background: rgba(254, 226, 226, 0.7);
}

@keyframes menuIn {
  0% { opacity: 0; transform: translateY(-4px); }
  100% { opacity: 1; transform: translateY(0); }
}

/* 鈹€鈹€ Account 鈹€鈹€ */
.account-area {
  flex-shrink: 0;
  padding: 8px 0 10px;
  border-top: 1px solid rgba(226, 232, 240, 0.5);
  position: relative;
  margin-top: 4px;
}

.sidebar-profile {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 5px 8px;
  border: none;
  border-radius: 10px;
  background: transparent;
  cursor: pointer;
  transition: background 0.15s ease;
}

.sidebar-profile:hover { background: rgba(226, 232, 240, 0.4); }

.profile-avatar {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex-shrink: 0;
}

.profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
.profile-avatar b { color: #fff; font-size: 0.82rem; font-weight: 900; }

.profile-meta { flex: 1; min-width: 0; text-align: left; }
.profile-meta strong { display: block; font-size: 0.76rem; font-weight: 800; color: #0f172a; }
.profile-meta small { font-size: 0.64rem; color: #94a3b8; }

.profile-chevron { width: 14px; height: 14px; fill: none; stroke: #94a3b8; stroke-width: 2; stroke-linecap: round; flex-shrink: 0; }

/* 鈹€鈹€ Account Menu 鈹€鈹€ */
.account-menu-card {
  position: absolute;
  bottom: calc(100% + 4px);
  left: 4px;
  right: 4px;
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.88);
  border: 1px solid rgba(226, 232, 240, 0.88);
  box-shadow: 0 26px 60px rgba(15, 23, 42, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(18px);
  padding: 16px 14px 12px;
  z-index: 30;
}

.account-menu-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(226, 232, 240, 0.6);
  margin-bottom: 6px;
}

.account-avatar-btn {
  width: 44px;
  height: 44px;
  border: 2px solid rgba(203, 213, 225, 0.5);
  border-radius: 50%;
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  cursor: pointer;
  overflow: hidden;
  flex-shrink: 0;
  position: relative;
  padding: 0;
}

.account-avatar-btn img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; }
.account-avatar-btn > span { color: #fff; font-size: 1.05rem; font-weight: 950; }
.account-avatar-btn em {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  font-size: 0.55rem;
  font-style: normal;
  font-weight: 800;
  color: #fff;
  background: linear-gradient(to top, rgba(15, 23, 42, 0.55) 35%, transparent 70%);
  border-radius: 50%;
  padding-bottom: 2px;
  opacity: 0;
  transition: opacity 0.2s ease;
}

.account-avatar-btn:hover em { opacity: 1; }

.profile-file-input { display: none; }

.account-identity { flex: 1; min-width: 0; }

.account-name-input,
.account-name-btn {
  display: block;
  width: 100%;
  border: none;
  background: transparent;
  font-size: 0.86rem;
  font-weight: 850;
  color: #0f172a;
  text-align: left;
  padding: 2px 0;
  font-family: inherit;
}

.account-name-input {
  border-bottom: 2px solid #0ea5e9;
  outline: none;
  border-radius: 0;
}

.account-name-btn {
  cursor: pointer;
  transition: color 0.15s ease;
}

.account-name-btn:hover { color: #0ea5e9; }

.account-identity small {
  display: block;
  margin-top: 1px;
  font-size: 0.66rem;
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 700;
}

.account-menu-section {
  padding: 5px 0;
  border-top: 1px solid rgba(226, 232, 240, 0.35);
}

.account-menu-section:first-of-type {
  border-top: none;
}

.model-section strong {
  margin-left: auto;
  font-size: 0.68rem;
  font-weight: 800;
  color: #0ea5e9;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.account-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: 10px;
  background: transparent;
  font-size: 0.76rem;
  font-weight: 700;
  color: #334155;
  cursor: pointer;
  transition: background 0.18s ease, color 0.18s ease;
}

.account-menu-item:hover { background: rgba(240, 244, 248, 0.8); }
.account-menu-item.is-disabled { opacity: 0.4; cursor: not-allowed; }

.menu-icon {
  font-size: 0.9rem;
  width: 20px;
  text-align: center;
  flex-shrink: 0;
  color: #94a3b8;
}

.logout-item { color: #b91c1c; }
.logout-item:hover { background: rgba(254, 226, 226, 0.55); color: #991b1b; }
.logout-item .menu-icon { color: #ef4444; }

.account-menu-fade-enter-active { transition: opacity 0.2s ease, transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1); }
.account-menu-fade-leave-active { transition: opacity 0.12s ease, transform 0.12s ease; }
.account-menu-fade-enter-from { opacity: 0; transform: translateY(8px) scale(0.96); }
.account-menu-fade-leave-to { opacity: 0; transform: translateY(4px); }

/* 鈹€鈹€ Main Stage 鈹€鈹€ */
.main-stage {
  flex: 1 1 0 !important;
  flex-grow: 1 !important;
  flex-basis: 0 !important;
  width: 0;
  min-width: 0;
  min-height: 0;
  height: 100%;
  position: relative;
  overflow: hidden;
}

/* 鈹€鈹€ Page Transitions 鈹€鈹€ */
.view-fade-enter-active { transition: opacity 0.22s ease, transform 0.22s ease; }
.view-fade-leave-active { transition: opacity 0.12s ease, transform 0.12s ease; }
.view-fade-enter-from { opacity: 0; transform: translateY(6px); }
.view-fade-leave-to { opacity: 0; transform: translateY(-4px); }

@media (max-width: 980px) {
  .global-sidebar {
    width: 240px;
    min-width: 240px;
  }

  .global-sidebar.collapsed {
    width: 44px;
    min-width: 44px;
  }
}
</style>
