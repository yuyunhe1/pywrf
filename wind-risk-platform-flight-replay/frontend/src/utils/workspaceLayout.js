export const LAYOUT_KEY = 'wind-platform-layout-v1'
export const defaultLayout = () => ({ leftWidth: 320, rightWidth: 360, leftOpen: true, rightOpen: true })
const clamp = (n, fallback) => Number.isFinite(n) ? Math.max(280, Math.min(480, n)) : fallback
export const normalizeLayout = (value = {}) => ({
  leftWidth: clamp(value.leftWidth, 320), rightWidth: clamp(value.rightWidth, 360),
  leftOpen: typeof value.leftOpen === 'boolean' ? value.leftOpen : true,
  rightOpen: typeof value.rightOpen === 'boolean' ? value.rightOpen : true,
})
export const fitDesktopPanels = (preferred, viewport) => {
  const result = normalizeLayout(preferred)
  const sides = ['left', 'right'].filter(side => result[`${side}Open`])
  const available = Math.max(560, viewport - 448)
  const total = sides.reduce((sum, side) => sum + result[`${side}Width`], 0)
  if (total > available) {
    const extra = total - sides.length * 280
    sides.forEach(side => { result[`${side}Width`] = 280 + (result[`${side}Width`] - 280) * (available - sides.length * 280) / extra })
  }
  return result
}
