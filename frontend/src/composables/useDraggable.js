/**
 * useDraggable.js — pointer-capture drag for floating panels
 *
 * Extracted from NodeDetailCard and LinkDetailCard (identical implementations).
 *
 * Usage:
 *   const { onDragStart, dragStyle } = useDraggable()
 *   const cardStyle = computed(() => ({ ...props.style, ...dragStyle.value }))
 *   // Bind @pointerdown="onDragStart" on the draggable header
 */

import { ref, computed, onUnmounted } from 'vue'

export function useDraggable(options = {}) {
  const { shouldIgnore = (e) => e.target.closest('button') } = options

  const dragOffset = ref({ x: 0, y: 0 })
  let _dragStart = null

  function onDragStart(e) {
    if (shouldIgnore(e)) return
    _dragStart = { x: e.clientX - dragOffset.value.x, y: e.clientY - dragOffset.value.y }
    e.target.setPointerCapture(e.pointerId)
    e.target.addEventListener('pointermove', onDragMove)
    e.target.addEventListener('pointerup', onDragEnd)
  }

  function onDragMove(e) {
    if (!_dragStart) return
    dragOffset.value = { x: e.clientX - _dragStart.x, y: e.clientY - _dragStart.y }
  }

  function onDragEnd(e) {
    _dragStart = null
    e.target.removeEventListener('pointermove', onDragMove)
    e.target.removeEventListener('pointerup', onDragEnd)
    e.target.releasePointerCapture(e.pointerId)
  }

  onUnmounted(() => { _dragStart = null })

  const dragStyle = computed(() => ({
    transform: `translate(${dragOffset.value.x}px, ${dragOffset.value.y}px)`,
  }))

  return { dragOffset, onDragStart, dragStyle }
}
