import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 15000 })
const ROUTE_PLAN_TIMEOUT_MS = 120000
export const CHINA_BBOX = [73, 18, 135, 54]

const selectionParams = (selection, bbox) => ({
  source: selection.source,
  ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
  level: selection.level,
  ...(bbox ? { bbox: bbox.join(',') } : {}),
})

const containsPoint = (bbox, lon, lat) => bbox && lon >= bbox[0] && lon <= bbox[2] && lat >= bbox[1] && lat <= bbox[3]

export const getTimes = (source = 'gfs') => api.get('/times', { params: { source } }).then(({ data }) => data)
export const getGfsDownloadStatus = () => api.get('/gfs/download-status').then(({ data }) => data)
export const getWind = (selection, bbox = CHINA_BBOX) => api.get('/wind', { params: selectionParams(selection, bbox) }).then(({ data }) => data)
export const getHeatmap = (selection, bbox = CHINA_BBOX) => api.get('/heatmap', { params: selectionParams(selection, bbox) }).then(({ data }) => data)
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
export const planRoute = (selection, start, end, thresholds, plannerType = 'wa_lpa_star', aircraftModel = 'fixed_wing', planningStrategy = 'wind_avoidance', windShear) =>
  api.post('/route/plan', {
    start, end,
    ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
    source: selection.source,
    level: selection.level,
    thresholds,
    planner_type: plannerType,
    aircraft_model: aircraftModel,
    planning_strategy: planningStrategy,
    wind_shear: windShear,
  }, { timeout: ROUTE_PLAN_TIMEOUT_MS }).then(({ data }) => data)
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
