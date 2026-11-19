import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { defaultLayout, fitDesktopPanels, LAYOUT_KEY, normalizeLayout } from './workspaceLayout'

export const useWorkspaceLayout = () => {
  let stored
  try { stored = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}') } catch { stored = {} }
  const preferred = reactive(normalizeLayout(stored || {}))
  const viewport = ref(window.innerWidth)
  const drawer = ref('left')
  const resizing = ref(false)
  const compact = computed(() => viewport.value <= 1200)
  const panels = computed(() => compact.value ? {
    leftWidth: Math.min(360, viewport.value - 32), rightWidth: Math.min(380, viewport.value - 32),
    leftOpen: drawer.value === 'left', rightOpen: drawer.value === 'right',
  } : fitDesktopPanels(preferred, viewport.value))
  const insets = computed(() => ({
    left: !compact.value && panels.value.leftOpen ? panels.value.leftWidth + 12 : 12,
    right: !compact.value && panels.value.rightOpen ? panels.value.rightWidth + 12 : 12,
  }))
  const save = () => { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(preferred)) } catch { /* Storage can be disabled. */ } }
  const open = side => { if (compact.value) drawer.value = side; else { preferred[`${side}Open`] = true; save() } }
  const close = side => { if (compact.value) { if (drawer.value === side) drawer.value = '' } else { preferred[`${side}Open`] = false; save() } }
  const reset = () => { Object.assign(preferred, defaultLayout()); drawer.value = 'left'; save() }
  const resizeTo = (side, width) => {
    if (compact.value) return
    const other = side === 'left' ? 'right' : 'left'
    const otherWidth = panels.value[`${other}Open`] ? panels.value[`${other}Width`] : 0
    preferred[`${side}Width`] = Math.max(280, Math.min(480, width, viewport.value - 448 - otherWidth))
  }
  let drag, frame
  const finish = () => {
    if (!drag) return
    cancelAnimationFrame(frame)
    frame = null
    if (drag.pending !== undefined) resizeTo(drag.side, drag.pending)
    drag.target.releasePointerCapture?.(drag.pointerId)
    drag = null; resizing.value = false; save()
  }
  const move = event => {
    if (!drag || event.pointerId !== drag.pointerId) return
    drag.pending = drag.width + (event.clientX - drag.x) * (drag.side === 'left' ? 1 : -1)
    if (!frame) frame = requestAnimationFrame(() => { frame = null; if (drag) resizeTo(drag.side, drag.pending) })
  }
  const start = (side, event) => {
    if (compact.value || event.button !== 0) return
    event.preventDefault(); event.stopPropagation()
    drag = { side, x: event.clientX, width: panels.value[`${side}Width`], target: event.currentTarget, pointerId: event.pointerId }
    drag.target.setPointerCapture(event.pointerId); resizing.value = true
  }
  const keyResize = (side, event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const width = event.key === 'Home' ? 280 : event.key === 'End' ? 480
      : panels.value[`${side}Width`] + (event.key === 'ArrowRight' ? 16 : -16) * (side === 'left' ? 1 : -1)
    resizeTo(side, width); save()
  }
  const onResize = () => { finish(); viewport.value = window.innerWidth }
  onMounted(() => {
    window.addEventListener('resize', onResize)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
  })
  onBeforeUnmount(() => {
    finish()
    window.removeEventListener('resize', onResize)
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', finish)
    window.removeEventListener('pointercancel', finish)
  })
  return { panels, compact, insets, resizing, open, close, reset, start, keyResize,
    revealDecision: () => { if (compact.value) drawer.value = 'right' },
    showMap: () => { if (compact.value) drawer.value = '' },
  }
}
