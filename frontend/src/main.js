import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import './styles/design-tokens.css'
import './styles/panel.css'

const app = createApp(App)

// ── Global error handler — prevents white-screen from unhandled errors ──
app.config.errorHandler = (err, instance, info) => {
  console.error('[Vue Error]', err, 'in', info, 'component:', instance?.$.type?.name || 'unknown')
  // Could send to a logging service here
}

// ── Unhandled Promise rejection handler ──
window.addEventListener('unhandledrejection', (event) => {
  console.error('[Unhandled Promise Rejection]', event.reason)
  // Prevent default console error flooding
  event.preventDefault()
})

app.use(createPinia())
app.use(router)

app.mount('#app')