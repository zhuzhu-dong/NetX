import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite runs inside idn-frontend during local development.  Use the backend
// service DNS on the shared Compose network instead of the frontend container.
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || 'http://idn-backend:8000'
// This value exists only in the Vite server process.  It is intentionally not
// exposed through import.meta.env or any VITE_* client variable.
const apiProxyKey = process.env.IDN_PROXY_API_KEY || ''

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  server: {
    hmr: {
      // Disable error overlay for non-fatal URI decode errors caused by
      // browser-generated requests (extensions, favicon variants, etc.)
      overlay: false,
    },
    proxy: {
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
        headers: apiProxyKey ? { 'X-API-Key': apiProxyKey } : {},
      },
    },
  },
})
