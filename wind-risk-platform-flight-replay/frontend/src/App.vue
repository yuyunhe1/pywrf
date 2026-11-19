<script setup>
import WorkspaceIcon from './components/WorkspaceIcon.vue'
import L from 'leaflet'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { analyzeRoute, deleteRoute, getDepartureWindows, getExportedRouteUrl, getExportedWaypointUrl, getGfsDownloadStatus, getWindField, getPoint, getTimes, getV3Status, listExportedRoutes, listRoutes, planRoute, saveRoute, recommendRoute, getRecommendationJob } from './api'
import AnalysisPanel from './components/AnalysisPanel.vue'
import WorkspacePanel from './components/WorkspacePanel.vue'
import { useWorkspaceLayout } from './utils/useWorkspaceLayout'
import { routeSnapshotKey, exportForRoute, planningStageLabel } from './utils/routeRecords'
import ControlPanel from './components/ControlPanel.vue'
import WindMap from './components/WindMap.vue'
import FlightReplayControls from './components/FlightReplayControls.vue'
import CustomRouteDrawingControls from './components/CustomRouteDrawingControls.vue'
import { getFlightProfile, getReplayWind, getReplayWindFrames, getV3Snapshot } from './api'
import { createFlightReplayController, flightPosition, fromBeijingInput, toBeijingInput, interpolateWindFrames, PARTICLE_MAP_UPDATE_INTERVAL_SECONDS, NATIVE_WRF_TIME_MODEL } from './utils/flightReplay'
import { chooseWrfTime, wrfPollDelay } from './utils/wrfUpdates.js'
import { chooseGfsTime, gfsPollDelay } from './utils/gfsUpdates.js'
import { createDepartureWindowEvaluator } from './utils/departureWindows.js'
import { createCustomRouteDrawingController } from './utils/customRouteDrawing.js'
import { workflowLabel } from './utils/recommendations.js'
import { createPlanningDemoController, createPlanningSearchPreview, createAnimatedPlanningFlight, isPlanningDemoCandidate } from './utils/planningDemo.js'
import { qualityRetryLabel } from './utils/routeQuality.js'
import { departureRangeBounds, recommendedRangeRequest, completeRecommendationDrawing } from './utils/departureRange.js'
import { createRangeRecommendationSelection } from './utils/rangeRecommendationSelection.js'

