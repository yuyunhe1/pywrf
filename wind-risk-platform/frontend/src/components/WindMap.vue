<script setup>
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import 'leaflet-draw'
import 'leaflet-draw/dist/leaflet.draw.css'
import 'leaflet-velocity'
import 'leaflet-velocity/dist/leaflet-velocity.css'
import { onBeforeUnmount, onMounted, watch, ref, computed } from 'vue'
import { createWindSpeedCanvasLayer } from '../layers/WindSpeedCanvasLayer'

const props = defineProps({
  wind: Object,
  heatmap: Object,
  layers: { type: Object, required: true },
  thresholds: { type: Object, required: true },
  analysis: Object,
  planner: Object,
})
const emit = defineEmits(['point-click', 'route-created', 'zoom-changed'])

let map
let heatLayer
let velocityLayers = []
let routeGroup
let drawnGroup
let heatLegend

// WRF/WPS domain center near Wuhu Aerospace Industrial Park, about 10 km x 10 km.
const CHINA_INITIAL_BOUNDS = [[18.0, 73.0], [54.0, 135.0]]

const riskColor = (risk) => ({
  一级风: '#28c76f',
  二级风: '#f4c95d',
  三级风: '#ff9f43',
  四级风: '#ea5455',
  大于四级: '#9b51e0',
}[risk] || '#eaf5ff')

const renderWind = () => {
  if (!map) return
  velocityLayers.forEach((layer) => map.removeLayer(layer))
  velocityLayers = []
  if (props.layers.velocity && props.wind?.velocity && L.velocityLayer) {
    const [uSource, vSource] = props.wind.velocity
    const bands = [
      { max: props.thresholds.safe, color: '#28c76f' },
      { max: props.thresholds.notice, color: '#f4c95d' },
      { max: props.thresholds.warning, color: '#ff9f43' },
      { max: props.thresholds.danger, color: '#ea5455' },
      { max: Infinity, color: '#9b51e0' },
    ]
    let minimum = -Infinity
    bands.forEach((band, bandIndex) => {
      // 如果 activeBlocks 不为空且当前档位不在选中列表中，则跳过渲染
      if (activeBlocks.value.length > 0 && !activeBlocks.value.includes(bandIndex)) {
        minimum = band.max
        return
      }
      
      const u = uSource.data.map((value, index) => {
        const speed = Math.hypot(value, vSource.data[index])
        return speed > minimum && speed <= band.max ? value : null
      })
      const v = vSource.data.map((value, index) => (u[index] === null ? null : value))
      const layer = L.velocityLayer({
        data: [{ ...uSource, data: u }, { ...vSource, data: v }],
        displayValues: false,
        velocityScale: 0.007,
        maxVelocity: Math.max(band.max, props.thresholds.danger + 5),
        lineWidth: 2.6,
        particleMultiplier: 1 / 650,
        colorScale: [band.color, band.color],
      }).addTo(map)
      velocityLayers.push(layer)
      minimum = band.max
    })
  }
}

const heatBlocks = computed(() => [
  { min: 0, max: props.thresholds.safe, color: '#28c76f', label: `0-${props.thresholds.safe}` },
  { min: props.thresholds.safe, max: props.thresholds.notice, color: '#f4c95d', label: `${props.thresholds.safe}-${props.thresholds.notice}` },
  { min: props.thresholds.notice, max: props.thresholds.warning, color: '#ff9f43', label: `${props.thresholds.notice}-${props.thresholds.warning}` },
  { min: props.thresholds.warning, max: props.thresholds.danger, color: '#ea5455', label: `${props.thresholds.warning}-${props.thresholds.danger}` },
  { min: props.thresholds.danger, max: Infinity, color: '#9b51e0', label: `${props.thresholds.danger}+` }
])
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
  if (heatLayer) map.removeLayer(heatLayer)
  heatLayer = null
  if (props.layers.heatmap && props.heatmap?.wind_speed) {
    let filterRanges = null
    if (activeBlocks.value.length > 0) {
      filterRanges = activeBlocks.value.map(i => [heatBlocks.value[i].min, heatBlocks.value[i].max])
    }
    heatLayer = createWindSpeedCanvasLayer(props.heatmap.wind_speed, { filterRanges }).addTo(map)
  }
}

