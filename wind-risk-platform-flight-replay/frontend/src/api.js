import axios from 'axios'
import { pollRouteJob } from './utils/routePlanning.js'
import { isDisplayableRoute } from './utils/routeQuality.js'
import { taskOwner } from './utils/taskOwner.js'
import { pollRecommendationJob } from './utils/recommendations.js'
import { createPageRouteCache,createPagePlanningGate } from './utils/pagePlanning.js'
import { isV3Route, pollV3Windows, createWindowRequestCoordinator } from './utils/v3Windows.js'
import { createWindRequestCache } from './utils/windRequestCache.js'
import { decodeWindPacket, windFrameFields, WIND_FRAME_MIME } from './utils/windFrameCodec.js'

const api = axios.create({ baseURL: '/api', timeout: 60000 })
const windRequests = createWindRequestCache()
const weatherRevisions = new Map()
const forecastRevisions = new Map()
export const getWindCacheStats = () => windRequests.stats()
const weatherRequest = (key, url, params, signal, priority = 1) => windRequests.get(key,
  (requestSignal) => api.get(url, { params, signal: requestSignal }).then(({ data }) => data), { signal, priority })
const binaryWeatherRequest = (key, url, params, signal, priority = 1) => windRequests.get(key, async (requestSignal) => {
  try {
    const { data } = await api.get(url, { params, signal: requestSignal, responseType: 'arraybuffer',
      paramsSerializer: { indexes: null }, headers: { Accept: WIND_FRAME_MIME } })
    return decodeWindPacket(data)
  } catch (error) {
    if (error.response?.data instanceof ArrayBuffer) {
      try { error.response.data = JSON.parse(new TextDecoder().decode(error.response.data)) } catch {}
    }
    throw error
  }
}, { signal, priority })
const pageRouteCache=createPageRouteCache()
const planningGate=createPagePlanningGate({owner:taskOwner,cancelPage:(client,sequence)=>api.delete(
  `/route/planning-clients/${encodeURIComponent(client)}`,{params:{through:sequence},timeout:5000}).then(({data})=>data)})
export const getPagePlanningCacheStats=()=>pageRouteCache.stats()
export const CHINA_BBOX = [73, 18, 135, 54]

const selectionParams = (selection, bbox) => ({
  source: selection.source,
  ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
  level: selection.level,
  ...(bbox ? { bbox: bbox.join(',') } : {}),
})

const containsPoint = (bbox, lon, lat) => bbox && lon >= bbox[0] && lon <= bbox[2] && lat >= bbox[1] && lat <= bbox[3]

export const getTimes = (source = 'gfs') => api.get('/times', { params: { source } }).then(({ data }) => {
  const revision = JSON.stringify([data.forecast_revision, data.planning_runtime_id, data.wind_time_model])
  forecastRevisions.set(source, data.forecast_revision)
  if (weatherRevisions.get(source) !== revision) {
    windRequests.invalidate((key) => key.startsWith(`snapshot:${source}:`))
    weatherRevisions.set(source, revision)
  }
  return data
})
export const getGfsDownloadStatus = () => api.get('/gfs/download-status').then(({ data }) => data)
const routeEvaluation = (route, configuration, kind, signal) => {
  const transient = typeof route === 'object' && route.ephemeral
  const payload = transient ? { ...route.weather_selection, points: route.points, level: route.level, name: route.name,
    dataset_id: route.dataset_id, ...configuration } : configuration
  const url = transient ? `/route/${kind}` : `/routes/${encodeURIComponent(route)}/${kind}`
  return api.post(url, payload, { signal }).then(({ data }) => data)
}
export const getV3Status = () => api.get('/wrf-v3/status').then(({ data }) => data)
const v3Payload = (route, configuration) => ({ trajectory_id: route.v3?.trajectory_id || route.trajectory_id,
  route_id: route.route_id, name: route.name, ...configuration })
const coordinateWindows = createWindowRequestCoordinator()
export const getDepartureWindows = (route, configuration, signal, onProgress) => isV3Route(route)
  ? coordinateWindows(JSON.stringify([route.v3?.trajectory_id || route.trajectory_id, configuration]), signal, () => pollV3Windows({ signal, onProgress,
    create: (signal) => api.post('/route/departure-windows/jobs', v3Payload(route, configuration), { signal, timeout: 15000 }).then(({ data }) => data),
    get: (id, revision, signal) => api.get(`/route/departure-windows/jobs/${id}`, { params: { since_revision: Math.max(0, revision) }, signal, timeout: 15000 }).then(({ data }) => data),
    cancel: (id) => api.delete(`/route/departure-windows/jobs/${id}`, { timeout: 5000 }),
  })) : routeEvaluation(route, configuration, 'departure-windows', signal)
