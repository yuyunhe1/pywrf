<script setup>
import L from 'leaflet'
import { onMounted, reactive, ref, watch } from 'vue'
import { analyzeRoute, getHeatmap, getPoint, getTimes, getWind } from './api'
import AnalysisPanel from './components/AnalysisPanel.vue'
import ControlPanel from './components/ControlPanel.vue'
import WindMap from './components/WindMap.vue'

const options = reactive({ cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
const selection = reactive({ source: 'gfs', cycle: '', forecastHour: 3, validTime: '', level: '100m AGL' })
const layers = reactive({ heatmap: true, velocity: true, route: true })
const thresholds = reactive({ safe: 3, notice: 6, warning: 8, danger: 10 })
const wind = ref()
const heatmap = ref()
const analysis = ref()
const metadata = ref()
const loading = ref(false)
const error = ref('')
const mapRef = ref()
let routePoints = []

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
  } finally {
    loading.value = false
  }
}

const runRouteAnalysis = async (points) => {
  routePoints = points
  analysis.value = await analyzeRoute(selection, points, thresholds)
}

const queryPoint = async ({ lon, lat, map }) => {
  try {
    const point = await getPoint(selection, lon, lat)
    L.popup()
      .setLatLng([lat, lon])
      .setContent(`<strong>${point.wind_speed.toFixed(2)} m/s</strong><br>气象风向：${point.wind_direction_from.toFixed(0)}°<br>U：${point.u.toFixed(2)} m/s<br>V：${point.v.toFixed(2)} m/s<br><small>最近格点 ${point.lon.toFixed(2)}, ${point.lat.toFixed(2)}</small>`)
      .openOn(map)
  } catch (reason) {
    error.value = reason.response?.data?.detail || reason.message
  }
}

const clearRoute = () => {
  routePoints = []
  analysis.value = undefined
  mapRef.value?.clearRoute()
}

const applyTimes = async ({ autoLoad = true } = {}) => {
  error.value = ''
  Object.assign(options, { cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
  Object.assign(options, await getTimes(selection.source))
  const latest = options.valid_times?.at(-1)
  if (!latest) {
    wind.value = undefined
    heatmap.value = undefined
    metadata.value = undefined
    analysis.value = undefined
    error.value = `${selection.source === 'wrf' ? 'WRF 降尺度' : 'GFS 原始'}数据源没有可用的当前整点或未来预报时刻。`
    return
  }
  selection.validTime = latest.label || ''
  selection.cycle = latest.cycle || options.cycles.at(-1)
  selection.forecastHour = latest.forecast_hour || options.forecast_hours[0]
  selection.level = options.levels.includes(selection.level) ? selection.level : (options.levels.includes('100m AGL') ? '100m AGL' : options.levels.at(-1))
  if (autoLoad) await loadWindField()
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
})
</script>

<template>
  <div class="app-shell">
    <ControlPanel :options="options" :selection="selection" :layers="layers" :thresholds="thresholds" :loading="loading" @reload="loadWindField" @clear-route="clearRoute" />
    <WindMap ref="mapRef" :wind="wind" :heatmap="heatmap" :layers="layers" :analysis="analysis" @point-click="queryPoint" @route-created="runRouteAnalysis" />
    <AnalysisPanel :metadata="metadata" :analysis="analysis" />
    <div v-if="error" class="error-toast" @click="error = ''">{{ error }}</div>
  </div>
</template>
