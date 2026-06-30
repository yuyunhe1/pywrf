<script setup>
import L from 'leaflet'
import { onMounted, reactive, ref, watch } from 'vue'
import { analyzeRoute, deleteRoute, getHeatmap, getPoint, getTimes, getWind, listRoutes, planRoute, saveRoute } from './api'
import AnalysisPanel from './components/AnalysisPanel.vue'
import ControlPanel from './components/ControlPanel.vue'
import WindMap from './components/WindMap.vue'

const options = reactive({ cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
const selection = reactive({ source: 'gfs', cycle: '', forecastHour: 3, validTime: '', level: '100m AGL' })
const layers = reactive({ heatmap: false, velocity: true, route: true })
const thresholds = reactive({ safe: 1.5, notice: 3.3, warning: 5.4, danger: 7.9 })
const wind = ref()
const heatmap = ref()
const analysis = ref()
const metadata = ref()
const loading = ref(false)
const error = ref('')
const mapRef = ref()
let routePoints = []
const planner = reactive({ name: '默认航线 A', startText: '', endText: '', points: [], planning: false })
const savedRoutes = ref([])
const picking = ref('')
const ZOOM_AVERAGE_LAYER = 13
const AVERAGE_LAYER = '250-350m AGL average'
let pointRequestController

const loadWindField = async () => {
  loading.value = true
  error.value = ''
  try {
    const [windData, heatData] = await Promise.all([getWind(selection), getHeatmap(selection)])
    wind.value = windData
    heatmap.value = heatData
    metadata.value = windData.metadata
    if (routePoints.length) await runRouteAnalysis(routePoints)
  } catch (reason) {
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  } finally {
    loading.value = false
  }
}

const normalizeDirection = (value) => ((value % 360) + 360) % 360

const loadedWindMatchesSelection = () => {
  if (!metadata.value) return false
  if (metadata.value.level !== selection.level) return false
  if (selection.validTime) return metadata.value.valid_time_bj === selection.validTime || metadata.value.valid_time === selection.validTime
  return metadata.value.cycle === selection.cycle && Number(metadata.value.forecast_hour) === Number(selection.forecastHour)
}

const pointFromLoadedWind = (lon, lat) => {
  if (!loadedWindMatchesSelection()) return null
  const [uSource, vSource] = wind.value?.velocity || []
  const header = uSource?.header
  if (!header || !Array.isArray(uSource.data) || !Array.isArray(vSource?.data)) return null

  const { nx, ny, lo1, la1, dx, dy } = header
  if (![nx, ny, lo1, la1, dx, dy].every(Number.isFinite) || nx < 1 || ny < 1 || dx <= 0 || dy <= 0) return null

  const col = Math.round((lon - lo1) / dx)
  const row = Math.round((la1 - lat) / dy)
  if (col < 0 || col >= nx || row < 0 || row >= ny) return null

  const index = row * nx + col
  const u = Number(uSource.data[index])
  const v = Number(vSource.data[index])
  if (!Number.isFinite(u) || !Number.isFinite(v)) return null

  const directionTo = normalizeDirection((Math.atan2(u, v) * 180) / Math.PI)
  return {
    lon: lo1 + col * dx,
    lat: la1 - row * dy,
    u,
    v,
    wind_speed: Math.hypot(u, v),
    wind_direction_to: directionTo,
    wind_direction_from: normalizeDirection(directionTo + 180),
    level: metadata.value?.level || selection.level,
    valid_time: metadata.value?.valid_time,
    unit: 'm/s',
    source: 'loaded-grid',
  }
}

const showPointPopup = (point, lon, lat, map) => {
  L.popup()
    .setLatLng([lat, lon])
    .setContent(`<strong>${point.wind_speed.toFixed(2)} m/s</strong><br>气象风向：${point.wind_direction_from.toFixed(0)}°<br>U：${point.u.toFixed(2)} m/s<br>V：${point.v.toFixed(2)} m/s<br><small>最近格点 ${point.lon.toFixed(2)}, ${point.lat.toFixed(2)}</small>`)
    .openOn(map)
}

const runRouteAnalysis = async (points) => {
  routePoints = points
  analysis.value = await analyzeRoute(selection, points, thresholds)
}

const queryPoint = async ({ lon, lat, map }) => {
  const localPoint = pointFromLoadedWind(lon, lat)
  if (localPoint) {
    showPointPopup(localPoint, lon, lat, map)
    return
  }

  pointRequestController?.abort()
  pointRequestController = new AbortController()
  try {
    const point = await getPoint(selection, lon, lat, { signal: pointRequestController.signal })
    showPointPopup(point, lon, lat, map)
  } catch (reason) {
    if (reason.code === 'ERR_CANCELED' || reason.name === 'CanceledError') return
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  }
}

const clearRoute = () => {
  routePoints = []
  analysis.value = undefined
  planner.startText = ''
  planner.endText = ''
  planner.points = []
  mapRef.value?.clearRoute()
}

const parsePoint = (text) => {
  const values = text.split(',').map(Number)
  if (values.length !== 2 || values.some((value) => !Number.isFinite(value))) throw new Error('起终点应为 lon, lat')
  return values
}

const runPlan = async (savedRoute) => {
  try {
    planner.planning = true
    if (savedRoute && Array.isArray(savedRoute.points)) {
      planner.name = savedRoute.name || '默认航线 A'
      planner.startText = Array.isArray(savedRoute.start) ? savedRoute.start.join(', ') : ''
      planner.endText = Array.isArray(savedRoute.end) ? savedRoute.end.join(', ') : ''
      planner.points = savedRoute.points
      analysis.value = await analyzeRoute(selection, savedRoute.points, thresholds)
      return
    }
    const result = await planRoute(selection, parsePoint(planner.startText), parsePoint(planner.endText), thresholds)
    planner.points = result.points
    routePoints = result.points
    analysis.value = result.analysis
  } catch (reason) { 
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  } finally {
    planner.planning = false
  }
}

const handleMapClick = (payload) => {
  if (picking.value) {
    planner[`${picking.value}Text`] = `${payload.lon.toFixed(5)}, ${payload.lat.toFixed(5)}`
    picking.value = ''
    return
  }
  queryPoint(payload)
}

const persistRoute = async () => {
  try {
    await saveRoute({ name: planner.name, start: parsePoint(planner.startText), end: parsePoint(planner.endText), points: planner.points, level: selection.level, cycle: selection.cycle, forecast_hour: selection.forecastHour })
    savedRoutes.value = await listRoutes()
  } catch (reason) { 
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  }
}
const refreshRoutes = async () => { 
  try { 
    savedRoutes.value = await listRoutes() 
  } catch (reason) { 
    error.value = reason.message
    setTimeout(() => { error.value = '' }, 3000)
  } 
}
const removeRoute = async (id) => { 
  const deletedRoute = savedRoutes.value.find(r => r.route_id === id);
  await deleteRoute(id); 
  await refreshRoutes();
  if (deletedRoute && planner.name === deletedRoute.name) {
    clearRoute();
  }
}

const useZoomDefaultLayer = async (zoom) => {
  if (zoom < ZOOM_AVERAGE_LAYER || !options.levels.includes(AVERAGE_LAYER) || selection.level === AVERAGE_LAYER) return
  selection.level = AVERAGE_LAYER
  await loadWindField()
}

const applyTimes = async ({ autoLoad = true } = {}) => {
  error.value = ''
  Object.assign(options, { cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
  try {
    Object.assign(options, await getTimes(selection.source))
    const latest = options.valid_times?.at(-1)
    if (!latest) {
      wind.value = undefined
      heatmap.value = undefined
      metadata.value = undefined
      analysis.value = undefined
      error.value = `${selection.source === 'wrf' ? 'WRF 降尺度' : 'GFS 原始'}数据源没有可用的当前整点或未来预报时刻。`
      setTimeout(() => { error.value = '' }, 3000)
      return
    }
    selection.validTime = latest.label || ''
    selection.cycle = latest.cycle || options.cycles.at(-1)
    selection.forecastHour = latest.forecast_hour || options.forecast_hours[0]
    selection.level = options.levels.includes(selection.level) ? selection.level : (options.levels.includes('100m AGL') ? '100m AGL' : options.levels.at(-1))
    if (autoLoad) await loadWindField()
  } catch (reason) {
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  }
}

watch(() => selection.validTime, (validTime) => {
  const option = options.valid_times?.find((item) => item.label === validTime)
  if (option) {
    selection.cycle = option.cycle
    selection.forecastHour = option.forecast_hour
  }
})

watch(() => selection.source, async () => {
  clearRoute()
  await applyTimes()
})

onMounted(async () => {
  await applyTimes()
  await refreshRoutes()
})
</script>

<template>
  <div class="app-shell">
    <ControlPanel :options="options" :selection="selection" :layers="layers" :thresholds="thresholds" :planner="planner" :saved-routes="savedRoutes" :loading="loading" :picking="picking" @reload="loadWindField" @clear-route="clearRoute" @pick-start="picking = 'start'" @pick-end="picking = 'end'" @plan-route="runPlan" @save-route="persistRoute" @load-routes="refreshRoutes" @delete-route="removeRoute" />
    <WindMap ref="mapRef" :wind="wind" :heatmap="heatmap" :layers="layers" :thresholds="thresholds" :analysis="analysis" :planner="planner" @point-click="handleMapClick" @route-created="runRouteAnalysis" @zoom-changed="useZoomDefaultLayer" />
    <AnalysisPanel :metadata="metadata" :analysis="analysis" />
    <div v-if="error" class="error-toast" @click="error = ''">{{ error }}</div>
  </div>
</template>
