import { onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'

export const useAdaptiveReplayDock = (dockRef, layoutDependencies) => {
  const style = shallowRef({})
  let observer, legend, frame, disposed = false

  const update = () => {
    frame = undefined
    const dock = dockRef.value
    const root = dock?.parentElement
    if (!root || disposed) return

    const currentLegend = root.querySelector('.wind-heat-legend')
    if (currentLegend !== legend) {
      if (legend) observer?.unobserve(legend)
      legend = currentLegend
      if (legend) observer?.observe(legend)
    }

    const bounds = root.getBoundingClientRect()
    const viewport = window.visualViewport
    const visibleLeft = Math.max(0, (viewport?.offsetLeft || 0) - bounds.left)
    const visibleTop = Math.max(0, (viewport?.offsetTop || 0) - bounds.top)
    const visibleRight = Math.min(bounds.width, visibleLeft + (viewport?.width || bounds.width))
    const visibleBottom = Math.min(bounds.height, visibleTop + (viewport?.height || bounds.height))
    const gap = 12
    const center = (visibleLeft + visibleRight) / 2
    const fullWidth = Math.max(0, Math.min(560, visibleRight - visibleLeft - gap * 2))
    let width = fullWidth
    let bottom = bounds.height - visibleBottom + gap

    if (legend && legend.getClientRects().length) {
      const box = legend.getBoundingClientRect()
      const left = box.left - bounds.left
      const right = box.right - bounds.left
      // Keep the replay centered. First use the actual horizontal space beside
      // the legend; stack only when that space cannot fit usable controls.
      const besideWidth = 2 * Math.max(0, left >= center ? left - center - gap : center - right - gap)
      if (besideWidth >= Math.min(280, fullWidth)) {
        width = Math.min(fullWidth, besideWidth)
      } else {
        bottom = Math.max(bottom, bounds.height - (box.top - bounds.top) + gap)
      }
    }

    const headerBottom = root.querySelector('.workspace-header')?.getBoundingClientRect().bottom ?? bounds.top
    const top = Math.max(visibleTop + gap, headerBottom - bounds.top + gap)
    const next = {
      '--replay-left': `${Math.round((center - width / 2) * 100) / 100}px`,
      '--replay-width': `${Math.round(width * 100) / 100}px`,
      '--replay-bottom': `${Math.round(bottom * 100) / 100}px`,
      '--replay-max-height': `${Math.max(0, Math.floor(bounds.height - bottom - top))}px`,
    }
    if (Object.keys(next).some(key => next[key] !== style.value[key])) style.value = next
  }

  const schedule = () => {
    if (!disposed && frame === undefined) frame = requestAnimationFrame(update)
  }

  watch(layoutDependencies, schedule, { flush: 'post' })
  onMounted(() => {
    const root = dockRef.value?.parentElement
    observer = new ResizeObserver(schedule)
    for (const element of [root, dockRef.value, root?.querySelector('.workspace-header')]) {
      if (element) observer.observe(element)
    }
    window.addEventListener('resize', schedule)
    window.visualViewport?.addEventListener('resize', schedule)
    window.visualViewport?.addEventListener('scroll', schedule)
    update()
  })
  onBeforeUnmount(() => {
    disposed = true
    if (frame !== undefined) cancelAnimationFrame(frame)
    observer?.disconnect()
    window.removeEventListener('resize', schedule)
    window.visualViewport?.removeEventListener('resize', schedule)
    window.visualViewport?.removeEventListener('scroll', schedule)
  })

  return style
}