export const getFlightProfile = (route, configuration, signal) => isV3Route(route)
  ? api.post('/route/v3/flight-profile', v3Payload(route, configuration), { signal, timeout: 75000 }).then(({ data }) => data)
  : routeEvaluation(route, configuration, 'flight-profile', signal)
export const getV3Snapshot = (route, configuration) => api.post('/route/v3/snapshot', v3Payload(route, configuration), { timeout: 75000 }).then(({ data }) => data)
export const getReplayWind = (datasetId, at, signal, { prefetch = false } = {}) => datasetId.startsWith('v3_')
  ? weatherRequest(`replay:${datasetId}:${at}`, '/flight-replay/wind', { dataset_id: datasetId, at }, signal, prefetch ? 0 : 1)
  : binaryWeatherRequest(`replay:${datasetId}:${at}`, '/wind/frame', { dataset_id: datasetId, at }, signal, prefetch ? 0 : 1)
    .then((frames) => Array.isArray(frames) ? frames[0] : frames)
export const getReplayWindFrames = async (datasetId, times, signal) => {
  if (datasetId.startsWith('v3_')) return Promise.all(times.map((at) => getReplayWind(datasetId, at, signal, { prefetch: true })))
  const resolved = new Map(times.map((at) => [at, windRequests.peek(`replay:${datasetId}:${at}`)]))
  const pending = times.filter((at) => !resolved.get(at))
  if (pending.length) {
    const frames = await binaryWeatherRequest(`replay-batch:${datasetId}:${pending.join('|')}`, '/wind/frames',
      { dataset_id: datasetId, at: pending }, signal, 0)
    frames.forEach((frame, index) => {
      resolved.set(pending[index], [frame])
      windRequests.prime(`replay:${datasetId}:${pending[index]}`, [frame])
    })
  }
  return times.map((at) => {
    const value = resolved.get(at)
    return Array.isArray(value) ? value[0] : value
  })
}
export const getWindField = async (selection, bbox = CHINA_BBOX, { signal } = {}) => {
  const params = { source: selection.source, level: selection.level, revision: forecastRevisions.get(selection.source),
    ...(selection.cycle && Number.isFinite(Number(selection.forecastHour))
      ? { cycle: selection.cycle, forecast_minutes: Math.round(Number(selection.forecastHour) * 60) }
      : { valid_time: selection.validTime }), ...(bbox ? { bbox: bbox.join(',') } : {}) }
  const frames = await binaryWeatherRequest(`snapshot:${selection.source}:${weatherRevisions.get(selection.source)}:frame:${JSON.stringify(params)}`,
    '/wind/frame', params, signal)
  const fields = windFrameFields(frames[0])
  if (!fields) throw Error(frames[0]?.reason || '该时刻风场数据不足')
  return fields
}
const snapshotWeather = (kind, selection, bbox, signal) => {
  const params = selectionParams(selection, bbox)
  const key = `snapshot:${selection.source}:${weatherRevisions.get(selection.source)}:${kind}:${JSON.stringify(params)}`
  return weatherRequest(key, `/${kind}`, params, signal)
}
export const getWind = (selection, bbox = CHINA_BBOX, { signal } = {}) => snapshotWeather('wind', selection, bbox, signal)
export const getHeatmap = (selection, bbox = CHINA_BBOX, { signal } = {}) => snapshotWeather('heatmap', selection, bbox, signal)
export const getPoint = (selection, lon, lat, { bbox = CHINA_BBOX, signal } = {}) =>
  api.get('/point', { params: { ...selectionParams(selection, containsPoint(bbox, lon, lat) ? bbox : undefined), lon, lat }, signal }).then(({ data }) => data)
export const analyzeRoute = (selection, points, thresholds, windShear) =>
  api.post('/route/analyze', {
    points,
    ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
    source: selection.source,
    level: selection.level,
    thresholds,
    wind_shear: windShear,
  }).then(({ data }) => data)
