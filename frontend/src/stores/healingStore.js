/**
 * Reserved for read-only presentation state.  Production self-healing is
 * initiated and reported by the backend lifecycle; the browser exposes no
 * demonstration or synthetic self-healing trigger.
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

export const useHealingStore = defineStore('healing', () => {
  const presentationState = ref('idle')
  return { presentationState }
})
