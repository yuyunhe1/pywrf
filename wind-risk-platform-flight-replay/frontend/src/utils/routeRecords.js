export const routeSnapshotKey = record => !record?.points?.length ? '' : JSON.stringify({
  name: (record.name || '').trim(), source: record.source || 'wrf', level: record.level,
  points: record.points, v3: record.v3 || null,
  mission_items: record.mission_items?.length ? record.mission_items : null,
})

export const routeDistanceKm = points => {
  if (!Array.isArray(points) || points.length < 2) return null
  const rad = n => n * Math.PI / 180
  let distance = 0
  for (let i = 1; i < points.length; i++) {
    const [a, b] = [points[i - 1], points[i]]
    if (![a?.[0], a?.[1], b?.[0], b?.[1]].every(Number.isFinite)) return null
    const h = Math.sin(rad(b[1] - a[1]) / 2) ** 2 + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(rad(b[0] - a[0]) / 2) ** 2
    distance += 12742 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))))
  }
  return distance
}

export const exportForRoute = (route, files, routes) => {
  const byId = files.filter(item => item.route_id && item.route_id === route.route_id)
  if (byId.length) return byId.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))[0]
  if (routes.filter(item => item.name === route.name).length !== 1) return undefined
  return files.filter(item => !item.route_id && item.route_name === route.name)
    .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))[0]
}

export const durationLabel = seconds => {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  const minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60)
  if (seconds === 0) return '0 分钟'
  if (!minutes) return '不足 1 分钟'
  return hours ? `${hours} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ''}` : `${minutes} 分钟`
}

export const planningStageLabel = (state, published) => {
  if (!state?.status) return ''
  if (state.status === 'queued') return '排队中'
  if (state.status === 'waiting_for_previous') return '正在释放上次规划资源'
  if (state.status === 'cached') return '已复核本页缓存'
  if (state.status === 'running') return published ? '已展示阶段结果 · 继续优化' : state.feasible ? '已找到候选 · 正在比较' : '正在搜索'
  return { completed: '规划完成', timed_out: '已达到时限', cancelled: '规划已取消', failed: '规划中断' }[state.status] || '正在规划'
}
