import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 15000 })
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
export const analyzeRoute = (selection, points, thresholds) =>
  api.post('/route/analyze', {
    points,
    ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
    source: selection.source,
    level: selection.level,
    thresholds,
  }).then(({ data }) => data)
export const planRoute = (selection, start, end, thresholds) =>
  api.post('/route/plan', {
    start, end,
    ...(selection.validTime ? { valid_time: selection.validTime } : { cycle: selection.cycle, forecast_hour: selection.forecastHour }),
    source: selection.source, level: selection.level, thresholds,
  }).then(({ data }) => data)
export const listRoutes = () => api.get('/routes').then(({ data }) => data)
export const saveRoute = (route) => api.post('/routes', route).then(({ data }) => data)
export const deleteRoute = (routeId) => api.delete(`/routes/${routeId}`)
