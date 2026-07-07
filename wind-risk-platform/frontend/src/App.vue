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

const areaPresets = {
  province: [
    { value: 'beijing', label: '北京市', center: [39.90, 116.40], zoom: 9 },
    { value: 'tianjin', label: '天津市', center: [39.13, 117.20], zoom: 9 },
    { value: 'hebei', label: '河北省', center: [38.04, 114.51], zoom: 7 },
    { value: 'shanxi', label: '山西省', center: [37.87, 112.55], zoom: 7 },
    { value: 'neimenggu', label: '内蒙古自治区', center: [40.82, 111.67], zoom: 5 },
    { value: 'liaoning', label: '辽宁省', center: [41.80, 123.43], zoom: 7 },
    { value: 'jilin', label: '吉林省', center: [43.82, 125.32], zoom: 7 },
    { value: 'heilongjiang', label: '黑龙江省', center: [45.75, 126.63], zoom: 6 },
    { value: 'shanghai', label: '上海市', center: [31.23, 121.47], zoom: 9 },
    { value: 'jiangsu', label: '江苏省', center: [32.06, 118.78], zoom: 7 },
    { value: 'zhejiang', label: '浙江省', center: [30.27, 120.15], zoom: 7 },
    { value: 'anhui', label: '安徽省', center: [31.86, 117.28], zoom: 7 },
    { value: 'fujian', label: '福建省', center: [26.07, 119.30], zoom: 7 },
    { value: 'jiangxi', label: '江西省', center: [28.67, 115.89], zoom: 7 },
    { value: 'shandong', label: '山东省', center: [36.67, 117.00], zoom: 7 },
    { value: 'henan', label: '河南省', center: [34.76, 113.65], zoom: 7 },
    { value: 'hubei', label: '湖北省', center: [30.59, 114.30], zoom: 7 },
    { value: 'hunan', label: '湖南省', center: [28.23, 112.93], zoom: 7 },
    { value: 'guangdong', label: '广东省', center: [23.13, 113.27], zoom: 7 },
    { value: 'guangxi', label: '广西壮族自治区', center: [22.82, 108.32], zoom: 7 },
    { value: 'hainan', label: '海南省', center: [20.02, 110.35], zoom: 8 },
    { value: 'chongqing', label: '重庆市', center: [29.56, 106.55], zoom: 7 },
    { value: 'sichuan', label: '四川省', center: [30.67, 104.07], zoom: 6 },
    { value: 'guizhou', label: '贵州省', center: [26.65, 106.63], zoom: 7 },
    { value: 'yunnan', label: '云南省', center: [25.04, 102.71], zoom: 6 },
    { value: 'xizang', label: '西藏自治区', center: [29.65, 91.13], zoom: 5 },
    { value: 'shanxi1', label: '陕西省', center: [34.27, 108.95], zoom: 7 },
    { value: 'gansu', label: '甘肃省', center: [36.06, 103.83], zoom: 6 },
    { value: 'qinghai', label: '青海省', center: [36.62, 101.78], zoom: 6 },
    { value: 'ningxia', label: '宁夏回族自治区', center: [38.47, 106.27], zoom: 8 },
    { value: 'xinjiang', label: '新疆维吾尔自治区', center: [43.79, 87.62], zoom: 5 },
    { value: 'taiwan', label: '台湾省', center: [25.04, 121.56], zoom: 8 },
    { value: 'hongkong', label: '香港特别行政区', center: [22.30, 114.17], zoom: 10 },
    { value: 'macao', label: '澳门特别行政区', center: [22.20, 113.54], zoom: 11 },
  ],
  region: [
    { value: 'china', label: '全国', bounds: [[18.0, 73.0], [54.0, 135.0]] },
    { value: 'north-china', label: '华北', bounds: [[34.5, 110.0], [43.5, 120.5]] },
    { value: 'east-china', label: '华东', bounds: [[24.0, 114.0], [37.5, 123.8]] },
    { value: 'south-china', label: '华南', bounds: [[18.0, 104.0], [27.8, 120.5]] },
    { value: 'central-china', label: '华中', bounds: [[24.5, 108.5], [34.8, 116.8]] },
    { value: 'southwest-china', label: '西南', bounds: [[21.0, 97.0], [34.5, 110.5]] },
    { value: 'northwest-china', label: '西北', bounds: [[31.0, 73.0], [49.5, 110.5]] },
    { value: 'northeast-china', label: '东北', bounds: [[38.5, 118.5], [53.8, 135.2]] },
    { value: 'hk-mo-tw', label: '港澳台', bounds: [[20.8, 113.0], [26.8, 122.2]] },
  ],
  project: [
    { value: 'wanzhi', label: '湾沚区', center: [31.13, 118.57], zoom: 11 },
  ],
}
const areaSelection = reactive({ province: '', region: '', project: '' })

