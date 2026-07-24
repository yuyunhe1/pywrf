<script setup>
import L from 'leaflet'
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { analyzeRoute, deleteRoute, getExportedRouteUrl, getExportedWaypointUrl, getGfsDownloadStatus, getHeatmap, getPoint, getTimes, getWind, listRoutes, planRoute, saveRoute } from './api'
import AnalysisPanel from './components/AnalysisPanel.vue'
import ControlPanel from './components/ControlPanel.vue'
import WindMap from './components/WindMap.vue'

const options = reactive({ cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
const DEFAULT_LOOKAHEAD_HOURS = 3
const selection = reactive({ source: 'gfs', cycle: '', forecastHour: DEFAULT_LOOKAHEAD_HOURS, validTime: '', level: '100m AGL' })
const layers = reactive({ heatmap: false, velocity: true, route: true, mutation: true })
const thresholds = reactive({ safe: 1.5, notice: 3.3, warning: 5.4, danger: 7.9, maxHorizontalWindShear: 5.4 })
const wind = ref()
const heatmap = ref()
const analysis = ref()
const metadata = ref()
const loading = ref(false)
const error = ref('')
const mapRef = ref()
const controlPanelRef = ref()
let routePoints = []
let suppressPlannerAutoClear = false
const activeRouteId = ref('')
const planner = reactive({
  name: '默认航线 A',
  algorithm: 'wa_lpa_star',
  aircraftModel: 'fixed_wing',
  strategy: 'wind_avoidance',
  startText: '',
  endText: '',
  points: [],
  missionItems: [],
  planning: false,
})
const savedRoutes = ref([])
const picking = ref('')
const ZOOM_AVERAGE_LAYER = 13
const AVERAGE_LAYER = '250-350m AGL average'
let pointRequestController
let downloadPollTimer

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
  const oneHour = 60 * 60 * 1000
  const target = Math.ceil((Date.now() + DEFAULT_LOOKAHEAD_HOURS * oneHour) / oneHour) * oneHour
  return times.find((item) => {
    const timestamp = parseSelectionTime(item)
    return Number.isFinite(timestamp) && timestamp >= target
  }) || times[0]
}

const showTemporaryError = (message) => {
  error.value = message
  setTimeout(() => { error.value = '' }, 3000)
}

const apiErrorMessage = (reason) => {
  const detail = reason.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return reason.message
}

const apiDownloadStatus = (reason) => {
  const detail = reason.response?.data?.detail
  return detail?.download || reason.response?.data?.download
}

const stopDownloadPolling = () => {
  if (!downloadPollTimer) return
  clearInterval(downloadPollTimer)
  downloadPollTimer = undefined
}

const checkDownloadStatus = async () => {
  if (selection.source !== 'gfs') {
    stopDownloadPolling()
    return
  }
  try {
    const status = await getGfsDownloadStatus()
    if (status.running) return
    stopDownloadPolling()
    if (status.return_code === 0) {
      await applyTimes()
    } else if (status.return_code !== null && status.return_code !== undefined) {
      showTemporaryError(`GFS 下载失败：${status.message || `exit ${status.return_code}`}`)
    }
  } catch (reason) {
    stopDownloadPolling()
    showTemporaryError(apiErrorMessage(reason))
  }
}

const startDownloadPolling = () => {
  if (selection.source !== 'gfs' || downloadPollTimer) return
  downloadPollTimer = setInterval(checkDownloadStatus, 10000)
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
    if (apiDownloadStatus(reason)?.running) startDownloadPolling()
    showTemporaryError(apiErrorMessage(reason))
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

const currentWindShearSettings = () => ({
  enabled: true,
  horizontal: {
    enabled: true,
    hard_delta_wind_vector_ms: Number(thresholds.maxHorizontalWindShear),
    hard_constraint_enabled: true,
  },
})

const runRouteAnalysis = async (points) => {
  routePoints = points
  activeRouteId.value = ''
  analysis.value = await analyzeRoute(selection, points, thresholds, currentWindShearSettings())
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

const clearPlannedRoute = ({ keepEndpoints = true } = {}) => {
  routePoints = []
  analysis.value = undefined
  planner.points = []
  planner.missionItems = []
  activeRouteId.value = ''
  mapRef.value?.clearDrawnRoute?.()
  if (!keepEndpoints) {
    planner.startText = ''
    planner.endText = ''
    picking.value = ''
    mapRef.value?.clearRoute()
  }
}

const clearRoute = () => {
  clearPlannedRoute({ keepEndpoints: false })
}

const runWithPlannerAutoClearSuppressed = async (callback) => {
  suppressPlannerAutoClear = true
  try {
    return await callback()
  } finally {
    await nextTick()
    suppressPlannerAutoClear = false
  }
}

const parsePoint = (text) => {
  const values = text.split(',').map((value) => Number(value.trim()))
  if (values.length < 2 || values.slice(0, 2).some((value) => !Number.isFinite(value))) throw new Error('起终点应为 lon, lat')
  return values.slice(0, 2)
}

const lonLatPair = (point) => {
  if (!Array.isArray(point) || point.length < 2) return null
  const lon = Number(point[0])
  const lat = Number(point[1])
  return Number.isFinite(lon) && Number.isFinite(lat) ? [lon, lat] : null
}

const normalizeRoutePoints = (points) => {
  if (!Array.isArray(points)) return []
  return points.map((point) => {
    if (Array.isArray(point)) {
      const result = [Number(point[0]), Number(point[1])]
      point.slice(2).map(Number).forEach((value) => {
        if (Number.isFinite(value)) result.push(value)
      })
      return result
    }
    if (point && typeof point === 'object') {
      const lon = Number(point.lon ?? point.longitude ?? point.lng)
      const lat = Number(point.lat ?? point.latitude)
      const altitudeAmsl = Number(point.altitude_amsl_m ?? point.altitude_m ?? point.ele)
      const altitudeAgl = Number(point.altitude_agl_m ?? point.agl_m)
      const terrain = Number(point.terrain_height_m ?? point.terrain_alt_m ?? point.hgt_surface_m)
      const result = [lon, lat]
      if (Number.isFinite(altitudeAmsl)) result.push(altitudeAmsl)
      else if (Number.isFinite(altitudeAgl) && Number.isFinite(terrain)) result.push(altitudeAgl + terrain)
      if (Number.isFinite(altitudeAgl)) result.push(altitudeAgl)
      if (Number.isFinite(terrain)) result.push(terrain)
      return result
    }
    return [Number.NaN, Number.NaN]
  }).filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat))
}

const applyRouteToPlanner = async (name, points, { persist = false, routeId = '', missionItems = null } = {}) => {
  const normalizedPoints = normalizeRoutePoints(points)
  if (normalizedPoints.length < 2) throw new Error('导入的航线至少需要包含 2 个有效航点')
  const start = lonLatPair(normalizedPoints[0])
  const end = lonLatPair(normalizedPoints[normalizedPoints.length - 1])
  if (!start || !end) throw new Error('导入的航点缺少有效经纬度')
  planner.name = name || '导入航线'
  planner.startText = start.join(', ')
  planner.endText = end.join(', ')
  planner.points = normalizedPoints
  planner.missionItems = Array.isArray(missionItems) ? missionItems.map((item) => ({ ...item })) : []
  routePoints = normalizedPoints
  activeRouteId.value = routeId || ''
  try {
    analysis.value = await analyzeRoute(selection, normalizedPoints, thresholds, currentWindShearSettings())
  } catch (reason) {
    console.warn('导入航线分析失败:', reason)
    analysis.value = undefined
  }
  if (persist) {
    const saved = await saveRoute({
      name: planner.name,
      start,
      end,
      points: normalizedPoints,
      level: selection.level,
      cycle: selection.cycle || null,
      forecast_hour: selection.forecastHour,
      mission_items: planner.missionItems.length ? planner.missionItems : null,
    })
    activeRouteId.value = saved.route_id || ''
    savedRoutes.value = await listRoutes()
  }
}

const downloadExportedFile = (url, fileName) => {
  if (!fileName) return
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

const downloadExportedFiles = (saved) => {
  const jsonFileName = saved?.exported_json?.file_name
  const waypointFileName = saved?.exported_waypoints?.file_name || saved?.exported_json?.waypoint_file_name
  if (jsonFileName) downloadExportedFile(getExportedRouteUrl(jsonFileName), jsonFileName)
  if (waypointFileName) {
    window.setTimeout(() => {
      downloadExportedFile(getExportedWaypointUrl(waypointFileName), waypointFileName)
    }, 120)
  }
}

const runPlan = async (savedRoute) => {
  try {
    planner.planning = true
    if (savedRoute && Array.isArray(savedRoute.points)) {
      await runWithPlannerAutoClearSuppressed(() => applyRouteToPlanner(savedRoute.name || '默认航线 A', savedRoute.points, {
        routeId: savedRoute.route_id,
        missionItems: savedRoute.mission_items,
      }))
      return
    }
    activeRouteId.value = ''
    planner.missionItems = []
    const result = await planRoute(
      selection,
      parsePoint(planner.startText),
      parsePoint(planner.endText),
      thresholds,
      planner.algorithm,
      planner.aircraftModel,
      planner.strategy,
      currentWindShearSettings(),
    )
    planner.points = result.points
    routePoints = result.points
    activeRouteId.value = ''
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
    const points = normalizeRoutePoints(planner.points)
    const start = lonLatPair(points[0]) || parsePoint(planner.startText)
    const end = lonLatPair(points[points.length - 1]) || parsePoint(planner.endText)
    const saved = await saveRoute({
      name: planner.name,
      start,
      end,
      points,
      level: selection.level,
      cycle: selection.cycle,
      forecast_hour: selection.forecastHour,
      mission_items: planner.missionItems.length ? planner.missionItems : null,
    })
    activeRouteId.value = saved.route_id || ''
    savedRoutes.value = await listRoutes()
    downloadExportedFiles(saved)
  } catch (reason) { 
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 3000)
  }
}

const importRouteFile = async ({ name, points, missionItems }) => {
  try {
    await runWithPlannerAutoClearSuppressed(() => applyRouteToPlanner(name, points, { persist: true, missionItems }))
  } catch (reason) {
    error.value = reason.response?.data?.detail || reason.message
    setTimeout(() => { error.value = '' }, 4000)
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
  await deleteRoute(id); 

  await refreshRoutes();
  controlPanelRef.value?.refreshJsonDialog?.()
  if (activeRouteId.value === id) activeRouteId.value = ''
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
    if (response.download?.running) startDownloadPolling()
    const availableTimes = keepUpcomingTimes(response.valid_times || [])
    Object.assign(options, { ...response, valid_times: availableTimes })
    const defaultTime = pickDefaultTime(options.valid_times || [])
    if (!defaultTime) {
      wind.value = undefined
      heatmap.value = undefined
      metadata.value = undefined
      analysis.value = undefined
      if (response.download?.running) {
        showTemporaryError('GFS 数据正在下载，完成后将自动刷新。')
        return
      }
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
    if (apiDownloadStatus(reason)?.running) startDownloadPolling()
    showTemporaryError(apiErrorMessage(reason))
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

watch(() => [planner.algorithm, planner.strategy], () => {
  if (suppressPlannerAutoClear) return
  clearPlannedRoute({ keepEndpoints: true })
})

watch(() => [planner.startText, planner.endText], () => {
  if (suppressPlannerAutoClear) return
  clearPlannedRoute({ keepEndpoints: true })
})

onMounted(async () => {
  await applyTimes()
  await refreshRoutes()
})

onBeforeUnmount(() => {
  stopDownloadPolling()
})
</script>

<template>
  <div class="app-shell">
    <header class="app-title">
      <h1>御风智航-面向低空无人机通航决策的风场预报平台</h1>
      <p class="en-title">YUFENG SMART FLIGHT - LOW-ALTITUDE UAV FLIGHT DECISION WIND FORECAST PLATFORM</p>
    </header>
    <ControlPanel ref="controlPanelRef" :options="options" :selection="selection" :layers="layers" :thresholds="thresholds" :planner="planner" :saved-routes="savedRoutes" :area-selection="areaSelection" :area-presets="areaPresets" :loading="loading" :picking="picking" @reload="loadWindField" @clear-route="clearRoute" @pick-start="picking = 'start'" @pick-end="picking = 'end'" @plan-route="runPlan" @save-route="persistRoute" @load-routes="refreshRoutes" @delete-route="removeRoute" @focus-area="focusArea" @import-route-file="importRouteFile" />
    <WindMap ref="mapRef" :wind="wind" :heatmap="heatmap" :layers="layers" :thresholds="thresholds" :analysis="analysis" :planner="planner" @point-click="handleMapClick" @route-created="runRouteAnalysis" @zoom-changed="useZoomDefaultLayer" />
    <AnalysisPanel :metadata="metadata" :analysis="analysis" :thresholds="thresholds" />
    <div v-if="error" class="error-toast" @click="error = ''">{{ error }}</div>
  </div>
</template>