export const planRoute = (selection, start, end, thresholds, plannerType = 'wa_lpa_star', aircraftModel = 'fixed_wing', planningStrategy = 'wind_avoidance', windShear, progress = {}) => planningGate.run(async owner => {
  const payload = {
    client_instance_id: owner.client_instance_id,client_request_sequence:owner.client_request_sequence,
    start, end,
    ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
    source: selection.source,
    level: selection.level,
    thresholds,
    planner_type: plannerType,
    aircraft_model: aircraftModel,
    planning_strategy: planningStrategy,
    ...(plannerType !== 'wrf_v3' ? { stop_on_first_feasible: false, optimize_during_display: true, display_segment_seconds: 2,
      prefer_simple_route: true, route_quality_extra_seconds: 10 } : {}),
    wind_shear: windShear,
    ...(plannerType === 'wrf_v3' ? { altitude_msl_m: progress.v3?.altitudeMslM ?? 300,
      constrain_headings: Boolean(progress.v3?.constrainHeadings),
      start_heading_deg: progress.v3?.startHeadingDeg === '' ? null : progress.v3?.startHeadingDeg ?? null,
      end_heading_deg: progress.v3?.endHeadingDeg === '' ? null : progress.v3?.endHeadingDeg ?? null } : {}),
    ...(progress.flight ? { time_aware: true, average_ground_speed_mps: progress.flight.average_ground_speed_mps,
      departure_at: progress.flight.departure_at || null } : {}),
  }
  let version,restored
  const epoch=pageRouteCache.epoch
  const classic=plannerType!=='wrf_v3'
  if (classic) {
    version=await api.get('/times',{params:{source:selection.source,auto_download:false},signal:owner.signal,timeout:15000}).then(({data})=>data)
    if (progress.departureRange) {
      Object.assign(payload, progress.departureRange, { cycle:selection.cycle, planning_strategy:'wind_avoidance',
        optimize_during_display:false, stop_on_first_feasible:false })
      payload.seed_routes=pageRouteCache.seeds(payload,version)
      return pollOwnedRecommendation(payload,{...progress,signal:owner.signal,
        onCandidate:(value,state)=>{pageRouteCache.put(payload,version,value,epoch);progress.onCandidate?.(value,state)}})
        .then(state=>state.result ? {...state.result,planning_status:state.status,planning_message:state.message}
          : {recommendation_only:true,workflow:state.workflow,planning_status:state.status,planning_message:state.message})
    }
    const cached=pageRouteCache.get(payload,version)
    if (cached && !progress.forceReplan && isDisplayableRoute(cached)) {
      try {
        const profile=await getFlightProfile({...cached.route_context,points:cached.points,source:payload.source,
          level:payload.level,dataset_id:undefined,ephemeral:true,weather_selection:{cycle:selection.cycle,valid_time:selection.validTime}},
          {source:payload.source,departure_at:payload.departure_at || version.server_time || new Date(Date.now()).toISOString(),thresholds:payload.thresholds,
            wind_shear:payload.wind_shear,average_ground_speed_mps:payload.average_ground_speed_mps},owner.signal)
        if (profile.analysis.navigation_allowed) {
          const {objective_cost,...geometry}=cached
          restored={...geometry,analysis:profile.analysis,flight_profile:profile,segments:profile.segments,
            dataset_id:profile.dataset_id,departure_at:profile.departure_at,arrival_at:profile.arrival_at,
            distance_km:profile.analysis.total_distance_km,source:payload.source,level:payload.level,
            route_context:{...cached.route_context,points:cached.points,dataset_id:profile.dataset_id,departure_at:profile.departure_at},
            cache_hit:true,planning_status:'cached',planning_message:'缓存航线已通过全程复核，正在分段显示'}
          if (cached.dataset_id===profile.dataset_id && cached.departure_at===profile.departure_at) restored.objective_cost=objective_cost
          pageRouteCache.put(payload,version,restored,epoch)
          progress.onProgress?.(restored)
          if (!progress.forceReplan) {
            progress.onStatus?.({status:'cached',cache_hit:true,feasible:true,elapsed_seconds:0,message:restored.planning_message})
            return restored
          }
        }
      } catch (reason) { if (owner.signal.aborted) throw reason /* A failed cache recheck falls through to search. */ }
    }
    payload.seed_routes=pageRouteCache.seeds(payload,version)
  }
  const tracked={...progress,signal:owner.signal,hasInitialResult:Boolean(restored),
    onCandidate:(value,state)=>{if(classic)pageRouteCache.put(payload,version,value,epoch);progress.onCandidate?.(value,state)}}
  if (progress.useRecommendations && plannerType !== 'wrf_v3') {
    return pollOwnedRecommendation({ ...payload, cycle: selection.cycle, mode: 'specified_time' }, tracked).then(state => state.result
      ? { ...state.result, planning_status: state.status, planning_message: state.message }
      : { recommendation_only: true, workflow: state.workflow, planning_status: state.status, planning_message: state.message })
  }
  return pollRouteJob({
    ...tracked,
    initialDisplayDelayMs: classic ? 0 : 30000,
    onCreated: (id) => {
      progress.onFinishReady?.(() => api.post(`/route/plan/jobs/${id}/finish`, null, { timeout: 15000 }))
      return taskOwner?.attach(id)
    },
    create: () => api.post('/route/plan/jobs', payload, { timeout: 120000 }).then(({ data }) => data),
    get: (id, revision, signal, progressRevision) => api.get(`/route/plan/jobs/${id}`, {
      params: { since_revision: Math.max(0, revision), since_progress_revision: Math.max(0, progressRevision ?? -1) }, signal, timeout: 15000,
    }).then(({ data }) => data),
    cancel: (id) => api.delete(`/route/plan/jobs/${id}`, { timeout: 5000 }),
  })
},{signal:progress.signal,onWaiting:()=>progress.onStatus?.({status:'waiting_for_previous',elapsed_seconds:0,message:'正在释放上次规划资源'})})
export const getRecommendationJob = id => api.get(`/route/recommendation/jobs/${id}`).then(({ data }) => data)
const pollOwnedRecommendation = (payload, progress = {}) => pollRecommendationJob({ ...progress,
  initialDisplayDelayMs: payload.mode === 'departure_range' || payload.stop_on_first_feasible || payload.optimize_during_display ? 0 : 30000,
  // Keep the POST alive to learn its ID; the owner sequence cancels a late
  // creation even if the page has already moved to another request.
  create: () => api.post('/route/recommendation/jobs', payload, { timeout: 120000 }).then(({ data }) => data),
  get: (id, revision, progressRevision, workflowRevision, signal) => api.get(`/route/recommendation/jobs/${id}`, {
    params: { since_revision: Math.max(0, revision), since_progress_revision: Math.max(0, progressRevision), since_workflow_revision: Math.max(0, workflowRevision) }, signal, timeout: 15000 }).then(({ data }) => data),
  cancel: id => api.delete(`/route/recommendation/jobs/${id}`, { timeout: 5000 }),
  onCreated: id => {
    progress.onCreated?.(id)
    progress.onFinishReady?.(() => api.post(`/route/recommendation/jobs/${id}/finish`, null, { timeout: 15000 }))
    return taskOwner?.attach(id, '/api/route/recommendation/jobs')
  },
})
export const recommendRoute = (payload,progress={})=>planningGate.run(owner=>pollOwnedRecommendation(
  {...payload,prefer_simple_route:true,route_quality_extra_seconds:10,client_instance_id:owner.client_instance_id,client_request_sequence:owner.client_request_sequence},
  {...progress,signal:owner.signal}),{signal:progress.signal,
    onWaiting:()=>progress.onStatus?.({status:'waiting_for_previous',message:'正在释放上次规划资源'})})
