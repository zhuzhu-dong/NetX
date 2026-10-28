import { ref } from 'vue'
import { defineStore } from 'pinia'
import { createCompetitionDemoAuth } from '../auth/competitionDemoAuth'

// The competition deployment intentionally uses origin/main's browser-local
// demo access flow.  This store is separate from api/auth.py so turning
// IDN_OPERATOR_AUTH_ENABLED back on for a real deployment remains possible.
export const useAuthStore = defineStore('competition-demo-auth', () => {
  const demoAuth = createCompetitionDemoAuth()
  const user = ref(demoAuth.currentUser())
  const checked = ref(true)
  const checking = ref(false)

  async function refresh() {
    user.value = demoAuth.currentUser()
    checked.value = true
    return user.value
  }

  async function register(credentials) {
    return demoAuth.register(credentials)
  }

  async function login(credentials) {
    user.value = demoAuth.login(credentials)
    checked.value = true
    return user.value
  }

  async function logout() {
    demoAuth.logout()
    user.value = null
    checked.value = true
  }

  return { user, checked, checking, refresh, register, login, logout }
})
