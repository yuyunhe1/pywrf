// Merge overlapping collinear segments, retaining forks and direction identity.
// This is display-only; it never changes backend labels or the official route.
export function mergeSearchCorridors(progress) {
  const groups = new Map()
  for (const direction of ['forward', 'backward']) {
    for (const corridor of (progress?.[direction] || []).slice(0, 3)) {
      const points = corridor.points || []
      for (let i = 1; i < points.length; i++) {
        const a = points[i - 1], b = points[i]
        if (![...a.slice(0, 2), ...b.slice(0, 2)].every(Number.isFinite)) continue
        let dx = b[0] - a[0], dy = b[1] - a[1]
        const length = Math.hypot(dx, dy)
        if (length < 1e-10) continue
        dx /= length; dy /= length
        if (dx < -1e-9 || (Math.abs(dx) < 1e-9 && dy < 0)) { dx = -dx; dy = -dy }
        const offset = -dy * a[0] + dx * a[1]
        const key = `${dx.toFixed(6)}:${dy.toFixed(6)}:${offset.toFixed(6)}`
        if (!groups.has(key)) groups.set(key, { dx, dy, offset, intervals: [] })
        const values = [dx * a[0] + dy * a[1], dx * b[0] + dy * b[1]].sort((x, y) => x - y)
        groups.get(key).intervals.push({ start: values[0], end: values[1], direction })
      }
    }
  }
  const lines = []
  for (const { dx, dy, offset, intervals } of groups.values()) {
    const cuts = [...new Set(intervals.flatMap((item) => [item.start, item.end]))].sort((a, b) => a - b)
    const at = (t) => [dx * t - dy * offset, dy * t + dx * offset]
    for (let i = 1; i < cuts.length; i++) {
      if (cuts[i] - cuts[i - 1] < 1e-10) continue
      const middle = (cuts[i] + cuts[i - 1]) / 2
      const directions = new Set(intervals.filter((item) => item.start <= middle && middle <= item.end).map((item) => item.direction))
      if (!directions.size) continue
      lines.push({ points: [at(cuts[i - 1]), at(cuts[i])], direction: directions.size === 2 ? 'both' : [...directions][0] })
    }
  }
  return lines
}