export const listRoutes = () => api.get('/routes').then(({ data }) => data)
export const saveRoute = (route) => api.post('/routes', route).then(({ data }) => data)
export const deleteRoute = (routeId) => api.delete(`/routes/${routeId}`)

export const listExportedRoutes = async () => {
  const { data } = await api.get('/exported-routes')
  return data
}

export const getExportedRouteUrl = (fileName) => {
  return `${api.defaults.baseURL}/exported-routes/${encodeURIComponent(fileName)}`
}

export const getExportedWaypointUrl = (fileName) => {
  return `${api.defaults.baseURL}/exported-waypoints/${encodeURIComponent(fileName)}`
}

export const parseWaypointText = (content) =>
  api.post('/waypoints/parse', content, {
    headers: { 'Content-Type': 'text/plain' },
  }).then(({ data }) => data)

export const deleteExportedRoute = async (fileName) => {
  await api.delete(`/exported-routes/${encodeURIComponent(fileName)}`)
}

export const renameExportedRouteFile = async (fileName, newFileName) => {
  const { data } = await api.put(`/exported-routes/${encodeURIComponent(fileName)}/rename`, { file_name: newFileName })
  return data
}

export const renameExportedRouteName = async (fileName, routeName) => {
  const { data } = await api.put(`/exported-routes/${encodeURIComponent(fileName)}/rename`, { route_name: routeName })
  return data
}

export const renameExportedRoute = renameExportedRouteFile
