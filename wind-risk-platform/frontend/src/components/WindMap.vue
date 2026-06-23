<script setup>
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import 'leaflet-draw'
import 'leaflet-draw/dist/leaflet.draw.css'
import 'leaflet-velocity'
import 'leaflet-velocity/dist/leaflet-velocity.css'
import { onBeforeUnmount, onMounted, watch } from 'vue'
import { createWindSpeedCanvasLayer } from '../layers/WindSpeedCanvasLayer'

const props = defineProps({
  wind: Object,
  heatmap: Object,
  layers: { type: Object, required: true },
  analysis: Object,
})
const emit = defineEmits(['point-click', 'route-created'])

let map
let heatLayer
let velocityLayer
let routeGroup
let drawnGroup
let heatLegend

// WRF/WPS domain center near Wuhu Aerospace Industrial Park, about 10 km x 10 km.
const WUHU_WRF_INITIAL_BOUNDS = [[31.055, 118.547], [31.145, 118.653]]

const riskColor = (risk) => ({
  安全: '#28c76f',
  注意: '#f4c95d',
  中等风险: '#ff9f43',
  高风险: '#ea5455',
  危险: '#9b51e0',
}[risk] || '#eaf5ff')

const renderWind = () => {
  if (!map) return
  if (velocityLayer) map.removeLayer(velocityLayer)
  velocityLayer = null
  if (props.layers.velocity && props.wind?.velocity && L.velocityLayer) {
    velocityLayer = L.velocityLayer({
      data: props.wind.velocity,
      displayValues: false,
      velocityScale: 0.007,
      maxVelocity: 15,
      lineWidth: 2,
      colorScale: ['#b8f3ff', '#69d7ff', '#44d7b6', '#f4c95d'],
    }).addTo(map)
  }
}

const renderHeatmap = () => {
  if (!map) return
  if (heatLayer) map.removeLayer(heatLayer)
  heatLayer = null
  if (props.layers.heatmap && props.heatmap?.wind_speed) {
    heatLayer = createWindSpeedCanvasLayer(props.heatmap.wind_speed).addTo(map)
  }
}

const renderRoute = () => {
  if (!map) return
  routeGroup.clearLayers()
  if (!props.layers.route || !props.analysis?.samples) return
  const samples = props.analysis.samples
  samples.slice(0, -1).forEach((sample, index) => {
    const next = samples[index + 1]
    L.polyline([[sample.lat, sample.lon], [next.lat, next.lon]], {
      color: riskColor(Math.abs(next.wind_speed) > Math.abs(sample.wind_speed) ? next.risk : sample.risk),
      weight: 6,
      opacity: 0.95,
    }).addTo(routeGroup)
  })
}

const clearRoute = () => {
  drawnGroup?.clearLayers()
  routeGroup?.clearLayers()
}

defineExpose({ clearRoute })
watch(() => [props.wind, props.layers.velocity], renderWind, { deep: true })
watch(() => [props.heatmap, props.layers.heatmap], renderHeatmap, { deep: true })
watch(() => [props.analysis, props.layers.route], renderRoute, { deep: true })

onMounted(() => {
  map = L.map('wind-map', { zoomControl: true }).fitBounds(WUHU_WRF_INITIAL_BOUNDS, { padding: [12, 12] })
  map.createPane('windHeatPane')
  map.getPane('windHeatPane').style.zIndex = 350
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
  }).addTo(map)
  heatLegend = L.control({ position: 'bottomleft' })
  heatLegend.onAdd = () => {
    const element = L.DomUtil.create('div', 'wind-heat-legend')
    element.innerHTML = `
      <strong>风速 m/s</strong>
      <div class="wind-heat-gradient"></div>
      <div class="wind-heat-ticks"><span>0</span><span>3</span><span>6</span><span>8</span><span>10</span><span>15+</span></div>
    `
    return element
  }
  heatLegend.addTo(map)
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
  renderWind()
  renderHeatmap()
})

onBeforeUnmount(() => {
  heatLegend?.remove()
  map?.remove()
})
</script>

<template><main id="wind-map" class="wind-map"></main></template>