const parseSelectionTime = (item) => {
  const raw = item?.valid_time || item?.label
  if (!raw) return Number.NaN
  const utcText = raw.includes('UTC') ? raw : `${raw} UTC`
  return new Date(utcText.replace(' UTC', ':00Z').replace(' ', 'T')).getTime()
}

const keepUpcomingTimes = (times) => {
  const now = Date.now()
  const filtered = times.filter((item) => {
    const timestamp = parseSelectionTime(item)
    return Number.isFinite(timestamp) && timestamp >= now
  })
  return filtered.length ? filtered : times
}

const pickDefaultTime = (times) => {
  if (!times.length) return null
  const target = Date.now() + 3 * 60 * 60 * 1000
  return times.find((item) => {
    const timestamp = parseSelectionTime(item)
    return Number.isFinite(timestamp) && timestamp >= target
  }) || times[0]
}

const loadWindField = async () => {
  if (!selection.source || !selection.level || !selection.validTime) return
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

const focusArea = ({ kind, value }) => {
  for (const key of Object.keys(areaSelection)) areaSelection[key] = key === kind ? value : ''
  if (!value) return
  const target = areaPresets[kind]?.find((item) => item.value === value)
  if (target) mapRef.value?.focusArea(target)
}

const applyTimes = async ({ autoLoad = true } = {}) => {
  error.value = ''
  Object.assign(options, { cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
  try {
    const response = await getTimes(selection.source)
    const availableTimes = keepUpcomingTimes(response.valid_times || [])
    Object.assign(options, { ...response, valid_times: availableTimes })
    const defaultTime = pickDefaultTime(options.valid_times || [])
    if (!defaultTime) {
      wind.value = undefined
      heatmap.value = undefined
      metadata.value = undefined
      analysis.value = undefined
      error.value = `${selection.source === 'wrf' ? 'WRF 降尺度' : 'GFS 原始'}数据源没有可用的当前整点或未来预报时刻。`
      setTimeout(() => { error.value = '' }, 3000)
      return
    }
    selection.validTime = defaultTime.label || ''
    selection.cycle = defaultTime.cycle || options.cycles.at(-1)
    selection.forecastHour = defaultTime.forecast_hour || options.forecast_hours[0]
    selection.level = options.levels.includes(selection.level) ? selection.level : (options.levels.includes('100m AGL') ? '100m AGL' : options.levels.at(-1))
    if (autoLoad) await loadWindField()
  } catch (reason) {
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  }
}

watch(() => selection.validTime, (validTime) => {
  if (!validTime) return
  const option = options.valid_times?.find((item) => item.label === validTime)
  if (option) {
    selection.cycle = option.cycle
    selection.forecastHour = option.forecast_hour
  }
})

watch(() => selection.source, async (source) => {
  clearRoute()
  if (!source) {
    Object.assign(options, { cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
    wind.value = undefined
    heatmap.value = undefined
    metadata.value = undefined
    return
  }
  await applyTimes()
})

onMounted(async () => {
  await applyTimes()
  await refreshRoutes()
})
</script>

<template>
  <div class="app-shell">
    <header class="app-title">
      <p class="app-title-eyebrow">WIND FORECAST FOR LOW-ALTITUDE DECISION</p>
      <h1>面向低空无人机通航决策的风场预报平台</h1>
    </header>
    <ControlPanel :options="options" :selection="selection" :layers="layers" :thresholds="thresholds" :planner="planner" :saved-routes="savedRoutes" :area-selection="areaSelection" :area-presets="areaPresets" :loading="loading" :picking="picking" @reload="loadWindField" @clear-route="clearRoute" @pick-start="picking = 'start'" @pick-end="picking = 'end'" @plan-route="runPlan" @save-route="persistRoute" @load-routes="refreshRoutes" @delete-route="removeRoute" @focus-area="focusArea" />
    <WindMap ref="mapRef" :wind="wind" :heatmap="heatmap" :layers="layers" :thresholds="thresholds" :analysis="analysis" :planner="planner" @point-click="handleMapClick" @route-created="runRouteAnalysis" @zoom-changed="useZoomDefaultLayer" />
    <AnalysisPanel :metadata="metadata" :analysis="analysis" :thresholds="thresholds" />
    <div v-if="error" class="error-toast" @click="error = ''">{{ error }}</div>
  </div>
</template>