const workspace = useWorkspaceLayout()
const { panels, compact, insets, resizing } = workspace
const dockEl = ref()
const dockHeight = ref(0)
let dockObserver
const layoutStyle = computed(() => ({ '--map-left': `${insets.value.left}px`, '--map-right': `${insets.value.right}px`, '--dock-height': `${dockHeight.value}px` }))
const options = reactive({ cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
const DEFAULT_LOOKAHEAD_HOURS = 3
const selection = reactive({ source: 'gfs', cycle: '', forecastHour: DEFAULT_LOOKAHEAD_HOURS, validTime: '', level: '100m AGL' })
const layers = reactive({ heatmap: false, velocity: true, route: true, mutation: true })
const thresholds = reactive({ safe: 1.5, notice: 3.3, warning: 5.4, danger: 7.9, maxHorizontalWindShear: 5.4 })
const flightSettings = reactive({ averageGroundSpeedMps: 50 })
const temporalPlanningEnabled = ref(false)
const fixedRouteSelection = ref(null)
const departureWindows = ref({ active: false })
const recommendation = shallowRef(null)
const recommendationBaseline = shallowRef(null)
const recommendationRange = reactive({ enabled:false, departureStartAt:'', departureEndAt:'', requiredWaypoints:[], name:'' })
const planningEntry = ref('specified')
const recommendationPreview = shallowRef(null)
const recommendationPreviewProfile = shallowRef(null)
const recommendationPreviewMap = shallowRef(null)
const rangeSelection = shallowRef({selectedId:null,pendingId:null,manual:false})
let rangeLatestResult, rangeDefaultDisplay, interruptRangeDisplay
const rangeSwitcher = createRangeRecommendationSelection({
  onState:value=>{rangeSelection.value=value},
  requestProfile:(card,signal)=>{
    const plan=card.plan,latest=rangeLatestResult
    const at=card.recommended_departure_at || card.first_departure_at
    if(latest?.flight_profile && latest.dataset_id===plan.dataset_id && latest.level===plan.level
      && latest.average_ground_speed_mps===plan.average_ground_speed_mps && Date.parse(latest.departure_at)===Date.parse(at)
      && JSON.stringify(latest.points)===JSON.stringify(plan.points)) return Promise.resolve(latest.flight_profile)
    return getFlightProfile({...plan.route_context,points:plan.points,source:plan.source,level:plan.level,
      dataset_id:plan.dataset_id,ephemeral:true},{...departureConfiguration(),dataset_id:plan.dataset_id,
        departure_at:at,average_ground_speed_mps:plan.average_ground_speed_mps},signal)
  },
  onPause:()=>{interruptRangeDisplay?.();demo.dismiss();replay.pause();closeRecommendationPreview()},
  onSelect:result=>applyRangeSelection(result),
  onDefault:workflow=>rangeDefaultDisplay?.(workflow) || false,
  onError:message=>showTemporaryError(message),
})
const detourSegment = ref(0)
const maxDetourPercent = ref(50)
const recommending = ref(false)
const adoptingRecommendation = ref(false)
let recommendationController, recommendationGeneration = 0, recommendationJobId, previewGeneration = 0
let baselineRefreshTimer
const detourEndpoints = computed(() => recommendationBaseline.value?.points?.slice(detourSegment.value,detourSegment.value+2))
const newBaselineVersion = () => crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`
let fixedRouteGeneration = 0
let switchingFixedRoute = 0
const departureEvaluator = createDepartureWindowEvaluator({
  request: getDepartureWindows,
  onChange: (state) => {
    departureWindows.value = state
    const current = fixedRouteSelection.value
    if (state.result && current?.ephemeral && current.planning_key === state.route?.planning_key
      && (current.route_id !== state.result.route_id || current.dataset_id !== state.result.dataset_id)) {
      fixedRouteSelection.value = { ...current, route_id: state.result.route_id, dataset_id: state.result.dataset_id }
    }
  },
})
const flightReplay = shallowRef({ active: false })
const replay = createFlightReplayController({ requestProfile: getFlightProfile, requestFrame: getReplayWind, requestFrames: getReplayWindFrames,
  requireMapReady: true,
  onChange: (state) => { flightReplay.value = state },
  mapUpdateIntervalSeconds: layers.velocity ? PARTICLE_MAP_UPDATE_INTERVAL_SECONDS : 0,
})
const planningDemo = shallowRef({ active: false })
let onDemoVisible
let onDemoComplete
let retainPlanningResult
const replayDockVisible = computed(() => planningDemo.value.active
  ? planningPublished.value && (planningDemo.value.completed || Boolean(planningDemo.value.error)) : flightReplay.value.active)
const demo = createPlanningDemoController({ requestFrame: getReplayWind, requestFrames: getReplayWindFrames,
  onChange: state => {
    const previous = planningDemo.value
    if (previous.active && !state.active && previous.mapWind) paintedSnapshot.value = {
      wind: previous.mapWind, heatmap: previous.mapHeatmap, metadata: previous.mapWind.metadata,
    }
    planningDemo.value = state
    if (state.active && state.points.length >= 2 && state.mapWind) onDemoVisible?.()
  },
  onComplete: () => onDemoComplete?.(),
  onError: reason => { showTemporaryError(apiErrorMessage(reason)); onDemoComplete?.() },
})
const flightMode = computed(() => planningDemo.value.active || Boolean(fixedRouteSelection.value && flightReplay.value.mode !== 'snapshot'))
const displayAnalysis = computed(() => recommendationPreviewProfile.value?.analysis || (planningDemo.value.active
  && !planningPublished.value ? undefined : planningDemo.value.active
  ? planningDemo.value.profile?.analysis : flightMode.value ? flightReplay.value.result?.analysis : analysis.value))
const paintedSnapshot = shallowRef(null)
const displayWind = computed(() => recommendationPreviewMap.value?.wind || (planningDemo.value.active
  ? planningDemo.value.mapWind || paintedSnapshot.value?.wind : flightMode.value
  ? flightReplay.value.mapWind || (!flightReplay.value.error && !flightReplay.value.mapError ? paintedSnapshot.value?.wind : null)
  : wind.value ? paintedSnapshot.value?.wind : null))
const displayHeatmap = computed(() => recommendationPreviewMap.value?.heatmap || (planningDemo.value.active
  ? planningDemo.value.mapHeatmap || paintedSnapshot.value?.heatmap : flightMode.value
  ? flightReplay.value.mapHeatmap || (!flightReplay.value.error && !flightReplay.value.mapError ? paintedSnapshot.value?.heatmap : null)
  : heatmap.value ? paintedSnapshot.value?.heatmap : null))
const renderWind = computed(() => recommendationPreviewMap.value?.wind || (planningDemo.value.active
  ? planningDemo.value.pendingMapWind || displayWind.value : flightMode.value
  ? flightReplay.value.pendingMapWind || displayWind.value : wind.value))
const renderHeatmap = computed(() => recommendationPreviewMap.value?.heatmap || (planningDemo.value.active
  ? planningDemo.value.pendingMapHeatmap || displayHeatmap.value : flightMode.value
  ? flightReplay.value.pendingMapHeatmap || displayHeatmap.value : heatmap.value))
const snapshotRenderKey = ref('')
const renderedWindKey = ref('')
const mapFrameKey = computed(() => recommendationPreviewMap.value
  ? `preview:${previewGeneration}:${recommendationPreviewMap.value.wind?.metadata.valid_time}`
  : planningDemo.value.active ? planningDemo.value.pendingMapKey || planningDemo.value.mapKey || snapshotRenderKey.value
    : flightMode.value ? flightReplay.value.pendingMapKey || flightReplay.value.mapKey || snapshotRenderKey.value : snapshotRenderKey.value)
const windLoading = computed(() => planningDemo.value.loading || loading.value || !flightMode.value && Boolean(snapshotRenderKey.value)
  && snapshotRenderKey.value !== renderedWindKey.value && (layers.velocity || layers.heatmap))
const windFrameReady = (key) => {
  if (key !== mapFrameKey.value) return
  if (planningDemo.value.active) { demo.acknowledgeMap(key); return }
  renderedWindKey.value = key
  if (key === snapshotRenderKey.value && !flightMode.value && wind.value) {
    paintedSnapshot.value = { wind: wind.value, heatmap: heatmap.value, metadata: metadata.value }
  }
  replay.acknowledgeMap(key)
  if (planningPublished.value && flightReplay.value.result) onDemoVisible?.()
}
const displayMetadata = computed(() => {
  if (recommendationPreviewMap.value?.wind?.metadata) return recommendationPreviewMap.value.wind.metadata
  if (planningDemo.value.active) return planningDemo.value.mapWind?.metadata || paintedSnapshot.value?.metadata
  if (!flightMode.value) return wind.value ? paintedSnapshot.value?.metadata : undefined
  const state = flightReplay.value
  if (state.mapWind?.metadata) return state.mapWind.metadata
  if (!state.result || !state.mapTime) return state.error || state.mapError ? undefined : paintedSnapshot.value?.metadata
  return { dataset_id: state.result.dataset_id, cycle: state.result.forecast_cycle, level: state.result.level,
    valid_time: state.mapTime, source: `${(state.result.source || 'wrf').toUpperCase()} · 数据不足`,
    forecast_hour: (Date.parse(state.mapTime) - Date.parse(state.result.forecast_cycle)) / 3600000 }
})
const aircraftPosition = computed(() => !planningDemo.value.active && !recommendationPreview.value && flightMode.value ? flightPosition(flightReplay.value.result, flightReplay.value.seconds) : null)
const analysisReplay = computed(() => recommendationPreviewProfile.value
  ? { ...flightReplay.value, active: true, loading: false, result: recommendationPreviewProfile.value, departureAt: recommendationPreviewProfile.value.departure_at }
  : planningDemo.value.active && !planningPublished.value ? { active: false }
  : planningDemo.value.active ? { ...flightReplay.value, active: true, mode: 'flight', loading: false,
    playing: false, result: planningDemo.value.profile, departureAt: planningDemo.value.profile?.departure_at,
    seconds: planningDemo.value.seconds, mapTime: planningDemo.value.mapTime, mapLoading: planningDemo.value.loading,
    error: planningDemo.value.error, mapError: '', mapUpdateIntervalSeconds: PARTICLE_MAP_UPDATE_INTERVAL_SECONDS }
    : flightReplay.value)
const wind = shallowRef()
const heatmap = shallowRef()
const analysis = ref()
const selectedRouteSampleIndex = ref(null)
const metadata = ref()
const loading = ref(false)
const error = ref('')
const mapRef = ref()
const controlPanelRef = ref()
let routePoints = []
let suppressPlannerAutoClear = 0
const activeRouteId = ref('')
const savingRoute = ref(false)
const saveError = ref('')
const savedReceipt = shallowRef(null)
let routeSaveDisposed = false
const planningTask = shallowRef(null)
const searchProgress = shallowRef(null)
const planningPublished = ref(false)
const planningStatus = computed(() => ({ ...planningTask.value, label: qualityRetryLabel(planningTask.value) || workflowLabel(planningTask.value?.workflow_stage) || planningStageLabel(planningTask.value, planningPublished.value), elapsed: planningTask.value?.elapsed_seconds }))
const planner = reactive({
  name: '默认航线 A',
  algorithm: 'wa_lpa_star',
  altitudeMslM: 300,
  startHeadingDeg: '',
  endHeadingDeg: '',
  constrainHeadings: false,
  aircraftModel: 'fixed_wing',
  strategy: 'wind_avoidance',
  startText: '',
  endText: '',
  points: [],
  missionItems: [],
  planning: false,
  planningMessage: '',
  departureMode: 'now',
  departureAt: '',
})
const savedRoutes = ref([])
const picking = ref('')
const savedRouteNotice = ref('')
let savedRouteNoticeTimer
const customDrawing = shallowRef({ active: false, name: '', points: [], saving: false, error: '' })
const drawingPurpose = ref('fixed')
let rangeDrawingSnapshot
const drawing = createCustomRouteDrawingController({ save: saveRoute,
  onChange: (state) => { customDrawing.value = state },
  onSaved: async (saved) => {
    savedRouteNotice.value = '固定航线已保存，正在评估通航窗口。'
    clearTimeout(savedRouteNoticeTimer)
    savedRouteNoticeTimer = setTimeout(() => { savedRouteNotice.value = '' }, 4000)
    savedRoutes.value = [saved, ...savedRoutes.value.filter((route) => route.route_id !== saved.route_id)]
    void refreshRoutes()
    void controlPanelRef.value?.refreshJsonDialog?.()
    await loadFixedRoute(saved)
  },
  onSavedError: (_saved, reason) => { showTemporaryError(`航线已保存，评估暂未完成：${apiErrorMessage(reason)}`) },
  onComplete: async payload => {
    if (drawingPurpose.value !== 'departure_range') return activateRecommendationBaseline(payload)
    await runWithPlannerAutoClearSuppressed(async () => {
      recommendationRange.requiredWaypoints = payload.points.map(p => p.slice(0, 2))
      recommendationRange.name = payload.name
      planner.startText = payload.points[0].join(', ')
      planner.endText = payload.points.at(-1).join(', ')
      planner.name = payload.name
    })
    rangeDrawingSnapshot = undefined
    planner.planningMessage = '绘制完成，准备推荐起飞时刻与航线'
  },
})
const ZOOM_AVERAGE_LAYER = 13
const AVERAGE_LAYER = '250-350m AGL average'
let pointRequestController
let downloadPollTimer
let wrfPollTimer
let gfsPollTimer
let timesGeneration = 0
let routePlanController
let routePlanGeneration = 0
let autoPlanTimer
let snapshotGeneration = 0
let windRequestController

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
  const timer = downloadPollTimer
  try {
    const status = await getGfsDownloadStatus()
    if (selection.source !== 'gfs' || timer !== downloadPollTimer) return
    if (status.running) return
    stopDownloadPolling()
    if (status.return_code === 0) {
      await applyTimes({ background: true })
    } else if (status.return_code !== null && status.return_code !== undefined) {
      showTemporaryError(`GFS 下载失败：${status.message || `exit ${status.return_code}`}`)
    }
  } catch (reason) {
    if (selection.source !== 'gfs' || timer !== downloadPollTimer) return
    stopDownloadPolling()
    showTemporaryError(apiErrorMessage(reason))
  }
}

const startDownloadPolling = () => {
  if (selection.source !== 'gfs' || downloadPollTimer) return
  downloadPollTimer = setInterval(checkDownloadStatus, 10000)
}

const stopWrfPolling = () => {
  clearTimeout(wrfPollTimer)
  wrfPollTimer = undefined
}

const scheduleWrfPolling = (sync) => {
  stopWrfPolling()
  if (selection.source !== 'wrf') return
  wrfPollTimer = setTimeout(() => applyTimes({ background: true }), wrfPollDelay(sync))
}

const stopGfsPolling = () => {
  clearTimeout(gfsPollTimer)
  gfsPollTimer = undefined
}

const scheduleGfsPolling = (download) => {
  stopGfsPolling()
  if (selection.source !== 'gfs') return
  gfsPollTimer = setTimeout(() => applyTimes({ background: true }), gfsPollDelay(download))
}

const loadWindField = async ({ snapshot = true, revisionRetry = false } = {}) => {
  const currentSnapshot = ++snapshotGeneration
  windRequestController?.abort()
  windRequestController = new AbortController()
  const signal = windRequestController.signal
  if (snapshot && planningDemo.value.active) {
    demo.dismiss()
    await syncReplayContext()
    if (currentSnapshot !== snapshotGeneration || signal.aborted) return
  }
  if (snapshot && fixedRouteSelection.value && flightReplay.value.mode === 'flight') replay.setMode('snapshot')
  if (fixedRouteSelection.value?.v3) {
    const route = fixedRouteSelection.value
    const profile = flightReplay.value.result
    if (!profile) return
    const text = selection.validTime
    const parsed = text ? Date.parse(text.includes('T') ? text : text.replace(' UTC', ':00Z').replace(' ', 'T')) : NaN
    const at = Number.isFinite(parsed) ? new Date(parsed).toISOString() : profile.departure_at
    loading.value = true
    try {
      const [frame, result] = await Promise.all([getReplayWind(profile.dataset_id, at, signal),
        getV3Snapshot(route, { ...departureConfiguration(), departure_at: at, dataset_id: profile.dataset_id })])
      if (currentSnapshot !== snapshotGeneration || route !== fixedRouteSelection.value || profile !== flightReplay.value.result || text !== selection.validTime) return
      const fields = interpolateWindFrames(frame, frame, at)
      if (!fields) throw new Error('V3 海拔风场未就绪')
      wind.value = fields.wind
      heatmap.value = fields.heatmap
      metadata.value = fields.wind.metadata
      snapshotRenderKey.value = `snapshot:${currentSnapshot}:${profile.dataset_id}:${at}`
      analysis.value = result.analysis
    } catch (reason) {
      if (currentSnapshot !== snapshotGeneration || route !== fixedRouteSelection.value || profile !== flightReplay.value.result) return
      wind.value = undefined
      heatmap.value = undefined
      analysis.value = undefined
      metadata.value = { dataset_id: profile.dataset_id, level: profile.level, cycle: profile.forecast_cycle,
        valid_time: at, source: 'WRF · 数据不足', altitude_reference: 'MSL' }
      showTemporaryError(apiErrorMessage(reason))
    } finally { if (currentSnapshot === snapshotGeneration) loading.value = false }
    return
  }
  if (!selection.source || !selection.level || !selection.validTime) return
  loading.value = true
  error.value = ''
  const selected = { ...selection }
  const matchesSelection = () => currentSnapshot === snapshotGeneration && !signal.aborted
    && JSON.stringify(selected) === JSON.stringify({ ...selection })
  try {
    const { wind: windData, heatmap: heatData } = await getWindField(selected, undefined, { signal })
    if (!matchesSelection()) return
    wind.value = windData
    heatmap.value = heatData
    metadata.value = windData.metadata
    snapshotRenderKey.value = `snapshot:${currentSnapshot}:${windData.metadata.frame_id}`
    if (routePoints.length) await runRouteAnalysis(routePoints, { preserveFixedRoute: true })
  } catch (reason) {
    if (!matchesSelection()) return
    if (!revisionRetry && reason.response?.status === 409 && String(reason.response?.data?.detail).includes('版本')) {
      await applyTimes({ autoLoad: false, background: true })
      if (currentSnapshot === snapshotGeneration && selected.source === selection.source) return loadWindField({ snapshot: false, revisionRetry: true })
      return
    }
    wind.value = undefined; heatmap.value = undefined; metadata.value = undefined
    paintedSnapshot.value = null
    snapshotRenderKey.value = ''
    if (apiDownloadStatus(reason)?.running) startDownloadPolling()
    showTemporaryError(apiErrorMessage(reason))
  } finally {
    if (currentSnapshot === snapshotGeneration) loading.value = false
  }
}

const normalizeDirection = (value) => ((value % 360) + 360) % 360

const loadedWindMatchesSelection = () => {
  if (planningDemo.value.active) return displayWind.value?.metadata?.dataset_id === planningDemo.value.profile?.dataset_id
  if (flightMode.value) return Boolean(displayWind.value?.metadata?.dataset_id === flightReplay.value.result?.dataset_id)
  const loaded = displayWind.value?.metadata
  if (!loaded || windLoading.value) return false
  if (fixedRouteSelection.value?.v3) {
    const profile = flightReplay.value.result
    const text = selection.validTime
    const expected = text ? Date.parse(text.includes('T') ? text : text.replace(' UTC', ':00Z').replace(' ', 'T')) : Date.parse(profile?.departure_at)
    return loaded.dataset_id === profile?.dataset_id && loaded.level === profile?.level
      && Date.parse(loaded.valid_time) === expected
  }
  if (loaded.level !== selection.level) return false
  if (selection.validTime) return loaded.valid_time_bj === selection.validTime || loaded.valid_time === selection.validTime
  return loaded.cycle === selection.cycle && Number(loaded.forecast_hour) === Number(selection.forecastHour)
}

const pointFromLoadedWind = (lon, lat) => {
  if (!loadedWindMatchesSelection()) return null
  const [uSource, vSource] = displayWind.value?.velocity || []
  const header = uSource?.header
  if (!header || !Array.isArray(uSource.data) || !Array.isArray(vSource?.data)) return null

  const { nx, ny, lo1, la1, dx, dy } = header
  if (![nx, ny, lo1, la1, dx, dy].every(Number.isFinite) || nx < 1 || ny < 1 || dx <= 0 || dy <= 0) return null

  const nearest = (value, first, step, count) => {
    let index = 0, best = Infinity
    for (let i = 0; i < count; i += 1) {
      const distance = Math.abs(first + i * step - value)
      if (distance < best) { index = i; best = distance }
    }
    return index
  }
  const bbox = displayWind.value?.metadata?.bbox
  if (bbox && (lon < bbox[0] || lon > bbox[2] || lat < bbox[1] || lat > bbox[3])) return null
  const col = nearest(lon, lo1, dx, nx)
  const row = nearest(lat, la1, -dy, ny)
  if (col < 0 || col >= nx || row < 0 || row >= ny) return null

  const index = row * nx + col
  const u = uSource.data[index]
  const v = vSource.data[index]
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
    level: displayMetadata.value?.level || selection.level,
    valid_time: displayMetadata.value?.valid_time,
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

const departureConfiguration = () => ({
  ...(fixedRouteSelection.value?.v3 || (!fixedRouteSelection.value && planner.algorithm === 'wrf_v3')
    ? { time_model: 'constant_airspeed', airspeed_mps: 10 }
    : { average_ground_speed_mps: flightSettings.averageGroundSpeedMps, source: fixedRouteSelection.value?.source || selection.source }),
  thresholds: { ...thresholds },
  wind_shear: currentWindShearSettings(),
})

const selectRouteSample = async (index) => {
  if (planningDemo.value.active) { onDemoComplete?.(); demo.dismiss(); await syncReplayContext() }
  selectedRouteSampleIndex.value = index
  const sample = displayAnalysis.value?.samples?.[index]
  if (flightMode.value && Number.isFinite(sample?.elapsed_seconds)) replay.seek(sample.elapsed_seconds)
}
const setProfileMode = async (mode) => {
  if (planningDemo.value.active) { onDemoComplete?.(); demo.dismiss(); await syncReplayContext() }
  selectedRouteSampleIndex.value = null
  replay.setMode(mode)
  if (mode === 'snapshot') await loadWindField()
}
const playFlight = async () => {
  if (planningDemo.value.active) { onDemoComplete?.(); demo.dismiss(); await syncReplayContext() }
  selectedRouteSampleIndex.value = null; replay.play()
}
const seekFlight = async (seconds) => {
  if (planningDemo.value.active) { onDemoComplete?.(); demo.dismiss(); await syncReplayContext() }
  return replay.seek(seconds)
}
const retryFlight = () => planningDemo.value.active ? demo.retry() : replay.retry()
const chooseCurrentDeparture = async () => {
  if (fixedRouteSelection.value?.v3) {
    const status = await getV3Status()
    replay.chooseDeparture(status.server_time)
    void departureEvaluator.refresh()
    return
  }
  if (temporalPlanningEnabled.value) {
    planningEntry.value = 'specified'
    planner.departureMode = 'now'
    planner.departureAt = ''
    scheduleAutomaticPlan()
    return
  }
  await departureEvaluator.refresh()
  if (departureWindows.value.result) replay.chooseDeparture(departureWindows.value.result.evaluated_at)
}
const chooseDeparture = (value) => {
  if (!Number.isFinite(Date.parse(value))) { showTemporaryError('请输入有效的起飞时间。'); return }
  if (temporalPlanningEnabled.value && !fixedRouteSelection.value?.v3) {
    planningEntry.value = 'specified'
    planner.departureAt = toBeijingInput(value)
    planner.departureMode = 'scheduled'
    scheduleAutomaticPlan()
  } else replay.chooseDeparture(value)
}

const exitFixedRoute = () => {
  demo.dismiss()
  fixedRouteGeneration += 1
  fixedRouteSelection.value = null
  departureEvaluator.clear()
  replay.setContext(null)
}

const runRouteAnalysis = async (points, { preserveFixedRoute = false } = {}) => {
  if (customDrawing.value.active) return
  if (!preserveFixedRoute) {
    temporalPlanningEnabled.value = false
    planningEntry.value = 'specified'
    clearTimeout(autoPlanTimer)
    exitFixedRoute()
    cancelPlanning()
  }
  selectedRouteSampleIndex.value = null
  routePoints = points
  if (!preserveFixedRoute) activeRouteId.value = ''
  const selected = { ...selection }
  const result = await analyzeRoute(selected, points, thresholds, currentWindShearSettings())
  if (routePoints === points && JSON.stringify(selected) === JSON.stringify({ ...selection })) analysis.value = result
}

const queryPoint = async ({ lon, lat, map }) => {
  const localPoint = pointFromLoadedWind(lon, lat)
  if (localPoint) {
    showPointPopup(localPoint, lon, lat, map)
    return
  }
  if (flightMode.value) {
    L.popup().setLatLng([lat, lon]).setContent('该位置或播放时刻的风场数据不足。').openOn(map)
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

const cancelPlanning = () => {
  onDemoVisible = undefined
  onDemoComplete = undefined
  retainPlanningResult = undefined
  demo.dismiss()
  routePlanController?.abort()
  routePlanController = undefined
  routePlanGeneration += 1
  planner.planning = false
  planner.planningMessage = ''
  planningTask.value = null
  searchProgress.value = null
  planningPublished.value = false
  recommendationController?.abort()
  recommendationController = undefined
  recommendationGeneration += 1
  recommending.value = false
}

const closeRecommendationPreview = () => {
  previewGeneration += 1
  recommendationPreview.value = null
  recommendationPreviewProfile.value = null
  recommendationPreviewMap.value = null
}
const receiveRecommendation = value => {
  if (value.stage === 'forecast_changed') {
    clearRecommendations()
    recommendation.value = value
    return
  }
  if (recommendationPreview.value && !value.recommendations?.some(card => card.id === recommendationPreview.value.id)) closeRecommendationPreview()
  recommendation.value = value
  rangeSwitcher.update(value)
}
const clearRecommendations = ({ baseline = false } = {}) => {
  rangeSwitcher.reset()
  rangeLatestResult=undefined;rangeDefaultDisplay=undefined;interruptRangeDisplay=undefined
  recommendationController?.abort()
  recommendationController = undefined
  recommendationGeneration += 1
  recommendationJobId = undefined
  recommending.value = false
  recommendation.value = null
  closeRecommendationPreview()
  if (baseline) recommendationBaseline.value = null
}
const stopRecommendation = () => {
  if (planner.planning) stopPlanning()
  recommendationController?.abort()
  recommendationController = undefined
  recommendationGeneration += 1
  recommending.value = false
}

const stopPlanning = () => {
  retainPlanningResult?.()
  const previous = planningTask.value
  cancelPlanning()
  planningTask.value = { ...previous, status: 'cancelled' }
  temporalPlanningEnabled.value = false
  const route = fixedRouteSelection.value
  planner.planningMessage = route ? '已停止优化，保留当前可行航线' : '规划已取消'
  if (route?.v3) departureEvaluator.select(route, departureConfiguration())
}

const clearPlannedRoute = ({ keepEndpoints = true } = {}) => {
  clearTimeout(baselineRefreshTimer)
  clearRecommendations({ baseline: true })
  temporalPlanningEnabled.value = false
  planningEntry.value = 'specified'
  clearTimeout(autoPlanTimer)
  exitFixedRoute()
  cancelPlanning()
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

const beginCustomDrawing = () => {
  if (customDrawing.value.saving) return
  const rangeMode = recommendationRange.enabled
  if (rangeMode) {
    try { departureRangeBounds(recommendationRange) } catch (reason) { showTemporaryError(reason.message); return }
  }
  const points = rangeMode ? recommendationRange.requiredWaypoints.map(p => p.slice()) : []
  const name = rangeMode ? recommendationRange.name : ''
  rangeDrawingSnapshot = rangeMode ? { name:planner.name, startText:planner.startText, endText:planner.endText } : undefined
  savedRouteNotice.value = ''
  clearTimeout(savedRouteNoticeTimer)
  pointRequestController?.abort()
  clearRoute()
  mapRef.value?.closePopup?.()
  layers.route = true
  drawing.begin()
  drawingPurpose.value = rangeMode ? 'departure_range' : 'fixed'
  if (name) drawing.rename(name)
  for (const point of points) drawing.add(point)
  workspace.showMap()
}

const completeCustomDrawing = () => completeRecommendationDrawing({
  complete:() => drawing.complete({ ...selection }), isDrawing:() => customDrawing.value.active,
  purpose:drawingPurpose.value, recommend:runRangeRecommendation,
})

const cancelCustomDrawing = async () => {
  drawing.cancel()
  if (drawingPurpose.value === 'departure_range' && rangeDrawingSnapshot) {
    const previous = rangeDrawingSnapshot
    rangeDrawingSnapshot = undefined
    await runWithPlannerAutoClearSuppressed(async () => Object.assign(planner, previous))
  }
}

const runWithPlannerAutoClearSuppressed = async (callback) => {
  suppressPlannerAutoClear += 1
  try {
    return await callback()
  } finally {
    await nextTick()
    suppressPlannerAutoClear -= 1
  }
}

const activateRecommendationBaseline = async payload => {
  clearRecommendations({ baseline: true })
  temporalPlanningEnabled.value = false
  planningEntry.value = 'specified'
  cancelPlanning()
  exitFixedRoute()
  const version = newBaselineVersion()
  recommendationBaseline.value = { ...payload, baseline_version: version }
  detourSegment.value = 0
  await runWithPlannerAutoClearSuppressed(async () => {
    await applyRouteToPlanner(payload.name, payload.points, { analyze: false })
    const context = { ...payload, route_id: `drawn-${version}`, planning_key: version, ephemeral: true,
      weather_selection: { cycle: selection.cycle, valid_time: selection.validTime }, fromDrawing: true }
    fixedRouteSelection.value = context
    departureEvaluator.select(context, departureConfiguration())
  })
  workspace.revealDecision()
  mapRef.value?.focusArea({ bounds: [
    [Math.min(...payload.points.map(p => p[1])), Math.min(...payload.points.map(p => p[0]))],
    [Math.max(...payload.points.map(p => p[1])), Math.max(...payload.points.map(p => p[0]))],
  ], maxZoom: 14, route: true })
}

const runRangeRecommendation = async () => {
  if (customDrawing.value.active) return
  let request
  try { request = recommendedRangeRequest(recommendationRange) }
  catch (reason) { showTemporaryError(reason.message); return }
  await runWithPlannerAutoClearSuppressed(async () => {
    planner.name = recommendationRange.name || planner.name
    planner.startText = request.required_waypoints[0].join(', ')
    planner.endText = request.required_waypoints.at(-1).join(', ')
  })
  return runPlan(undefined, { departureRange:request })
}

const runDetourRecommendation = async () => {
  const baseline = recommendationBaseline.value
  if (recommendationRange.enabled || !baseline || customDrawing.value.active) return
  stopRecommendation()
  clearRecommendations()
  const generation = recommendationGeneration
  const controller = new AbortController()
  recommendationController = controller
  const isCurrent = () => generation === recommendationGeneration && !controller.signal.aborted
    && recommendationBaseline.value?.baseline_version === baseline.baseline_version
  recommending.value = true
  workspace.revealDecision()
  try {
    await recommendRoute({ mode: 'fixed_route', points: baseline.points,
      source: selection.source, level: selection.level, cycle: selection.cycle, valid_time: selection.validTime,
      thresholds: { ...thresholds }, wind_shear: currentWindShearSettings(),
      average_ground_speed_mps: flightSettings.averageGroundSpeedMps,
      planner_type: planner.algorithm === 'wrf_v3' ? 'wa_lpa_star' : planner.algorithm,
      planning_strategy: planner.strategy, segment_index: detourSegment.value,
      max_detour_ratio: maxDetourPercent.value / 100, baseline_version: baseline.baseline_version }, {
      signal: controller.signal,
      onCreated: id => { if (isCurrent()) recommendationJobId = id },
      onWorkflow: value => { if (isCurrent()) receiveRecommendation(value) },
    })
  } catch (reason) {
    if (isCurrent() && reason.name !== 'AbortError') showTemporaryError(apiErrorMessage(reason))
  } finally {
    if (isCurrent()) { recommending.value = false; recommendationController = undefined }
  }
}

const previewRecommendation = async card => {
  demo.dismiss()
  closeRecommendationPreview()
  const generation = previewGeneration
  recommendationPreview.value = card
  replay.pause()
  try {
    const route = { ...card.plan.route_context, points: card.plan.points, source: card.plan.source,
      level: card.plan.level, dataset_id: card.plan.dataset_id, ephemeral: true }
    const profile = await getFlightProfile(route, { ...departureConfiguration(), dataset_id: card.plan.dataset_id,
      departure_at: card.first_departure_at })
    const frame = await getReplayWind(card.plan.dataset_id, card.first_departure_at)
    if (generation !== previewGeneration) return
    recommendationPreviewProfile.value = profile
    recommendationPreviewMap.value = interpolateWindFrames(frame, frame, frame.metadata.valid_time)
    workspace.revealDecision()
    mapRef.value?.focusArea({ bounds: [
      [Math.min(...card.plan.points.map(p => p[1])), Math.min(...card.plan.points.map(p => p[0]))],
      [Math.max(...card.plan.points.map(p => p[1])), Math.max(...card.plan.points.map(p => p[0]))],
    ], maxZoom: 14, route: true })
  } catch (reason) {
    if (generation === previewGeneration) { closeRecommendationPreview(); showTemporaryError(apiErrorMessage(reason)) }
  }
}

const applyRangeSelection = result => {
  const configuration=departureConfiguration()
  const key=`range-selection:${result.range_profile_key}`
  const sameProfile=fixedRouteSelection.value?.planning_key===key
  const context={...result.route_context,name:planner.name,planning_key:key,
    initial_profile:result.flight_profile,profile_signature:JSON.stringify(configuration)}
  analysis.value=result.analysis
  planningPublished.value=true
  if(!sameProfile) {
    demo.dismiss();replay.pause();closeRecommendationPreview()
    planner.points=result.points;routePoints=result.points;planner.missionItems=[]
    activeRouteId.value='';selectedRouteSampleIndex.value=null;fixedRouteSelection.value=context
  }
  departureEvaluator.clear()
  const current=fixedRouteSelection.value
  departureWindows.value={active:true,route:current,loading:false,
    result:result.departure_windows ? {...result.departure_windows,route_id:current.route_id} : null,
    message:result.departure_windows ? '' : '所选起飞时刻已验收，低风连续窗口待确认'}
  if(!sameProfile) {
    replay.setMode('flight')
    void syncReplayContext()
    mapRef.value?.focusArea({bounds:[
      [Math.min(...result.points.map(p=>p[1])),Math.min(...result.points.map(p=>p[0]))],
      [Math.max(...result.points.map(p=>p[1])),Math.max(...result.points.map(p=>p[0]))],
    ],maxZoom:14,route:true})
  }
}

const selectRangeRecommendation = card => { void rangeSwitcher.select(card.id) }

const adoptRecommendation = async card => {
  if (adoptingRecommendation.value) return
  const generation = recommendationGeneration
  adoptingRecommendation.value = true
  try {
    if (recommendationJobId) {
      const latest = await getRecommendationJob(recommendationJobId)
      if (latest.workflow?.stage === 'forecast_changed') throw new Error('预报已更新，请重新推荐')
    }
    if (Date.parse(card.first_departure_at) < Date.now()) throw new Error('起飞时刻已过期，请刷新推荐')
    const route = { ...card.plan.route_context, points: card.plan.points, level: card.plan.level,
      source: card.plan.source, dataset_id: card.plan.dataset_id, ephemeral: true }
    const configuration = departureConfiguration()
    const profile = await getFlightProfile(route, { ...configuration, dataset_id: card.plan.dataset_id,
      departure_at: card.first_departure_at })
    if (generation !== recommendationGeneration) return
    if (!profile.analysis.navigation_allowed) throw new Error('该起飞时刻复核未通过，请重新推荐')
    temporalPlanningEnabled.value = false
    cancelPlanning()
    clearRecommendations()
    await runWithPlannerAutoClearSuppressed(async () => {
      planner.departureMode = 'scheduled'
      planner.departureAt = toBeijingInput(profile.departure_at)
      await applyRouteToPlanner(planner.name, card.plan.points, { analyze: false })
      analysis.value = profile.analysis
      fixedRouteSelection.value = { ...route, planning_key: newBaselineVersion(), initial_profile: profile,
        profile_signature: JSON.stringify(configuration) }
      recommendationBaseline.value = { ...route, name: planner.name, baseline_version: newBaselineVersion() }
      detourSegment.value = 0
      departureEvaluator.select(fixedRouteSelection.value, configuration)
    })
    planner.planningMessage = `已采用推荐时刻 ${toBeijingInput(profile.departure_at).replace('T', ' ')}`
  } catch (reason) { showTemporaryError(apiErrorMessage(reason)) }
  finally { adoptingRecommendation.value = false }
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

const applyRouteToPlanner = async (name, points, { persist = false, routeId = '', missionItems = null, analyze = true } = {}) => {
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
  if (analyze) {
    try {
      const selected = { ...selection }
      const result = await analyzeRoute(selected, normalizedPoints, thresholds, currentWindShearSettings())
      if (routePoints === normalizedPoints && JSON.stringify(selected) === JSON.stringify({ ...selection })) analysis.value = result
    } catch (reason) {
      console.warn('导入航线分析失败:', reason)
      if (routePoints === normalizedPoints) analysis.value = undefined
    }
  }
  if (persist) {
    const saved = await saveRoute({
      name: planner.name,
      start,
      end,
      points: normalizedPoints,
      level: selection.level,
      source: selection.source,
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

const loadFixedRoute = async (savedRoute) => {
  if (customDrawing.value.saving) return
  planningEntry.value = 'specified'
  drawing.cancel()
  clearRecommendations({ baseline: true })
  temporalPlanningEnabled.value = false
  clearTimeout(autoPlanTimer)
  cancelPlanning()
  exitFixedRoute()
  const generation = ++fixedRouteGeneration
  workspace.revealDecision()
  const isCurrent = () => fixedRouteGeneration === generation
  switchingFixedRoute += 1
  try {
    await runWithPlannerAutoClearSuppressed(async () => {
      analysis.value = undefined
      selectedRouteSampleIndex.value = null
      mapRef.value?.clearDrawnRoute?.()
      selection.source = savedRoute.v3 ? 'wrf' : savedRoute.source || 'wrf'
      if (!savedRoute.v3) selection.level = savedRoute.level
      if (planner.algorithm === 'wrf_v3') planner.algorithm = 'wa_lpa_star'
      if (savedRoute.v3) planner.altitudeMslM = savedRoute.v3.altitude_msl_m
      fixedRouteSelection.value = savedRoute
      departureEvaluator.select(savedRoute, departureConfiguration())
      await applyRouteToPlanner(savedRoute.name, savedRoute.points, {
        routeId: savedRoute.route_id, missionItems: savedRoute.mission_items, analyze: false,
      })
    })
  } finally {
    switchingFixedRoute -= 1
  }
  if (!isCurrent()) return
  if (!savedRoute.v3) recommendationBaseline.value = { ...savedRoute, baseline_version: newBaselineVersion() }
  await applyTimes({ autoLoad: false, preferredLevel: savedRoute.v3 ? selection.level : savedRoute.level })
  if (!isCurrent()) return
  await loadWindField({ snapshot: false })
  if (isCurrent()) replay.setMode('flight')
  if (isCurrent()) planner.planningMessage = '固定航线已加载'
  if (isCurrent()) {
    const points = savedRoute.points
    mapRef.value?.focusArea({ bounds: [
      [Math.min(...points.map((p) => p[1])), Math.min(...points.map((p) => p[0]))],
      [Math.max(...points.map((p) => p[1])), Math.max(...points.map((p) => p[0]))],
    ], maxZoom: 14, route: true })
  }
}

const runPlan = async (savedRoute, { departureRange } = {}) => {
  if (savedRoute && Array.isArray(savedRoute.points)) {
    try { await loadFixedRoute(savedRoute) } catch (reason) { showTemporaryError(apiErrorMessage(reason)) }
    return
  }
  if (customDrawing.value.active) return
  const rangeMode = Boolean(departureRange)
  clearRecommendations({ baseline: true })
  clearTimeout(autoPlanTimer)
  temporalPlanningEnabled.value = true
  exitFixedRoute()
  cancelPlanning()
  planningEntry.value = rangeMode ? 'recommended' : 'specified'
  demo.reset()
  const generation = routePlanGeneration
  const controller = new AbortController()
  routePlanController = controller
  const isCurrent = () => generation === routePlanGeneration && !controller.signal.aborted
  const planningStartedAt = performance.now()
  onDemoVisible = () => {
    if (!isCurrent() || planningTask.value?.first_visible_elapsed_seconds != null) return
    planningTask.value = { ...planningTask.value, first_visible_elapsed_seconds: (performance.now() - planningStartedAt) / 1000 }
  }
  const sentConfiguration = departureConfiguration()
  let candidateRevision = 0
  let displayedSignature
  const focusRoute = points => mapRef.value?.focusArea({ bounds: [
    [Math.min(...points.map(p => p[1])), Math.min(...points.map(p => p[0]))],
    [Math.max(...points.map(p => p[1])), Math.max(...points.map(p => p[0]))],
  ], maxZoom: 14, route: true })
  let previewSearch = () => {}, firstVerifiedFocused = false
  const displayResult = (result) => {
    if (!isCurrent() || rangeMode && rangeSwitcher.pinned) return
    if (rangeMode && result.mode === 'departure_range') {
      departureEvaluator.clear()
      const windows = result.departure_windows
      departureWindows.value = { active:true, loading:false, result:windows,
        message:windows ? '' : '推荐起飞时刻已通过全程验收，低风连续窗口正在确认' }
    }
    if (result.time_aware && (result.reference_route || !result.points?.length || result.analysis?.navigation_allowed !== true)) {
      planner.points = []
      routePoints = []
      analysis.value = undefined
      exitFixedRoute()
      planner.planningMessage = result.planning_message || '尚未找到通过全程验收的可行航线'
      return
    }
    const signature = JSON.stringify([result.points, result.dataset_id, result.departure_at, result.average_ground_speed_mps])
    // A terminal poll returns the retained best route again. Only a real
    // improvement should reset playback, chart selection and the route view.
    if (signature === displayedSignature) return
    displayedSignature = signature
    const firstCandidate = candidateRevision === 0
    planningPublished.value = true
    if (result.v3 && planningTask.value?.first_visible_elapsed_seconds == null) {
      planningTask.value = { ...planningTask.value, first_visible_elapsed_seconds: (performance.now() - planningStartedAt) / 1000 }
    }
    searchProgress.value = null
    planner.points = result.points
    routePoints = result.points
    activeRouteId.value = ''
    analysis.value = result.analysis
    if (result.time_aware && result.route_context) {
      selectedRouteSampleIndex.value = null
      const context = { ...result.route_context, name: planner.name,
        planning_key: `${generation}:${++candidateRevision}`, initial_profile: result.flight_profile,
        profile_signature: JSON.stringify(sentConfiguration) }
      fixedRouteSelection.value = context
      // The retained candidate can be minutes old after optimization. Fetch a
      // fresh "depart now" verdict while keeping its forecast arrays pinned.
      if (result.v3) {
        departureWindows.value = { active: true, route: context, loading: true, result: null,
          message: '路线优化中；结束后计算固定航线的未来窗口' }
      } else if (rangeMode) {
        if (result.departure_windows) departureWindows.value = { active:true, route:context, loading:false,
          result:{...result.departure_windows, route_id:context.route_id} }
      } else departureEvaluator.select(context, sentConfiguration)
      if (firstCandidate) {
        workspace.revealDecision()
        focusRoute(result.points)
      }
    }
  }
  const animation = createAnimatedPlanningFlight({ isCurrent,
    compareDepartureRange:rangeMode,
    show: (result, options) => {
      replay.pause()
      if (!firstVerifiedFocused) { firstVerifiedFocused = true; focusRoute(result.points) }
      return demo.show(result, options)
    },
    stopPreview: () => demo.dismiss(), publish: displayResult,
    onFinishError: () => { if (isCurrent()) planner.planningMessage = '结束通知暂未确认，正在等待后台保留最佳航线' },
    play: () => {
      if(rangeMode && rangeSwitcher.pinned) return
      // The verified initial profile is adopted synchronously. A cold map
      // request or the independent window job must not delay flight playback.
      void syncReplayContext()
      void replay.play()
    },
  })
  onDemoComplete = () => animation.drawn()
  retainPlanningResult = () => animation.cancel({ retain: true })
  const abortAnimation = () => animation.cancel()
  controller.signal.addEventListener('abort', abortAnimation, { once: true })
  let rangeInterrupted=false
  if(rangeMode) {
    interruptRangeDisplay=()=>{rangeInterrupted=true;animation.cancel()}
    rangeDefaultDisplay=workflow=>{
      if(!isCurrent()) return false
      const first=workflow?.recommendations?.[0],latest=rangeLatestResult
      if(!first) {exitFixedRoute();planner.points=[];routePoints=[];analysis.value=undefined;return true}
      if(latest && latest.dataset_id===first.plan.dataset_id
        && Date.parse(latest.departure_at)===Date.parse(first.recommended_departure_at || first.first_departure_at)
        && latest.max_contact_wind_speed_mps<=workflow.peak_wind_limit_mps
        && JSON.stringify(latest.points)===JSON.stringify(first.plan.points)) {
        displayedSignature=undefined
        displayResult({...latest,departure_windows:first.windows?.low_wind_certified ? first.windows : null})
        void syncReplayContext();return true
      }
      exitFixedRoute();planner.points=[];routePoints=[];analysis.value=undefined
      return false
    }
  }
  const receiveResult = result => {
    if(rangeMode) {rangeLatestResult=result;if(rangeSwitcher.pinned) return}
    if(rangeMode && rangeInterrupted) displayResult(result)
    else if (isPlanningDemoCandidate(result)) animation.receive(result)
    else displayResult(result)
  }
  try {
    planner.planning = true
    planningTask.value = { status: 'queued', elapsed_seconds: 0 }
    workspace.showMap()
    planner.planningMessage = '正在寻找首条可行航线…'
    activeRouteId.value = ''
    planner.missionItems = []
    planner.points = []
    routePoints = []
    analysis.value = undefined
    mapRef.value?.clearDrawnRoute?.()
    if (planner.algorithm !== 'wrf_v3' && (!Number.isFinite(sentConfiguration.average_ground_speed_mps) || sentConfiguration.average_ground_speed_mps <= 0)) {
      throw new Error('平均对地速度必须是大于 0 的有效数值。')
    }
    const departureAt = !rangeMode && planner.departureMode === 'scheduled' ? fromBeijingInput(planner.departureAt) : null
    if (!rangeMode && planner.departureMode === 'scheduled' && !departureAt) throw new Error('请选择有效的未来起飞时间。')
    if (departureAt && Date.parse(departureAt) < Math.floor(Date.now()/60000)*60000) throw new Error('指定起飞时间已过，请选择未来时刻或现在起飞。')
    previewSearch = createPlanningSearchPreview({ isCurrent,
      isPublished: () => planningPublished.value || animation.hasCandidate,
      start: parsePoint(planner.startText), source: selection.source, level: selection.level,
      show: (result, options) => { replay.pause(); return demo.show(result, options) },
    })
    const result = await planRoute(
      selection,
      parsePoint(planner.startText),
      parsePoint(planner.endText),
      thresholds,
      planner.algorithm,
      planner.aircraftModel,
      planner.strategy,
      currentWindShearSettings(),
      {
        useRecommendations: planner.algorithm !== 'wrf_v3',
        forceReplan:Boolean(savedRoute?.forceReplan),
        onCreated: id => { if (isCurrent()) recommendationJobId = id },
        onFinishReady: finish => { if (isCurrent() && !rangeMode) animation.registerFinish(finish) },
        departureRange,
        onWorkflow: value => { if (isCurrent()) {
          receiveRecommendation(value); workspace.revealDecision()
          if (rangeMode && value.stage === 'forecast_changed') {
            animation.cancel(); temporalPlanningEnabled.value=false; cancelPlanning(); exitFixedRoute()
            planner.points=[]; routePoints=[]; analysis.value=undefined
            planner.planningMessage='预报已更新，请重新计算限定时间段内的推荐'
            return
          }
          if (rangeMode && value.recommended_departure_expired && !rangeSwitcher.pinned) {
            animation.invalidate(); exitFixedRoute(); planner.points=[]; routePoints=[]; analysis.value=undefined
            planner.planningMessage='之前的推荐起飞时刻已过，继续比较时间段内的其他时刻'
          }
        } },
        v3: { altitudeMslM: planner.altitudeMslM, startHeadingDeg: planner.startHeadingDeg, endHeadingDeg: planner.endHeadingDeg, constrainHeadings: planner.constrainHeadings },
        flight: { average_ground_speed_mps: sentConfiguration.average_ground_speed_mps, departure_at: departureAt },
        signal: controller.signal,
        onProgress: receiveResult,
        onCandidate: receiveResult,
        onSearchProgress: rangeMode ? undefined : previewSearch,
        onStatus: (state) => { if (isCurrent()) { planner.planningMessage = state.message; planningTask.value = { ...planningTask.value,
          status: state.status, feasible: state.feasible, elapsed_seconds: state.elapsed_seconds,
          preparation_seconds: state.preparation_seconds, queue_wait_seconds: state.queue_wait_seconds,
          first_feasible_elapsed_seconds: state.first_feasible_elapsed_seconds, workflow_stage: state.workflow?.stage,
          route_quality:state.route_quality,
          cache_hit:state.cache_hit } } },
      },
    )
    if (!result.recommendation_only) receiveResult(result)
    if (planner.algorithm !== 'wrf_v3') await animation.terminal(result.recommendation_only ? undefined : result)
    if (isCurrent() && result.v3 && fixedRouteSelection.value) departureEvaluator.select(fixedRouteSelection.value, sentConfiguration)
    if (isCurrent()) {
      planner.planningMessage = result.planning_message || '跨时刻航线规划完成'
      planningTask.value = { ...planningTask.value, status: result.planning_status || 'completed',cache_hit:Boolean(result.cache_hit) }
    }
  } catch (reason) {
    animation.cancel({ retain: true })
    if (!isCurrent() || reason.name === 'AbortError' || reason.code === 'ERR_CANCELED') return
    planningTask.value = { ...planningTask.value, status: 'failed' }
    planner.planningMessage = planner.points.length ? '规划中断，保留已显示的航线' : apiErrorMessage(reason)
    error.value = apiErrorMessage(reason)
    setTimeout(() => { error.value = '' }, 3000)
  } finally {
    controller.signal.removeEventListener('abort', abortAnimation)
    if (isCurrent()) {
      searchProgress.value = null
      if (!planningPublished.value) demo.dismiss()
      planner.planning = false
      routePlanController = undefined
      if (fixedRouteSelection.value?.v3 && !controller.signal.aborted && departureWindows.value.message) {
        departureEvaluator.select(fixedRouteSelection.value, sentConfiguration)
      }
    }
  }
}

const scheduleAutomaticPlan = () => {
  if (!temporalPlanningEnabled.value) return
  clearTimeout(autoPlanTimer)
  cancelPlanning()
  exitFixedRoute()
  planner.points = []
  routePoints = []
  analysis.value = undefined
  selectedRouteSampleIndex.value = null
  planner.planningMessage = '飞行参数已变更，正在重新规划…'
  autoPlanTimer = setTimeout(() => {
    if (temporalPlanningEnabled.value) void (planningEntry.value === 'recommended' ? runRangeRecommendation() : runPlan())
  }, 350)
}

const beginPicking = (kind) => { picking.value = kind; workspace.showMap() }

const handleMapClick = (payload) => {
  if (customDrawing.value.active) return
  if (picking.value) {
    planner[`${picking.value}Text`] = `${payload.lon.toFixed(5)}, ${payload.lat.toFixed(5)}`
    picking.value = ''
    if (compact.value) workspace.open('left')
    return
  }
  queryPoint(payload)
}

const currentRoutePayload = computed(() => {
  const points = normalizeRoutePoints(planner.points)
  return { name: planner.name.trim(), start: points[0]?.slice(0, 2), end: points.at(-1)?.slice(0, 2), points,
    level: fixedRouteSelection.value?.v3 ? fixedRouteSelection.value.level : selection.level,
    source: fixedRouteSelection.value?.source || selection.source, v3: fixedRouteSelection.value?.v3 || null,
    cycle: selection.cycle, forecast_hour: selection.forecastHour,
    forecast_minutes: Math.round(Number(selection.forecastHour)*60),
    valid_time: options.valid_times?.find(item=>item.label===selection.validTime)?.valid_time || null,
    mission_items: planner.missionItems.length ? planner.missionItems : null }
})
const currentRouteKey = computed(() => routeSnapshotKey(currentRoutePayload.value))
const savedCurrent = computed(() => {
  if (!currentRouteKey.value) return null
  if (savedReceipt.value?.key === currentRouteKey.value) {
    const stored = savedRoutes.value.find(route => route.route_id === savedReceipt.value.record.route_id)
    if (stored && routeSnapshotKey(stored) === routeSnapshotKey(savedReceipt.value.record)) return stored
  }
  return savedRoutes.value.find(route => route.route_id === activeRouteId.value && routeSnapshotKey(route) === currentRouteKey.value) || null
})
watch(currentRouteKey, () => { saveError.value = '' })
const persistRoute = async () => {
  demo.dismiss()
  if (savingRoute.value || savedCurrent.value || recommendationPreview.value) return
  if (planner.points.length < 2) { saveError.value = '尚无可保存的规划航线。'; return }
  if (!planner.name.trim()) { saveError.value = '请输入航线名称。'; return }
  const payload = JSON.parse(JSON.stringify(currentRoutePayload.value))
  const key = currentRouteKey.value
  savingRoute.value = true
  saveError.value = ''
  try {
    const saved = await saveRoute(payload)
    if (routeSaveDisposed) return
    savedReceipt.value = { key, record: saved }
    if (currentRouteKey.value === key) activeRouteId.value = saved.route_id
    savedRoutes.value = [saved, ...savedRoutes.value.filter(route => route.route_id !== saved.route_id)]
    savedRouteNotice.value = currentRouteKey.value === key ? '航线已保存，可选择导出格式。' : '已保存提交时的航线；当前航线已更新。'
    clearTimeout(savedRouteNoticeTimer)
    savedRouteNoticeTimer = setTimeout(() => { savedRouteNotice.value = '' }, 4000)
    void refreshRoutes()
    void controlPanelRef.value?.refreshJsonDialog?.()
  } catch (reason) {
    if (!routeSaveDisposed && currentRouteKey.value === key) saveError.value = apiErrorMessage(reason) || '保存失败，请重试。'
  } finally { if (!routeSaveDisposed) savingRoute.value = false }
}
const exportCurrentRoute = async (format) => {
  demo.dismiss()
  const saved = savedCurrent.value
  if (!saved || savingRoute.value || recommendationPreview.value) return
  const key = currentRouteKey.value
  try {
    const files = await listExportedRoutes()
    if (routeSaveDisposed || recommendationPreview.value || currentRouteKey.value !== key || savedCurrent.value?.route_id !== saved.route_id) return
    const file = exportForRoute(saved, files, savedRoutes.value)
    const name = format === 'waypoints' ? file?.waypoint_file_name : file?.file_name
    if (!name) throw new Error('未找到当前航线的对应导出文件。')
    downloadExportedFile(format === 'waypoints' ? getExportedWaypointUrl(name) : getExportedRouteUrl(name), name)
  } catch (reason) { if (currentRouteKey.value === key) showTemporaryError(apiErrorMessage(reason)) }
}

const importRouteFile = async ({ name, points, missionItems, raw }) => {
  if (customDrawing.value.saving) return
  drawing.cancel()
  exitFixedRoute()
  cancelPlanning()
  try {
    if (raw?.v3) {
      const saved = await saveRoute({ name, points, start: points[0].slice(0, 2), end: points[points.length - 1].slice(0, 2),
        level: raw.level, cycle: raw.cycle, forecast_hour: raw.forecast_hour, v3: raw.v3 })
      await refreshRoutes()
      await loadFixedRoute(saved)
      return
    }
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
  if (fixedRouteSelection.value?.route_id === id) exitFixedRoute()

  await refreshRoutes();
  controlPanelRef.value?.refreshJsonDialog?.()
  if (activeRouteId.value === id) activeRouteId.value = ''
}

const useZoomDefaultLayer = async (zoom) => {
  if (fixedRouteSelection.value || customDrawing.value.active || planner.planning || planningDemo.value.active) return
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

const applyTimes = async ({ autoLoad = true, background = false, preferredLevel } = {}) => {
  const source = selection.source
  const generation = ++timesGeneration
  const previousLookahead = options.lookahead
  const previousRevision = options.forecast_revision
  const previousSelection = [selection.validTime, selection.cycle, selection.forecastHour, selection.level].join('|')
  if (!background) {
    error.value = ''
    Object.assign(options, { cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '', lookahead: undefined, remote_sync: undefined })
  }
  try {
    const response = await getTimes(source)
    if (generation !== timesGeneration || source !== selection.source) return
    if (source === 'wrf') scheduleWrfPolling(response.remote_sync)
    if (source === 'gfs') scheduleGfsPolling(response.download)
    if (response.download?.running) startDownloadPolling()
    const availableTimes = response.wind_time_model === NATIVE_WRF_TIME_MODEL
      ? (response.valid_times || []) : keepUpcomingTimes(response.valid_times || [])
    Object.assign(options, { ...response, valid_times: availableTimes })
    const defaultTime = source === 'wrf'
      ? chooseWrfTime(options.valid_times, background ? selection.validTime : '', response.lookahead, previousLookahead)
      : chooseGfsTime(options.valid_times || [], background ? selection.validTime : '', pickDefaultTime(options.valid_times || []))
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
    selection.level = (!customDrawing.value.active && preferredLevel) || fixedRouteSelection.value?.level || (options.levels.includes(selection.level) ? selection.level : (options.levels.includes('100m AGL') ? '100m AGL' : options.levels.at(-1)))
    const newSelection = [selection.validTime, selection.cycle, selection.forecastHour, selection.level].join('|')
    if (autoLoad && (!background || previousSelection !== newSelection || previousRevision !== response.forecast_revision || !metadata.value)) await loadWindField({ snapshot: !background })
  } catch (reason) {
    if (generation !== timesGeneration || source !== selection.source) return
    if (source === 'wrf') scheduleWrfPolling()
    if (source === 'gfs') scheduleGfsPolling()
    if (apiDownloadStatus(reason)?.running) startDownloadPolling()
    showTemporaryError(apiErrorMessage(reason))
  }
}

watch(() => layers.velocity, (enabled) => {
  void replay.setMapUpdateInterval(enabled ? PARTICLE_MAP_UPDATE_INTERVAL_SECONDS : 0)
})

watch(() => selection.validTime, (validTime) => {
  if (!validTime) return
  const option = options.valid_times?.find((item) => item.label === validTime)
  if (option) {
    selection.cycle = option.cycle
    selection.forecastHour = option.forecast_hour
  }
})

watch(() => selection.source, async (source) => {
  timesGeneration += 1
  stopWrfPolling()
  stopGfsPolling()
  stopDownloadPolling()
  if (switchingFixedRoute) {
    flightSettings.averageGroundSpeedMps = source === 'gfs' ? 50 : 10
    return
  }
  const previousBaseline = recommendationBaseline.value
  clearRoute()
  flightSettings.averageGroundSpeedMps = source === 'gfs' ? 50 : 10
  if (!source) {
    Object.assign(options, { cycles: [], forecast_hours: [], valid_times: [], levels: [], source: '' })
    wind.value = undefined
    heatmap.value = undefined
    metadata.value = undefined
    return
  }
  await applyTimes()
  if (previousBaseline && selection.source === source && !customDrawing.value.active) {
    await activateRecommendationBaseline({ ...previousBaseline, source, level: selection.level,
      cycle: selection.cycle, forecast_hour: selection.forecastHour })
  }
})

watch(() => [planner.algorithm, planner.strategy, planner.aircraftModel], () => {
  if (suppressPlannerAutoClear) return
  if (recommendationBaseline.value && !temporalPlanningEnabled.value) { clearRecommendations(); return }
  clearPlannedRoute({ keepEndpoints: true })
})

watch(() => [planner.startText, planner.endText], () => {
  if (suppressPlannerAutoClear) return
  clearPlannedRoute({ keepEndpoints: true })
})

watch(() => [selection.validTime, selection.cycle, selection.forecastHour, selection.level, ...Object.values(thresholds)], () => {
  if (!temporalPlanningEnabled.value) cancelPlanning()
})

watch(displayAnalysis, () => { selectedRouteSampleIndex.value = null }, { flush: 'sync' })

const syncReplayContext = () => {
  if (planningDemo.value.active) return
  const route = fixedRouteSelection.value
  const result = departureWindows.value.result
  const matches = route?.v3 ? result?.trajectory_id === route.v3.trajectory_id : route && result?.route_id === route.route_id
  return replay.setContext(route, departureConfiguration(), matches ? result : undefined)
}
watch(() => [fixedRouteSelection.value, JSON.stringify(departureConfiguration()), departureWindows.value.result, planningDemo.value.active], syncReplayContext)
watch(() => departureWindows.value.error, (message) => {
  if (fixedRouteSelection.value && message && !flightReplay.value.result && !planningDemo.value.active) replay.setError(message)
  if (temporalPlanningEnabled.value && message?.includes('过期')) scheduleAutomaticPlan()
})

watch(() => [flightSettings.averageGroundSpeedMps, ...Object.values(thresholds)], () => {
  if (temporalPlanningEnabled.value && !(fixedRouteSelection.value?.v3 && !planner.planning)) scheduleAutomaticPlan()
  else departureEvaluator.update(departureConfiguration())
})
watch(() => [planner.altitudeMslM, planner.startHeadingDeg, planner.endHeadingDeg, planner.constrainHeadings], () => {
  if (!suppressPlannerAutoClear && planner.algorithm === 'wrf_v3' && temporalPlanningEnabled.value) scheduleAutomaticPlan()
})
watch(() => [planner.departureMode, planner.departureMode === 'scheduled' ? planner.departureAt : ''], () => {
  if (suppressPlannerAutoClear || planningEntry.value === 'recommended') return
  if (temporalPlanningEnabled.value) scheduleAutomaticPlan()
})
watch(() => [recommendationRange.enabled, recommendationRange.departureStartAt, recommendationRange.departureEndAt], () => {
  if (suppressPlannerAutoClear) return
  if (planningEntry.value === 'recommended') clearPlannedRoute({keepEndpoints:true})
  else if (recommending.value || recommendation.value?.mode === 'fixed_route') clearRecommendations()
})
watch(() => selection.level, (level) => {
  if (fixedRouteSelection.value?.v3) return
  if (temporalPlanningEnabled.value && !switchingFixedRoute) { scheduleAutomaticPlan(); return }
  if (!switchingFixedRoute && fixedRouteSelection.value && level !== fixedRouteSelection.value.level) exitFixedRoute()
})
watch(() => JSON.stringify([options.forecast_revision ?? options.remote_sync?.last_success, options.valid_times?.map((item) => [item.cycle, item.forecast_hour])]), (signature, previous) => {
  if (signature === previous) return
  if(recommendation.value?.mode==='departure_range') {
    clearPlannedRoute({keepEndpoints:true})
    planner.planningMessage='预报已更新，请重新计算时间段内的推荐方案'
    return
  }
  if (recommendation.value) clearRecommendations()
  if (fixedRouteSelection.value?.v3) {
    if (!planner.planning && !switchingFixedRoute) departureEvaluator.update(departureConfiguration())
    return
  }
  if (temporalPlanningEnabled.value && fixedRouteSelection.value && !planner.planning) { scheduleAutomaticPlan(); return }
  if (fixedRouteSelection.value && !switchingFixedRoute) {
    const current = fixedRouteSelection.value
    if (current.ephemeral && !current.v3) {
      const { dataset_id, initial_profile, profile_signature, ...geometry } = current
      const fresh = { ...geometry, planning_key: newBaselineVersion(),
        weather_selection: { cycle: selection.cycle, valid_time: selection.validTime } }
      fixedRouteSelection.value = fresh
      departureEvaluator.select(fresh, departureConfiguration())
    } else departureEvaluator.update(departureConfiguration())
  }
})
const refreshDepartureOnFocus = () => { if (fixedRouteSelection.value) departureEvaluator.refresh() }
watch(() => [selection.validTime, selection.cycle, selection.level, flightSettings.averageGroundSpeedMps, ...Object.values(thresholds)], () => {
  if (suppressPlannerAutoClear || switchingFixedRoute) return
  const baseline = recommendationBaseline.value
  const searching = planner.planning
  clearRecommendations()
  if (searching) cancelPlanning()
  if (temporalPlanningEnabled.value) { scheduleAutomaticPlan(); return }
  if (baseline && !customDrawing.value.active) {
    departureEvaluator.clear()
    clearTimeout(baselineRefreshTimer)
    baselineRefreshTimer = setTimeout(() => void activateRecommendationBaseline({ ...baseline, source: selection.source,
      level: selection.level, cycle: selection.cycle, forecast_hour: selection.forecastHour }), 300)
  }
})
watch(() => [detourSegment.value,maxDetourPercent.value], () => clearRecommendations())

onMounted(async () => {
  dockObserver = new ResizeObserver(([entry]) => { dockHeight.value = Math.ceil(entry.target.getBoundingClientRect().height) })
  if (dockEl.value) dockObserver.observe(dockEl.value)
  window.addEventListener('focus', refreshDepartureOnFocus)
  window.addEventListener('pagehide', cancelPlanning)
  window.addEventListener('pagehide', rangeSwitcher.cancelPending)
  await applyTimes()
  await refreshRoutes()
  getV3Status().then((status) => { options.v3_native = status }).catch(() => {})
})

onBeforeUnmount(() => {
  snapshotGeneration += 1
  windRequestController?.abort()
  routeSaveDisposed = true
  dockObserver?.disconnect()
  clearTimeout(savedRouteNoticeTimer)
  clearTimeout(autoPlanTimer)
  clearTimeout(baselineRefreshTimer)
  departureEvaluator.dispose()
  replay.dispose()
  demo.dispose()
  drawing.dispose()
  rangeSwitcher.dispose()
  window.removeEventListener('focus', refreshDepartureOnFocus)
  window.removeEventListener('pagehide', cancelPlanning)
  window.removeEventListener('pagehide', rangeSwitcher.cancelPending)
  timesGeneration += 1
  stopWrfPolling()
  stopGfsPolling()
  stopDownloadPolling()
  cancelPlanning()
  pointRequestController?.abort()
})
</script>

<template>
  <div class="app-shell workspace" :class="{ 'compact-workspace': compact, 'is-resizing': resizing }" :style="layoutStyle">
    <header class="workspace-header">
      <span class="system-emblem"><WorkspaceIcon name="flight" /></span>
      <h1><span class="system-brand">御风智航</span>-面向低空无人机通航决策的风场预报平台</h1>
    </header>
    <div v-if="compact && (panels.leftOpen || panels.rightOpen)" class="panel-backdrop" @click="workspace.showMap()"></div>
    <WorkspacePanel side="left" title="规划与航线" :width="panels.leftWidth" :visible="panels.leftOpen" :compact="compact"
      @toggle="panels.leftOpen ? workspace.close('left') : workspace.open('left')">
      <ControlPanel ref="controlPanelRef" :native-level="fixedRouteSelection?.v3 ? fixedRouteSelection.level : ''" :replay-active="flightMode" :options="options" :selection="selection" :layers="layers" :thresholds="thresholds" :flight-settings="flightSettings" :planner="planner" :saved-routes="savedRoutes" :area-selection="areaSelection" :area-presets="areaPresets" :loading="windLoading" :picking="picking" :drawing-active="customDrawing.active" :drawing-saving="customDrawing.saving" :saved-current="Boolean(savedCurrent)" :saving-route="savingRoute" :save-error="saveError" :planning-status="planningStatus" :recommendation-baseline="recommendationBaseline" :recommendation-range="recommendationRange" :planning-entry="planningEntry" :detour-segment="detourSegment" :max-detour-percent="maxDetourPercent" :recommending="recommending" :previewing-recommendation="Boolean(recommendationPreview)" @detour-segment="detourSegment = $event" @detour-limit="maxDetourPercent = $event" @recommend-detour="runDetourRecommendation" @cancel-recommendation="stopRecommendation" @export-route="exportCurrentRoute" @notice="showTemporaryError" @draw-custom-route="beginCustomDrawing" @recommend-range="runRangeRecommendation" @reload="loadWindField" @clear-route="clearRoute" @pick-start="beginPicking('start')" @pick-end="beginPicking('end')" @plan-route="runPlan" @cancel-plan="stopPlanning" @save-route="persistRoute" @load-routes="refreshRoutes" @delete-route="removeRoute" @focus-area="focusArea" @import-route-file="importRouteFile" />
    </WorkspacePanel>
    <WorkspacePanel side="right" title="通航决策" :width="panels.rightWidth" :visible="panels.rightOpen" :compact="compact"
      @toggle="panels.rightOpen ? workspace.close('right') : workspace.open('right')">
      <AnalysisPanel :route-active="planner.points.length > 1" :planning-status="planningStatus" :metadata="displayMetadata" :analysis="displayAnalysis" :thresholds="thresholds" :departure-windows="departureWindows" :replay="analysisReplay" :auto-replan="temporalPlanningEnabled" :recommendation="recommendation" :recommendation-preview="recommendationPreview?.id" :recommendation-selected="rangeSelection.selectedId" :recommendation-pending="rangeSelection.pendingId" :adopting-recommendation="adoptingRecommendation" :recommending="recommending || planner.planning" @recommendation-preview="previewRecommendation" @recommendation-select="selectRangeRecommendation" @recommendation-adopt="adoptRecommendation" @recommendation-close="closeRecommendationPreview" @recommendation-cancel="stopRecommendation" :playhead-sample-index="aircraftPosition?.sampleIndex" :selected-sample-index="selectedRouteSampleIndex" @sample-select="selectRouteSample" @departure-select="chooseDeparture" @depart-now="chooseCurrentDeparture" @window-cancel="departureEvaluator.stop()" />
    </WorkspacePanel>
    <WindMap ref="mapRef" :recommendations="recommendation?.mode === 'departure_range' ? undefined : recommendation?.recommendations" :recommendation-preview="Boolean(recommendationPreview)" :detour-endpoints="detourEndpoints" :search-progress="null" :planning-points="planningDemo.active ? planningDemo.points : undefined" :layout-insets="insets" :dock-height="dockHeight" :wind="renderWind" :heatmap="renderHeatmap" :frame-key="mapFrameKey" @wind-frame-ready="windFrameReady" :metadata="displayMetadata" :layers="layers" :thresholds="thresholds" :analysis="displayAnalysis" :planner="planner" :flight-mode="flightMode" :flight-profile="planningDemo.active ? planningDemo.profile : flightReplay.result" :aircraft-position="aircraftPosition" :selected-sample-index="selectedRouteSampleIndex" :custom-drawing="customDrawing" @drawing-point-add="drawing.add($event)" @drawing-point-move="drawing.move($event.index, $event)" @point-click="handleMapClick" @route-created="runRouteAnalysis" @zoom-changed="useZoomDefaultLayer" />
    <div ref="dockEl" class="map-dock" :class="{ 'beside-wind-legend': layers.heatmap || layers.velocity, 'replay-dock': !customDrawing.active }">
      <CustomRouteDrawingControls :purpose="drawingPurpose" :draft="customDrawing" :selection="selection" :speed="flightSettings.averageGroundSpeedMps" @rename="drawing.rename($event)" @undo="drawing.undo()" @clear="drawing.clear()" @cancel="cancelCustomDrawing" @save="completeCustomDrawing" />
      <Transition name="replay-panel">
        <FlightReplayControls v-if="!recommendationPreview && replayDockVisible" :replay="analysisReplay" @play="playFlight" @pause="replay.pause()" @seek="seekFlight($event)" @rate-change="replay.setRate($event)" @mode-change="setProfileMode" @retry="retryFlight()" />
      </Transition>
    </div>
    <div v-if="error" class="error-toast" role="alert" @click="error = ''">{{ error }}</div>
    <div v-else-if="savedRouteNotice" class="error-toast route-saved-toast" role="status" @click="savedRouteNotice = ''">{{ savedRouteNotice }}</div>
  </div>
</template>

<style scoped>
.route-saved-toast { color: #d6fff2; background: rgba(7, 48, 39, .96); border-color: #44d7b680; box-shadow: 0 8px 24px #0008; }
</style>
