<script setup>
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import 'leaflet-velocity'
import 'leaflet-velocity/dist/leaflet-velocity.css'
import { onBeforeUnmount, onMounted, watch, ref, computed } from 'vue'
import ElTooltip from 'element-plus/es/components/tooltip/index'
import { createWindSpeedCanvasLayer } from '../layers/WindSpeedCanvasLayer'
import { getRouteChartSample } from '../utils/routeChartSelection'
import { formatReplayTime } from '../utils/flightReplay'
import { createReplayVelocityLayer } from '../layers/ReplayVelocityLayer'
import { windFeelForRange } from '../utils/windFeel'
import { mergeSearchCorridors } from '../utils/searchCorridors'
import { RECOMMENDATION_COLORS } from '../utils/recommendations'
import { windBandIndex, WIND_BAND_COLORS } from '../utils/windBands.js'
import { createFrameRenderGate } from '../utils/frameRenderGate.js'

const props = defineProps({
  wind: Object,
  heatmap: Object,
  layers: { type: Object, required: true },
  thresholds: { type: Object, required: true },
  analysis: Object,
  planner: Object,
  selectedSampleIndex: { type: Number, default: null },
  metadata: Object,
  flightMode: Boolean,
  flightProfile: Object,
  aircraftPosition: Object,
  customDrawing: Object,
  layoutInsets: { type: Object, default: () => ({ left: 12, right: 12 }) },
  dockHeight: { type: Number, default: 0 },
  searchProgress: Object,
  recommendations: Array,
  recommendationPreview: Boolean,
  detourEndpoints: Array,
  frameKey: String,
  planningPoints: Array,
})
const emit = defineEmits(['point-click', 'route-created', 'zoom-changed', 'drawing-point-add', 'drawing-point-move', 'wind-frame-ready'])
const frameGate = createFrameRenderGate((key) => emit('wind-frame-ready', key))

let map
let heatLayer
let velocityLayers = []
let routeGroup
let searchGroup
let recommendationGroup
let routeSelectionGroup
let flightProgressGroup
let drawnGroup
let customDraftGroup
let draftMarkers = []
let draftLine
let draftOutline
let drawingModeActive = false
let restoreDoubleClickZoom = false

// WRF/WPS domain center near Wuhu Aerospace Industrial Park, about 10 km x 10 km.
const CHINA_INITIAL_BOUNDS = [[18.0, 73.0], [54.0, 135.0]]

const searchLines = computed(() => props.customDrawing?.active || !props.layers.route ? [] : mergeSearchCorridors(props.searchProgress))
const renderSearchCorridors = () => {
  if (!searchGroup) return
  searchGroup.clearLayers()
  for (const line of searchLines.value) {
    for (const style of [{ color: '#0f172a', weight: 6, opacity: .9, className: 'search-corridor-outline' },
      { color: '#00f3ff', weight: 4, opacity: 1, className: `search-corridor search-corridor-${line.direction}` }]) {
      L.polyline(line.points.map(([lon, lat]) => [lat, lon]), {
        pane: 'searchCorridorPane', ...style, dashArray: '10 8', dashOffset: '0',
        lineCap: 'round', lineJoin: 'round', interactive: false,
      }).addTo(searchGroup)
    }
  }
}

const renderRecommendations = () => {
  if (!recommendationGroup) return
  recommendationGroup.clearLayers()
  if (props.customDrawing?.active || !props.layers.route) return
  for (const [index, card] of (props.recommendations || []).entries()) {
    const points = card.segment_points || card.plan.points
    for (const style of [{ color: '#0f172a', weight: 6, opacity: .9 }, { color: RECOMMENDATION_COLORS[index], weight: 4, opacity: 1 }]) {
      L.polyline(points.map(([lon,lat]) => [lat,lon]), { pane: 'recommendationPane', ...style, dashArray: '10 8',
        dashOffset: '0', lineCap: 'round', lineJoin: 'round', interactive: false, className: 'recommendation-route' }).addTo(recommendationGroup)
    }
    if (points.length >= 2) {
      const middle=points.length === 2 ? [(points[0][0]+points[1][0])/2,(points[0][1]+points[1][1])/2] : points[Math.floor(points.length/2)]
      L.circleMarker([middle[1],middle[0]], { pane: 'recommendationPane', radius: 4,
        color: RECOMMENDATION_COLORS[index], fillColor: RECOMMENDATION_COLORS[index], fillOpacity: 1, interactive: false })
        .bindTooltip(`方案 ${index+1}`, { permanent: true, direction: 'top', className: 'recommendation-label' }).addTo(recommendationGroup)
    }
  }
  for (const [index, point] of (props.detourEndpoints || []).entries()) {
    L.circleMarker([point[1],point[0]], { pane: 'recommendationPane', radius: 7, color: '#ffb347', weight: 2, fillOpacity: .8 })
      .bindTooltip(index === 0 ? '绕行端点 A' : '绕行端点 B', { permanent: true, direction: 'bottom' }).addTo(recommendationGroup)
  }
}

