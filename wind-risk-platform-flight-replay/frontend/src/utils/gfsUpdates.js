export const GFS_CHECK_INTERVAL_MS = 30 * 60 * 1000

// Schedule just after the backend's next check so an active download can be
// followed by the existing ten-second local progress polling.
export const gfsPollDelay = (download, now = Date.now()) => {
  const next = Date.parse(download?.next_check_at)
  return Number.isFinite(next)
    ? Math.max(1000, Math.min(GFS_CHECK_INTERVAL_MS, next - now + 1000))
    : GFS_CHECK_INTERVAL_MS
}

export const chooseGfsTime = (times, selectedLabel, fallback, now = Date.now()) => {
  const selected = times.find(item => item.label === selectedLabel)
  const at = selected?.valid_time?.replace(' UTC', ':00Z').replace(' ', 'T')
  return selected && Date.parse(at) >= now ? selected : fallback
}