const renderRoute = () => {
  if (!map) return
  routeGroup.clearLayers()
  
  if (props.analysis?.samples) {
    const samples = props.analysis.samples
    if (samples.length >= 2) {
      const start = samples[0]
      const end = samples[samples.length - 1]
      
      // 添加起点标记
      L.circleMarker([start.lat, start.lon], {
        radius: 6,
        fillColor: '#44d7b6',
        color: '#fff',
        weight: 2,
        opacity: 1,
        fillOpacity: 1
      }).bindTooltip('起点', { permanent: true, direction: 'right' }).addTo(routeGroup)
      
      // 添加终点标记
      L.circleMarker([end.lat, end.lon], {
        radius: 6,
        fillColor: '#ea5455',
        color: '#fff',
        weight: 2,
        opacity: 1,
        fillOpacity: 1
      }).bindTooltip('终点', { permanent: true, direction: 'right' }).addTo(routeGroup)
    }
  } else if (props.planner?.startText || props.planner?.endText) {
    // 即使没有规划航线，也显示已经选好的起点和终点
    const parsePointSafely = (text) => {
      if (!text) return null
      const parts = text.split(',')
      if (parts.length === 2) {
        const lon = Number(parts[0])
        const lat = Number(parts[1])
        if (Number.isFinite(lon) && Number.isFinite(lat)) {
          return { lon, lat }
        }
      }
      return null
    }
    
    const startPoint = parsePointSafely(props.planner?.startText)
    const endPoint = parsePointSafely(props.planner?.endText)
    
    if (startPoint) {
      L.circleMarker([startPoint.lat, startPoint.lon], {
        radius: 6,
        fillColor: '#44d7b6',
        color: '#fff',
        weight: 2,
        opacity: 1,
        fillOpacity: 1
      }).bindTooltip('起点', { permanent: true, direction: 'right' }).addTo(routeGroup)
    }
    
    if (endPoint) {
      L.circleMarker([endPoint.lat, endPoint.lon], {
        radius: 6,
        fillColor: '#ea5455',
        color: '#fff',
        weight: 2,
        opacity: 1,
        fillOpacity: 1
      }).bindTooltip('终点', { permanent: true, direction: 'right' }).addTo(routeGroup)
    }
  }
  
  if (!props.layers.route || !props.analysis?.samples) return
  
  const samples = props.analysis.samples
  if (samples.length < 2) return
  const routeLatLngs = samples.map((item) => [item.lat, item.lon])
  
  const start = samples[0]
  const end = samples[samples.length - 1]
  // 1. 直线参考线（深灰色虚线，降低存在感）
  L.polyline([[start.lat, start.lon], [end.lat, end.lon]], {
    color: '#64748b',
    weight: 1.5,
    dashArray: '4, 8',
    opacity: 0.6,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(routeGroup)

  // 2. 规划航线 - 底层（极简黑色实线边缘，增强对比度）
  L.polyline(routeLatLngs, {
    color: '#0f172a',
    weight: 5,
    opacity: 0.9,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(routeGroup)

  // 3. 规划航线 - 顶层（高亮青色轨迹芯，赛博朋克经典配色）
  L.polyline(routeLatLngs, {
    color: '#00f3ff',
    weight: 2,
    opacity: 1,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(routeGroup)
}

const clearRoute = () => {
  drawnGroup?.clearLayers()
  routeGroup?.clearLayers()
}

const focusArea = (target) => {
  if (!map || !target) return
  if (Array.isArray(target.bounds)) {
    map.flyToBounds(target.bounds, {
      padding: [30, 30],
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

defineExpose({ clearRoute, focusArea })
watch(() => [props.wind, props.layers.velocity, props.thresholds], renderWind, { deep: true })
watch(() => [props.heatmap, props.layers.heatmap], renderHeatmap, { deep: true })
watch(() => [props.analysis, props.layers.route, props.planner?.startText, props.planner?.endText], renderRoute, { deep: true })

onMounted(() => {
  map = L.map('wind-map', { zoomControl: true, attributionControl: false }).fitBounds(CHINA_INITIAL_BOUNDS, { padding: [12, 12] })
  map.createPane('windHeatPane')
  map.getPane('windHeatPane').style.zIndex = 350
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
  }).addTo(map)
  routeGroup = L.featureGroup().addTo(map)
  drawnGroup = L.featureGroup().addTo(map)
  const drawControl = new L.Control.Draw({
    edit: { featureGroup: drawnGroup },
    draw: { polygon: false, rectangle: false, circle: false, circlemarker: false, marker: false, polyline: { shapeOptions: { color: '#eaf5ff', weight: 4 } } },
  })
  map.addControl(drawControl)
  map.on(L.Draw.Event.CREATED, (event) => {
    clearRoute()
    drawnGroup.addLayer(event.layer)
    const points = event.layer.getLatLngs().map(({ lng, lat }) => [lng, lat])
    emit('route-created', points)
  })
  map.on('click', ({ latlng }) => emit('point-click', { lon: latlng.lng, lat: latlng.lat, map }))
  map.on('zoomend', () => emit('zoom-changed', map.getZoom()))
  renderWind()
  renderHeatmap()
})

onBeforeUnmount(() => {
  map?.remove()
})
</script>

<template>
  <div style="position: relative; width: 100%; height: 100%;">
    <main id="wind-map" class="wind-map"></main>
    <div class="leaflet-bottom leaflet-left" style="pointer-events: none; z-index: 1000; position: absolute; bottom: 0; left: 0;">
      <!-- 热力图多选分级图例 -->
      <div v-if="layers.heatmap" class="leaflet-control wind-heat-legend" style="pointer-events: auto;">
        <strong>风速 m/s (点击分级)</strong>
        <div class="legend-blocks">
          <div v-for="(block, i) in heatBlocks" :key="i" 
               class="legend-block"
               :style="{ background: block.color, opacity: activeBlocks.length === 0 || activeBlocks.includes(i) ? 1 : 0.3 }"
               @click="toggleBlock(i)">
            {{ ['一级风', '二级风', '三级风', '四级风', '大于四级', '大于四级'][i] }} {{ block.label }}
          </div>
        </div>
      </div>
      
      <!-- 粒子流可点击图例 -->
      <div v-else-if="layers.velocity" class="leaflet-control wind-heat-legend" style="pointer-events: auto;">
        <strong>风速 m/s (点击分级)</strong>
        <div class="legend-blocks">
          <div v-for="(block, i) in heatBlocks.slice(0, 5)" :key="i" 
               class="legend-block"
               :style="{ background: block.color, opacity: activeBlocks.length === 0 || activeBlocks.includes(i) ? 1 : 0.3 }"
               @click="toggleBlock(i)">
            {{ ['一级风', '二级风', '三级风', '四级风', '大于四级'][i] }} {{ block.label }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
