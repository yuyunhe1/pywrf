export const MAX_CUSTOM_ROUTE_POINTS = 20000

const initial = () => ({ active: false, name: '', points: [], saving: false, error: '' })
const samePoint = (a, b) => a[0] === b[0] && a[1] === b[1]
const coordinate = (value) => {
  const point = Array.isArray(value) ? value : [value?.lon, value?.lat]
  const [lon, lat] = point
  if (!Number.isFinite(lon) || !Number.isFinite(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90) {
    throw new Error('航点经纬度无效')
  }
  return [lon, lat]
}

export const customRouteName = (stamp = Date.now()) => {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(stamp)
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]))
  return `自定义航线_${values.year}${values.month}${values.day}_${values.hour}${values.minute}${values.second}`
}

export const customRoutePayload = (draft, selection) => {
  const name = draft.name?.trim()
  if (!name || name.length > 120) throw new Error('请输入 1 至 120 个字符的航线名称')
  if (!Array.isArray(draft.points) || draft.points.length > MAX_CUSTOM_ROUTE_POINTS) {
    throw new Error(`航点数量不能超过 ${MAX_CUSTOM_ROUTE_POINTS}`)
  }
  const points = draft.points.map(coordinate)
  const radians = (value) => value * Math.PI / 180
  const distance = points.slice(1).reduce((total, point, index) => {
    const previous = points[index]
    const a = Math.sin(radians(point[1] - previous[1]) / 2) ** 2
      + Math.cos(radians(previous[1])) * Math.cos(radians(point[1])) * Math.sin(radians(point[0] - previous[0]) / 2) ** 2
    return total + 12742000 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, a))))
  }, 0)
  if (points.length < 2 || !(distance > 0)) throw new Error('至少需要两个不同位置的航点，且总长度须大于零')
  if (!['gfs', 'wrf'].includes(selection.source)) throw new Error('请选择 GFS 或 WRF 数据源')
  if (!selection.level || !/m\s+AGL\b/i.test(selection.level)) throw new Error('请选择有效的离地高度层')
  return { name, points, start: points[0], end: points.at(-1), source: selection.source, level: selection.level,
    cycle: selection.cycle || null, forecast_hour: selection.forecastHour ?? null,
    ...(selection.source==='wrf' && selection.forecastHour!=null ? {forecast_minutes:Math.round(selection.forecastHour*60)} : {}) }
}

// Older exports may lack an ID. A name is usable only when it identifies one route.
export const savedRouteFromExport = (row, routes) => row.route_id
  ? routes.find((route) => route.route_id === row.route_id)
  : routes.filter((route) => route.name === row.route_name).length === 1
    ? routes.find((route) => route.name === row.route_name) : undefined

export const createCustomRouteDrawingController = ({ save, onChange, onSaved, onComplete, onSavedError = () => {}, now = Date.now }) => {
  let state = initial(), generation = 0, disposed = false
  const emit = (changes) => { state = { ...state, ...changes }; if (!disposed) onChange(state) }
  const editable = () => state.active && !state.saving && !disposed
  const updatePoint = (callback) => {
    if (!editable()) return
    try { emit({ points: callback(), error: '' }) } catch (reason) { emit({ error: reason.message }) }
  }
  return {
    begin() {
      if (state.saving || disposed) return
      generation++
      emit({ ...initial(), active: true, name: customRouteName(now()) })
    },
    add(value) {
      updatePoint(() => {
        if (state.points.length >= MAX_CUSTOM_ROUTE_POINTS) throw new Error(`航点数量不能超过 ${MAX_CUSTOM_ROUTE_POINTS}`)
        const point = coordinate(value)
        return state.points.length && samePoint(point, state.points.at(-1)) ? state.points : [...state.points, point]
      })
    },
    move(index, value) {
      if (!Number.isInteger(index) || index < 0 || index >= state.points.length) return
      updatePoint(() => state.points.map((point, i) => i === index ? coordinate(value) : point))
    },
    undo() { if (editable()) emit({ points: state.points.slice(0, -1), error: '' }) },
    clear() { if (editable()) emit({ points: [], error: '' }) },
    rename(name) { if (editable()) emit({ name, error: '' }) },
    cancel() { if (editable()) { generation++; emit(initial()) } },
    async complete(selection) {
      if (!editable()) return
      let payload
      try { payload = customRoutePayload(state, selection) } catch (reason) { emit({ error: reason.message }); return }
      const current = generation
      emit({ saving: true, error: '' })
      try {
        await onComplete(payload)
        if (!disposed && current === generation) emit(initial())
        return payload
      } catch (reason) {
        if (!disposed && current === generation) emit({ saving: false, error: reason.message || '航线评估未能启动，请重试' })
      }
    },
    async save(selection) {
      if (!editable()) return
      let payload
      try { payload = customRoutePayload(state, selection) } catch (reason) { emit({ error: reason.message }); return }
      const current = generation
      emit({ saving: true, error: '' })
      let saved
      try { saved = await save(payload) } catch (reason) {
        if (!disposed && current === generation) {
          emit({ saving: false, error: reason.response?.data?.detail || reason.message || '保存失败，请重试' })
        }
        return
      }
      if (disposed || current !== generation) return
      emit(initial())
      try { await onSaved(saved) } catch (reason) { if (!disposed && current === generation) onSavedError(saved, reason) }
      return saved
    },
    dispose() { disposed = true; generation++ },
  }
}