const renderWind = () => {
  if (!map) return
  const previous = velocityLayers
  velocityLayers = []
  if (props.layers.velocity && props.wind?.velocity) {
    // All grades share one unchanged U/V grid and one projected particle field.
    const thresholds = { ...props.thresholds }
    const key = props.frameKey
    const options = {
      data: props.wind.velocity, displayValues: false,
      velocityScale: .007, maxVelocity: thresholds.danger + 5,
      lineWidth: 4, particleMultiplier: 1 / 400, particleAge: 60,
      opacity: .98, colorScale: [...WIND_BAND_COLORS],
      colorIndex: (speed) => windBandIndex(speed, thresholds),
      visibleBands: [...activeBlocks.value],
      onFrameReady: () => frameGate.ready(key, 'velocity'),
    }
    const existing = previous.find((layer) => layer.flightBandKey === 'shared')
    const layer = existing || createReplayVelocityLayer(options).addTo(map)
    if (existing) existing.setOptions(options)
    layer.flightBandKey = 'shared'
    velocityLayers.push(layer)
  }
  previous.filter((layer) => !velocityLayers.includes(layer)).forEach((layer) => map.removeLayer(layer))
}

const heatBlocks = computed(() => [
  { min: 0, max: props.thresholds.safe, color: '#00b84b', label: `0-${props.thresholds.safe}` },
  { min: props.thresholds.safe, max: props.thresholds.notice, color: '#e6a100', label: `${props.thresholds.safe}-${props.thresholds.notice}` },
  { min: props.thresholds.notice, max: props.thresholds.warning, color: '#e66b00', label: `${props.thresholds.notice}-${props.thresholds.warning}` },
  { min: props.thresholds.warning, max: props.thresholds.danger, color: '#c21e1f', label: `${props.thresholds.warning}-${props.thresholds.danger}` },
  { min: props.thresholds.danger, max: Infinity, color: '#6a1b9a', label: `${props.thresholds.danger}+` }
].map((block) => ({ ...block, feel: windFeelForRange(block.min, block.max) })))
const activeBlocks = ref([])

const toggleBlock = (i) => {
  const index = activeBlocks.value.indexOf(i)
  if (index > -1) {
    activeBlocks.value.splice(index, 1)
  } else {
    activeBlocks.value.push(i)
  }
  renderHeatmap()
  renderWind()
}

const renderHeatmap = () => {
  if (!map) return
  if (props.layers.heatmap && props.heatmap?.wind_speed) {
    let filterRanges = null
    if (activeBlocks.value.length > 0) {
      filterRanges = activeBlocks.value.map(i => [heatBlocks.value[i].min, heatBlocks.value[i].max])
    }
    const options = {
      filterRanges,
      windVelocity: props.wind?.velocity,
      thresholds: props.thresholds,
      showMutation: props.layers.mutation
    }
    if (heatLayer) heatLayer.setData(props.heatmap.wind_speed, options)
    else heatLayer = createWindSpeedCanvasLayer(props.heatmap.wind_speed, options).addTo(map)
    const key = props.frameKey
    requestAnimationFrame(() => frameGate.ready(key, 'heatmap'))
  } else if (heatLayer) {
    map.removeLayer(heatLayer)
    heatLayer = null
  }
}

