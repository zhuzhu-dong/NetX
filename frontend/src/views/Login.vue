<template>
  <main class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <p class="kicker">NETPULSE COMPETITION DEMO ACCESS</p>
      <h1>登录网络运维控制台</h1>

      <div class="mode-switch" role="tablist" aria-label="演示账号操作">
        <button type="button" :class="{ active: mode === 'login' }" @click="switchMode('login')">账号登录</button>
        <button type="button" :class="{ active: mode === 'register' }" @click="switchMode('register')">注册演示账号</button>
      </div>

      <label>邮箱<input v-model.trim="email" type="email" autocomplete="email" required :disabled="loading"></label>
      <label>密码<input v-model="password" type="password" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" required :disabled="loading"></label>
      <label v-if="mode === 'register'">确认密码<input v-model="confirmation" type="password" autocomplete="new-password" required :disabled="loading"></label>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button type="submit" :disabled="loading">{{ loading ? '正在验证…' : mode === 'login' ? '登录' : '创建演示账号' }}</button>
    </form>
  </main>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/authStore'
import { waitForSystemReady } from '../api/client'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const mode = ref('login')
const email = ref('')
const password = ref('')
const confirmation = ref('')
const error = ref('')
const notice = ref('')
const loading = ref(false)

function switchMode(nextMode) {
  mode.value = nextMode
  password.value = ''
  confirmation.value = ''
  error.value = ''
  notice.value = ''
}

async function submit() {
  error.value = ''
  notice.value = ''
  if (mode.value === 'register' && password.value !== confirmation.value) {
    error.value = '两次输入的密码不一致。'
    return
  }
  loading.value = true
  try {
    if (mode.value === 'register') {
      await auth.register({ email: email.value, password: password.value })
      switchMode('login')
      notice.value = '演示账号已创建，请使用刚才的邮箱和密码登录。'
      return
    }
    await auth.login({ email: email.value, password: password.value })
    // Keep main's readiness courtesy while retaining the current real API
    // integration. A readiness delay never fabricates topology data.
    await waitForSystemReady({ maxAttempts: 3, intervalMs: 500 })
    await router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard')
  } catch (_) {
    error.value = mode.value === 'login' ? '邮箱或密码错误；首次使用请先注册演示账号。' : '无法创建演示账号。'
  } finally {
    password.value = ''
    confirmation.value = ''
    loading.value = false
  }
}
</script>

<style scoped>
.login-page{height:100%;display:grid;place-items:center;padding:24px;box-sizing:border-box}.login-card{width:min(440px,100%);padding:34px;border:1px solid #cfe4f2;border-radius:16px;background:rgba(255,255,255,.9);box-shadow:0 24px 60px rgba(15,23,42,.13);display:grid;gap:14px}.kicker{margin:0;color:#168ac0;font:700 .68rem ui-monospace,monospace;letter-spacing:.12em}.login-card h1{margin:0;color:#1e293b;font-size:1.45rem}.hint{margin:0;color:#64748b;font-size:.83rem;line-height:1.6}.mode-switch{display:flex;gap:8px}.mode-switch button{flex:1;background:#edf6fb;color:#356477}.mode-switch button.active{background:#168ac0;color:#fff}.login-card label{display:grid;gap:6px;color:#475569;font-size:.78rem;font-weight:700}.login-card input{padding:11px 12px;border:1px solid #bfd8e8;border-radius:8px;font:inherit}.login-card button{padding:12px;border:0;border-radius:8px;background:#168ac0;color:white;font-weight:800;cursor:pointer}.login-card button:disabled{opacity:.65;cursor:wait}.error{margin:0;color:#b42318;font-size:.8rem}.notice{margin:0;color:#087e5b;font-size:.8rem}
</style>
