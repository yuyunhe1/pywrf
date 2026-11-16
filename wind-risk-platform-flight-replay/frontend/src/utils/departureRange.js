import { fromBeijingInput } from './flightReplay.js'

export const RANGE_SELECTION_MODEL = 'PEAK_WIND_BAND_DISTANCE_V1'
export function departureRangeBounds(settings, now = Date.now()) {
  const from = fromBeijingInput(settings.departureStartAt), to = fromBeijingInput(settings.departureEndAt)
  if (!from || !to) throw Error('请填写有效的起飞时间段开始和结束时间。')
  if (Date.parse(to) < Date.parse(from)) throw Error('起飞时间段结束时间不能早于开始时间。')
  if (Date.parse(to) < now) throw Error('起飞时间段已过，请选择未来时间。')
  return { departure_start_at:from, departure_end_at:to }
}

export function departureRangeRequest(settings, start, end, now = Date.now()) {
  const bounds = departureRangeBounds(settings, now)
  const middle = settings.requiredWaypoints?.length > 2 ? settings.requiredWaypoints.slice(1, -1) : []
  const points = [start, ...middle, end].map(p => p.slice(0, 2))
  if (points.some(([lon, lat]) => !Number.isFinite(lon) || !Number.isFinite(lat) || Math.abs(lon) > 180 || Math.abs(lat) >= 90)) {
    throw Error('必经航点经纬度无效。')
  }
  if (points.some((p, i) => i && p[0] === points[i-1][0] && p[1] === points[i-1][1])) throw Error('相邻必经航点必须不同。')
  return { mode: 'departure_range', ...bounds,
    required_waypoints: points, selection_model: RANGE_SELECTION_MODEL }
}

export function recommendedRangeRequest(settings, now = Date.now()) {
  if (!settings.enabled) throw Error('请先启用起飞时间段限制。')
  const points = settings.requiredWaypoints
  if (!points || points.length < 2) throw Error('请先自定义绘制至少两个不同的航点。')
  return departureRangeRequest(settings, points[0], points.at(-1), now)
}

// A drawing must leave edit mode before the shared planning pipeline starts.
export async function completeRecommendationDrawing({ complete, isDrawing, purpose, recommend }) {
  const payload = await complete()
  if (payload && !isDrawing() && purpose === 'departure_range') await recommend()
  return payload
}

export const acceptsRangeRevision = (previous, next) => next?.mode === 'departure_range'
  && (!previous || Number(next.selection_revision) > Number(previous.selection_revision)
    || Number(next.selection_revision) === Number(previous.selection_revision)
    && next.departure_at === previous.departure_at && JSON.stringify(next.points) === JSON.stringify(previous.points))