const renderRoute = () => {
  if (!map) return
  routeGroup.clearLayers()
  if (props.customDrawing?.active) return

  const parsePointSafely = (text) => {
    if (!text) return null
    const parts = text.split(',')
    if (parts.length < 2) return null
    const lon = Number(parts[0])
    const lat = Number(parts[1])
    return Number.isFinite(lon) && Number.isFinite(lat) ? { lon, lat } : null
  }
  const addEndpointMarker = (point, label, color) => {
    if (!point) return
    L.circleMarker([point.lat, point.lon], {
      radius: 6,
      fillColor: color,
      color: '#fff',
      weight: 2,
      opacity: 1,
      fillOpacity: 1
    }).bindTooltip(label, { permanent: true, direction: 'right' }).addTo(routeGroup)
  }

  const samples = Array.isArray(props.planningPoints) ? props.flightProfile?.analysis?.samples || []
    : props.analysis?.samples?.length ? props.analysis.samples : (props.planner?.points || []).map((point) => ({ lon: point[0], lat: point[1] }))
  const fallbackStart = samples.length >= 2 ? samples[0] : null
  const fallbackEnd = samples.length >= 2 ? samples[samples.length - 1] : null
  const startPoint = parsePointSafely(props.planner?.startText) || fallbackStart
  const endPoint = parsePointSafely(props.planner?.endText) || fallbackEnd

  addEndpointMarker(startPoint, '起点', '#44d7b6')
  addEndpointMarker(endPoint, '终点', '#ea5455')

  if (!props.layers.route) return

  if (samples.length < 2) return
  if (props.recommendationPreview && !props.planner?.points?.length) return
  const routeLatLngs = Array.isArray(props.planningPoints) ? props.planningPoints.map(point => [point[1], point[0]])
    : (props.flightMode || props.recommendationPreview) && props.planner?.points?.length
      ? props.planner.points.map((point) => [point[1], point[0]]) : samples.map((item) => [item.lat, item.lon])
  if (routeLatLngs.length < 2) return

  const start = samples[0]
  const end = samples[samples.length - 1]
  // 2. 规划航线 - 底层（极简黑色实线边缘，增强对比度）
  L.polyline(routeLatLngs, {
    color: '#0f172a',
    weight: 6,
    opacity: 0.9,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(routeGroup)

  // 3. 所有航线统一使用旧版蓝色粗实线；风险由右侧通航决策与曲线展示。
  L.polyline(routeLatLngs, {
    color: '#00f3ff',
    weight: 4,
    opacity: 1,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(routeGroup)

  if (props.flightMode && props.flightProfile && !Array.isArray(props.planningPoints)) {
    props.flightProfile.events.forEach((event) => {
      const label = document.createElement('div')
      label.textContent = `${formatReplayTime(event.eta)} 北京时间 · ${event.reason}`
      L.circleMarker([event.lat, event.lon], { color: '#fff', weight: 1, fillColor: '#ea5455',
        fillOpacity: 1, radius: 4 }).bindTooltip(label).addTo(routeGroup)
    })
  }
}

const clearRoute = () => {
  drawnGroup?.clearLayers()
  routeGroup?.clearLayers()
  routeSelectionGroup?.clearLayers()
  flightProgressGroup?.clearLayers()
}

const renderFlightProgress = () => {
  if (!map || !flightProgressGroup) return
  flightProgressGroup.clearLayers()
  const profile = props.flightProfile, position = props.aircraftPosition
  if (props.customDrawing?.active || !props.flightMode || !props.layers.route || !profile || !position) return
  const passed = profile.analysis.samples.filter((sample) => sample.elapsed_seconds <= position.elapsed_seconds)
    .map((sample) => [sample.lat, sample.lon])
  passed.push([position.lat, position.lon])
  if (passed.length > 1) L.polyline(passed, { color: '#e2e8f0', weight: 2, dashArray: '7, 6',
    opacity: .75, interactive: false, pane: 'flightProgressPane' }).addTo(flightProgressGroup)
  const icon = L.divIcon({ className: 'flight-aircraft-icon', iconSize: [28, 28], iconAnchor: [14, 14],
    html: `<svg width="28" height="28" viewBox="0 0 28 28" role="img" aria-label="飞机预计位置" style="transform: rotate(${position.flight_heading}deg); filter: drop-shadow(0 1px 3px #000)"><path d="M14 2 L18 11 L25 17 L25 20 L17 17 L17 23 L20 26 L14 24 L8 26 L11 23 L11 17 L3 20 L3 17 L10 11 Z" fill="white" stroke="#0f172a" stroke-width="1.3"/></svg>` })
  const at = new Date(Date.parse(profile.departure_at) + position.elapsed_seconds * 1000).toISOString()
  L.marker([position.lat, position.lon], { icon, interactive: false, pane: 'flightProgressPane' })
    .bindTooltip(`飞机预计位置 · ${formatReplayTime(at)} 北京时间`, { direction: 'top', offset: [0, -10] })
    .addTo(flightProgressGroup)
}

const renderRouteSelection = () => {
  if (!map || !routeSelectionGroup) return
  routeSelectionGroup.clearLayers()
  if (props.customDrawing?.active || !props.layers.route) return
  const sample = getRouteChartSample(props.analysis?.samples, props.selectedSampleIndex)
  if (!sample) return
  const latlng = [sample.lat, sample.lon]
  L.circleMarker(latlng, {
    pane: 'routeSelectionPane',
    radius: 15,
    color: '#ff3b30',
    weight: 2,
    fillColor: '#ff3b30',
    fillOpacity: 0.2,
    interactive: false,
  }).addTo(routeSelectionGroup)
  const marker = L.circleMarker(latlng, {
    pane: 'routeSelectionPane',
    radius: 7,
    color: '#fff',
    weight: 2,
    fillColor: '#ff3b30',
    fillOpacity: 1,
    interactive: false,
  }).addTo(routeSelectionGroup)
  const label = document.createElement('div')
  label.style.whiteSpace = 'pre-line'
  label.style.width = '280px'
  label.style.lineHeight = '1.6'
  label.textContent = `航线里程 ${Number(sample.distance_km || 0).toFixed(3)} km\n位置 ${sample.lon.toFixed(5)}, ${sample.lat.toFixed(5)}`
  if (props.flightMode) label.textContent += `\n预计经过 ${formatReplayTime(sample.eta)} 北京时间\n风速 ${Number.isFinite(sample.wind_speed) ? sample.wind_speed.toFixed(2) + ' m/s' : '数据不足'}\n${sample.reason}`
  marker.bindTooltip(label, { permanent: true, direction: 'top', offset: [0, -15] })
  map.panInside(latlng, { padding: [40, 40] })
}

const clearDrawnRoute = () => {
  drawnGroup?.clearLayers()
}

const synchronizeDrawingMode = () => {
  if (!map) return
  const active = Boolean(props.customDrawing?.active)
  if (active === drawingModeActive) return
  drawingModeActive = active
  if (active) {
    map.closePopup()
    drawnGroup.clearLayers()
    restoreDoubleClickZoom = map.doubleClickZoom.enabled()
    map.doubleClickZoom.disable()
  } else {
    if (restoreDoubleClickZoom) map.doubleClickZoom.enable()
  }
}

const renderCustomRoute = () => {
  if (!map || !customDraftGroup) return
  customDraftGroup.clearLayers()
  draftMarkers = []
  draftLine = draftOutline = undefined
  if (!props.customDrawing?.active) return
  const points = props.customDrawing.points.map(([lon, lat]) => [lat, lon])
  if (points.length > 1) {
    draftOutline = L.polyline(points, { pane: 'customRoutePane', color: '#0f172a', weight: 6, interactive: false }).addTo(customDraftGroup)
    draftLine = L.polyline(points, { pane: 'customRoutePane', color: '#00f3ff', weight: 4, interactive: false,
      className: 'custom-route-preview' }).addTo(customDraftGroup)
  }
  points.forEach((point, index) => {
    const marker = L.marker(point, { pane: 'customRoutePane', draggable: !props.customDrawing.saving,
      bubblingMouseEvents: false, autoPan: true, title: `航点 ${index + 1}`,
      icon: L.divIcon({ className: 'custom-route-waypoint', iconSize: [28, 28], iconAnchor: [14, 14],
        html: `<span>${index + 1}</span>` }),
    }).addTo(customDraftGroup)
    marker.on('drag', () => {
      const positions = draftMarkers.map((item) => item.getLatLng())
      draftLine?.setLatLngs(positions)
      draftOutline?.setLatLngs(positions)
    })
    marker.on('dragend', () => {
      if (props.customDrawing?.saving) return
      const position = marker.getLatLng().wrap()
      emit('drawing-point-move', { index, lon: position.lng, lat: position.lat })
    })
    draftMarkers.push(marker)
  })
}

const focusArea = (target) => {
  if (!map || !target) return
  if (Array.isArray(target.bounds)) {
    const viewport = map.getSize()
    const leftPadding = Math.min(props.layoutInsets.left + 20, viewport.x / 2 - 20)
    const rightPadding = Math.min(props.layoutInsets.right + 20, viewport.x / 2 - 20)
    map.flyToBounds(target.bounds, {
      paddingTopLeft: [leftPadding, Math.min(130, viewport.y / 3)],
      paddingBottomRight: [rightPadding, Math.min(props.dockHeight + 48, viewport.y / 3)],
      maxZoom: target.maxZoom || 11,
      duration: 0.8,
    })
    return
  }
  if (Array.isArray(target.center)) {
    map.flyTo(target.center, target.zoom || map.getZoom(), {
      duration: 0.8,
    })
  }
}

defineExpose({ clearRoute, clearDrawnRoute, focusArea, closePopup: () => map?.closePopup() })
watch([() => props.frameKey, () => props.layers.velocity, () => props.layers.heatmap,
  () => props.wind, () => props.heatmap], () => frameGate.reset(props.frameKey,
  [...(props.layers.velocity && props.wind?.velocity ? ['velocity'] : []),
    ...(props.layers.heatmap && props.heatmap?.wind_speed ? ['heatmap'] : [])]), { immediate: true })
watch(() => props.customDrawing?.active, synchronizeDrawingMode)
watch(searchLines, renderSearchCorridors)
watch(() => [props.recommendations, props.detourEndpoints, props.layers.route, props.customDrawing?.active], renderRecommendations, { deep: true })
watch([() => props.customDrawing?.active, () => props.customDrawing?.points, () => props.customDrawing?.saving], renderCustomRoute)
// Frames are replaced atomically; traversing every U/V value on every update
// adds work without detecting any legitimate in-place weather edits.
watch([() => props.wind, () => props.layers.velocity, () => props.thresholds.safe,
  () => props.thresholds.notice, () => props.thresholds.warning, () => props.thresholds.danger], renderWind)
watch(
  [() => props.heatmap, () => props.wind, () => props.layers.heatmap, () => props.layers.mutation,
    () => props.thresholds.safe, () => props.thresholds.notice, () => props.thresholds.warning,
    () => props.thresholds.danger, () => props.thresholds.maxHorizontalWindShear],
  renderHeatmap,
)
watch(() => [props.analysis, props.flightMode, props.flightProfile, props.layers.route, props.planner?.startText, props.planner?.endText, props.planner?.points, props.customDrawing?.active, props.planningPoints], renderRoute, { deep: true })
watch(() => [props.aircraftPosition, props.flightMode, props.layers.route, props.customDrawing?.active], renderFlightProgress, { deep: true })
watch(() => [props.selectedSampleIndex, props.analysis?.samples, props.layers.route, props.customDrawing?.active], renderRouteSelection, { deep: true, flush: 'post' })

onMounted(() => {
  map = L.map('wind-map', { zoomControl: false, attributionControl: true }).fitBounds(CHINA_INITIAL_BOUNDS, { padding: [12, 12] })
  map.createPane('windHeatPane')
  map.getPane('windHeatPane').style.zIndex = 350
  map.createPane('searchCorridorPane')
  map.getPane('searchCorridorPane').style.zIndex = 390
  map.getPane('searchCorridorPane').style.pointerEvents = 'none'
  map.createPane('routeSelectionPane')
  map.createPane('recommendationPane')
  map.getPane('recommendationPane').style.zIndex = 420
  map.getPane('recommendationPane').style.pointerEvents = 'none'
  map.getPane('routeSelectionPane').style.zIndex = 620
  map.getPane('routeSelectionPane').style.pointerEvents = 'none'
  map.createPane('flightProgressPane')
  map.getPane('flightProgressPane').style.zIndex = 610
  map.getPane('flightProgressPane').style.pointerEvents = 'none'
  map.createPane('customRoutePane')
  map.getPane('customRoutePane').style.zIndex = 625
  L.tileLayer('/api/map-tiles/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map)
  routeGroup = L.featureGroup().addTo(map)
  searchGroup = L.layerGroup().addTo(map)
  recommendationGroup = L.layerGroup().addTo(map)
  routeSelectionGroup = L.layerGroup().addTo(map)
  flightProgressGroup = L.layerGroup().addTo(map)
  drawnGroup = L.featureGroup().addTo(map)
  customDraftGroup = L.layerGroup().addTo(map)
  map.on('click', ({ latlng }) => {
    if (props.customDrawing?.active) {
      if (!props.customDrawing.saving) {
        const position = latlng.wrap()
        emit('drawing-point-add', { lon: position.lng, lat: position.lat })
      }
      return
    }
    emit('point-click', { lon: latlng.lng, lat: latlng.lat, map })
  })
  map.on('zoomend', () => emit('zoom-changed', map.getZoom()))
  renderWind()
  renderHeatmap()
  renderRoute()
  renderSearchCorridors()
  renderRecommendations()
  renderRouteSelection()
  renderFlightProgress()
  synchronizeDrawingMode()
  renderCustomRoute()
})

onBeforeUnmount(() => {
  map?.remove()
})
</script>

<template>
  <div class="map-viewport" style="position: relative; width: 100%; height: 100%;">
    <main id="wind-map" class="wind-map"></main>
    <div v-if="searchLines.length" class="search-corridor-status" role="status" :style="{ left: `${layoutInsets.left + 12}px` }">
      <span style="color: #00f3ff">候选航线 · 虚线</span>
    </div>
    <div class="leaflet-bottom leaflet-left wind-legend-position" style="pointer-events: none; z-index: 640; position: absolute; left: 0; bottom: 0;">
      <div v-if="layers.heatmap || layers.velocity" class="leaflet-control wind-heat-legend" style="pointer-events: auto;">
        <strong>风速 m/s (点击分级)</strong>
        <div class="legend-blocks">
          <ElTooltip v-for="(block, i) in heatBlocks" :key="i" placement="top" effect="dark"
            popper-class="wind-feel-tooltip" :show-after="180" :hide-after="100" :trigger-keys="[]">
            <template #content>
              <div class="wind-feel-content">
                <div class="wind-feel-heading">{{ block.label }} m/s · 地面参考感受</div>
                <p v-for="feel in block.feel" :key="feel.name" class="wind-feel-item">
                  <b>{{ feel.name }}</b><span>{{ feel.description }}</span>
                </p>
              </div>
            </template>
            <div class="legend-block" role="button" tabindex="0"
                 :aria-pressed="activeBlocks.length === 0 || activeBlocks.includes(i)"
                 :style="{ background: block.color, opacity: activeBlocks.length === 0 || activeBlocks.includes(i) ? 1 : 0.3 }"
                 @click="toggleBlock(i)" @keydown.enter.prevent="toggleBlock(i)" @keydown.space.prevent="toggleBlock(i)">
              {{ ['一级风', '二级风', '三级风', '四级风', '大于四级'][i] }} {{ block.label }}
            </div>
          </ElTooltip>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-corridor-status { position: absolute; top: 58px; z-index: 700; display: flex; gap: 14px; padding: 6px 10px; background: #111e30e8; border-radius: 5px; font-size: 12px; pointer-events: none; }
:global(.el-popper.wind-feel-tooltip) { max-width: min(320px, calc(100vw - 24px)); padding: 12px 14px; color: #fff; background: #050505; border: 1px solid #555; border-radius: 6px; box-shadow: 0 4px 16px #0008; }
:global(.el-popper.wind-feel-tooltip .el-popper__arrow::before) { background: #050505; border-color: #555; }
.wind-feel-content { color: #fff; font: 12px/1.6 sans-serif; text-align: left; }
.wind-feel-heading { margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid #ffffff40; font-weight: 700; }
.wind-feel-item { margin: 0 0 7px; }
.wind-feel-item:last-child { margin-bottom: 0; }
.wind-feel-item b, .wind-feel-item span { display: block; color: #fff; }
:deep(.custom-route-waypoint) { display: flex; align-items: center; justify-content: center; border: 2px solid #00f3ff; border-radius: 50%; color: #fff; background: #07121cf2; font-size: 12px; font-weight: 700; box-shadow: 0 2px 8px #0009; cursor: grab; }
:deep(.custom-route-waypoint.leaflet-drag-target) { cursor: grabbing; }
</style>
