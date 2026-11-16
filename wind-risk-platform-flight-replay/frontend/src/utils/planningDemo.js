import { frameBracket, interpolateWindFrames } from './flightReplay.js'
import { assessRouteQuality, isDisplayableRoute } from './routeQuality.js'
import { acceptsRangeRevision } from './departureRange.js'

const frameMs = 600000
const radians = value => value * Math.PI / 180
const distance = (a, b) => 12742000 * Math.asin(Math.sqrt(Math.min(1,
  Math.sin(radians(b[1] - a[1]) / 2) ** 2 + Math.cos(radians(a[1])) * Math.cos(radians(b[1]))
  * Math.sin(radians(b[0] - a[0]) / 2) ** 2)))
const same = (a, b) => a[0] === b[0] && a[1] === b[1]
const near = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-9
const extendsPath = (prefix, points) => {
  if (!prefix.length || !points.length || !near(prefix[0], points[0])) return false
  let i = 1, j = 1, current = prefix[0]
  while (i < prefix.length) {
    if (j >= points.length) return false
    const a = prefix[i], b = points[j]
    if (near(current, a)) { i++; continue }
    if (near(current, b)) { j++; continue }
    const ax = a[0] - current[0], ay = a[1] - current[1], bx = b[0] - current[0], by = b[1] - current[1]
    const da = Math.hypot(ax, ay), db = Math.hypot(bx, by)
    if (ax * bx + ay * by < 0 || Math.abs(ax * by - ay * bx) > 1e-9 * (da + db)) return false
    if (da <= db + 1e-9) { current = a; i++ }
    if (db <= da + 1e-9) { current = b; j++ }
  }
  return true
}
const sameSegment = (a, b) => Math.abs(a.begin - b.begin) < .001 && Math.abs(a.end - b.end) < .001
  && extendsPath(a.points, b.points) && extendsPath(b.points, a.points)

// The first verified candidate can animate before the 30-second publication.
// Keep it stable until the normal stage/final callbacks select an improvement.
export const isPlanningDemoCandidate = result => {
  const profile = result?.flight_profile
  return Boolean(!result?.v3 && !result?.reference_route && !result?.wind_shear_fallback && !result?.endpoint_hard_block
    && result?.analysis?.navigation_allowed === true && result?.time_aware && result.points?.length >= 2
    && result.dataset_id && profile?.dataset_id === result.dataset_id && profile.level === result.level
    && profile.departure_at === result.departure_at && profile.analysis?.samples?.length >= 2 && isDisplayableRoute(result))
}

export const createEarlyPlanningDemo = ({ show, isCurrent, isPublished }) => {
  let started = false
  return result => {
    if (started || !isCurrent() || isPublished() || !isPlanningDemoCandidate(result)) return
    if (show(result) !== false) started = true
  }
}

// One verified route completes a planning run. Terminal duplicates, later
// progress, window refreshes and stale tasks cannot restart its replay.
export const createFirstFeasibleFlight = ({ isCurrent, stopPreview, publish, play }) => {
  let accepted = false
  return result => {
    if (accepted || !isCurrent() || !isPlanningDemoCandidate(result)) return false
    accepted = true
    stopPreview()
    publish(result)
    play()
    return true
  }
}

// Compare during the animation, then freeze the server's latest verified
// incumbent. A revision redraws only from its first changed ten-minute leg.
export const createAnimatedPlanningFlight = ({ isCurrent, show, stopPreview, publish, play, compareDepartureRange = false, onFinishError = () => {} }) => {
  let best, signature, identity, terminal = false, drawn = false, closed = false, finish, requested = false
  let resolveDone
  const done = new Promise(resolve => { resolveDone = resolve })
  const complete = () => {
    if (closed || !isCurrent() || !terminal || !drawn || !best) return
    closed = true
    stopPreview(); publish(best); play(); resolveDone(best)
  }
  const requestFinish = () => {
    if (compareDepartureRange) return
    if (requested || terminal || !finish || !drawn || !best || !isCurrent()) return
    requested = true
    void Promise.resolve().then(finish).catch(onFinishError)
  }
  const receive = result => {
    if (closed || !isCurrent() || !isPlanningDemoCandidate(result)) return false
    const key = JSON.stringify([result.dataset_id, compareDepartureRange ? result.departure_start_at : result.departure_at, result.level, result.average_ground_speed_mps])
    if (identity && identity !== key) return false
    if (compareDepartureRange ? !acceptsRangeRevision(best, result)
      : Number.isFinite(best?.objective_cost) && Number.isFinite(result.objective_cost) && result.objective_cost > best.objective_cost + 1e-9) return false
    identity = key; best = result
    if (compareDepartureRange) publish(result)
    const next = JSON.stringify([result.points, result.departure_at])
    if (next !== signature) {
      signature = next; drawn = false
      if (show(result, { replaceInvalid: true }) === false) { drawn = true; requestFinish() }
    }
    complete()
    return true
  }
  return {
    receive, done,
    invalidate() { if (!closed) { best=undefined; signature=undefined; drawn=false; stopPreview() } },
    get hasCandidate() { return Boolean(best) },
    registerFinish(callback) { finish = callback; requestFinish() },
    drawn() { drawn = true; requestFinish(); complete() },
    terminal(result) {
      if (result) receive(result)
      terminal = true
      if (!best) { closed = true; resolveDone(null) }
      else complete()
      return done
    },
    cancel({ retain = false } = {}) {
      if (closed) return
      closed = true
      stopPreview()
      if (retain && best && isCurrent()) publish(best)
      resolveDone(null)
    },
  }
}

// Search prefixes are display-only, never routes that may be saved. Keep one
// branch and append completed ten-minute legs; a different branch requires an
// explicit invalidation, not a change in candidate rank or poll revision.
export const createPlanningSearchPreview = ({ isCurrent, isPublished, show, start, source, level }) => {
  let prefix, identity
  return progress => {
    const context = progress?.preview_context
    if (!isCurrent() || isPublished() || !context || context.source !== source || context.level !== level
      || !context.dataset_id || !(context.average_ground_speed_mps > 0)) return
    const nextIdentity = JSON.stringify([context.dataset_id, context.departure_at, context.level, context.average_ground_speed_mps])
    if (identity && identity !== nextIdentity) return
    const invalidated = progress.preview_invalidated === true
    for (const candidate of progress.forward || []) {
      if (candidate.points?.length < 2 || !same(candidate.points[0], start)) continue
      const points = candidate.points.map(p => p.slice(0, 2))
      if (!assessRouteQuality(points,context.average_ground_speed_mps).acceptable) continue
      if (prefix && !invalidated && !extendsPath(prefix, points)) continue
      const duration = points.slice(1).reduce((total, point, i) => total + distance(points[i], point) / context.average_ground_speed_mps, 0)
      const result = { points, flight_profile: { ...context, flight_duration_seconds: duration, analysis: { samples: [] } } }
      let geometry
      try { geometry = planningDemoGeometry(result) } catch { continue }
      const complete = geometry.segments.filter(s => Math.abs((Date.parse(context.departure_at) + s.end * 1000) / frameMs
        - Math.round((Date.parse(context.departure_at) + s.end * 1000) / frameMs)) < 1e-7)
      if (!complete.length) continue
      const clipped = []
      for (const segment of complete) for (const point of segment.points) if (!clipped.length || !same(clipped.at(-1), point)) clipped.push(point)
      if (prefix && extendsPath(clipped, prefix) && extendsPath(prefix, clipped)) return
      if (show({ points: clipped, flight_profile: { ...result.flight_profile, flight_duration_seconds: complete.at(-1).end } },
        { replaceInvalid: invalidated }) === false) continue
      prefix = clipped; identity = nextIdentity
      return
    }
  }
}

// Split the verified spatial polyline, not a line joining ten-minute samples.
// Local segment arrays together contain only O(vertices + frames) coordinates.
export const planningDemoGeometry = result => {
  const profile = result.flight_profile
  const start = Date.parse(profile?.departure_at), duration = profile?.flight_duration_seconds
  const speed = profile?.average_ground_speed_mps ?? result.average_ground_speed_mps
  const points = result.points?.map(point => point.slice(0, 2))
  if (!Number.isFinite(start) || !(duration > 0) || !(speed > 0) || !points || points.length < 2
    || points.some(p => !p.every(Number.isFinite))) throw Error('完整航程资料不足，无法分段演示。')
  const elapsed = [0]
  for (let i = 1; i < points.length; i++) elapsed.push(elapsed.at(-1) + distance(points[i - 1], points[i]) / speed)
  if (Math.abs(elapsed.at(-1) - duration) > Math.max(.01, duration * 1e-6)) throw Error('航线几何与航程时间不一致，未启动分段演示。')
  elapsed[elapsed.length - 1] = duration
  const locate = seconds => {
    let lo = 0, hi = elapsed.length - 1
    while (lo < hi) { const mid = (lo + hi) >> 1; if (elapsed[mid] < seconds) lo = mid + 1; else hi = mid }
    if (!lo || elapsed[lo] === seconds) return points[lo]
    const a = points[lo - 1], b = points[lo], fraction = (seconds - elapsed[lo - 1]) / (elapsed[lo] - elapsed[lo - 1])
    return [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction]
  }
  const cuts = [0]
  for (let stamp = (Math.floor(start / frameMs) + 1) * frameMs; stamp < start + duration * 1000 - .001; stamp += frameMs) cuts.push((stamp - start) / 1000)
  cuts.push(duration)
  let vertex = 1
  const segments = cuts.slice(1).map((end, index) => {
    const begin = cuts[index], geometry = [locate(begin)]
    while (vertex < points.length && elapsed[vertex] < end) {
      if (elapsed[vertex] > begin && !same(geometry.at(-1), points[vertex])) geometry.push(points[vertex])
      vertex++
    }
    const last = locate(end)
    if (!same(geometry.at(-1), last)) geometry.push(last)
    const at = new Date(Math.floor((start + begin * 1000) / frameMs) * frameMs).toISOString()
    return { begin, end, at, points: geometry }
  })
  return { points, segments, profile, identity: JSON.stringify([profile.dataset_id, profile.departure_at, profile.level, speed, points]) }
}

const initial = () => ({ active: false, completed: false, loading: false, points: [], index: -1,
  profile: null, seconds: 0, error: '', mapWind: null, mapHeatmap: null, mapTime: null, mapKey: null,
  pendingMapWind: null, pendingMapHeatmap: null, pendingMapKey: null })

// No second weather LRU: requests and two-frame lookahead share the page cache.
export const createPlanningDemoController = ({ requestFrame, requestFrames, onChange,
  onError = () => {}, onComplete, setTimer = setTimeout, clearTimer = clearTimeout, holdMs = 2000, requireMapReady = true }) => {
  let state = initial(), context, controller, timer, pending, prefetch, queued, generation = 0, sequence = 0
  let disposed = false, dismissed = false
  const waiting = new Map()
  const emit = changes => { state = { ...state, ...changes }; if (!disposed) onChange(state) }
  const abort = () => {
    generation++; clearTimer(timer); timer = undefined
    controller?.abort(); prefetch?.controller.abort(); prefetch = undefined; waiting.clear(); pending = undefined; queued = undefined
  }
  const prefix = index => {
    const result = []
    for (const segment of context.segments.slice(0, index + 1)) {
      for (const p of segment.points) if (!result.length || !same(result.at(-1), p)) result.push(p)
    }
    return result
  }
  const prepareNext = index => {
    if (!requestFrames || prefetch || !controller || controller.signal.aborted) return
    const { profile, segments } = context
    const times = [...new Set(segments.slice(index + 1, index + 3).flatMap(segment =>
      frameBracket(profile.frame_times, segment.at, profile.wind_time_model) || []))].slice(0, 2)
    if (!times.length) return
    const task = { controller: new AbortController() }, current = generation
    prefetch = task
    const batch = requestFrames(profile.dataset_id, times, task.controller.signal)
    for (const [i, at] of times.entries()) {
      const promise = batch.then(values => values[i])
      waiting.set(at, promise)
      const clean = () => { if (waiting.get(at) === promise) waiting.delete(at) }
      void promise.then(clean, clean)
    }
    void batch.catch(() => {}).finally(() => { if (generation === current && prefetch === task) prefetch = undefined })
  }
  const frame = async at => {
    const signal = controller.signal, profile = context.profile
    if (waiting.has(at)) {
      try { return await waiting.get(at) } catch (error) { if (signal.aborted) throw error }
    }
    return requestFrame(profile.dataset_id, at, signal)
  }
  const commit = () => {
    if (!pending || disposed) return
    const { index, key, data } = pending
    pending = undefined
    const complete = index === context.segments.length - 1
    emit({ index, points: prefix(index), seconds: context.segments[index].end, completed: complete, loading: false,
      mapWind: data.wind, mapHeatmap: data.heatmap, mapTime: context.segments[index].at, mapKey: key,
      pendingMapWind: null, pendingMapHeatmap: null, pendingMapKey: null })
    if (!complete || onComplete) timer = setTimer(() => {
      timer = undefined
      if (queued) { const next = queued; queued = undefined; apply(next, true); return }
      if (state.index + 1 < context.segments.length) void advance(state.index + 1)
      else onComplete?.()
    }, holdMs)
  }
  const advance = async index => {
    if (disposed || dismissed || !context || index >= context.segments.length) return
    const current = generation, profile = context.profile, segment = context.segments[index]
    const key = `planning-demo:${current}:${++sequence}:${segment.at}`
    emit({ loading: true, error: '', pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null })
    try {
      const pair = frameBracket(profile.frame_times, segment.at, profile.wind_time_model)
      if (!pair) throw Error('该航段的风场数据不足。')
      const frames = await Promise.all([...new Set(pair)].map(frame))
      if (disposed || generation !== current || dismissed) return
      const data = interpolateWindFrames(frames[0], frames[1] || frames[0], segment.at)
      if (!data || data.wind.metadata.dataset_id !== profile.dataset_id || data.wind.metadata.level !== profile.level) throw Error('该航段的风场数据不足或版本不一致。')
      pending = { index, key, data }
      emit({ pendingMapKey: key, pendingMapWind: data.wind, pendingMapHeatmap: data.heatmap })
      prepareNext(index)
      if (!requireMapReady) commit()
    } catch (error) {
      if (disposed || generation !== current || dismissed) return
      pending = undefined
      emit({ loading: false, error: '分段风场加载失败，可重试或查看完整航线。', pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null })
      onError(error)
    }
  }
  const apply = (next, atBoundary = false) => {
      let common = 0
      if (context && context.profile.dataset_id === next.profile.dataset_id && context.profile.departure_at === next.profile.departure_at) {
        while (common < Math.min(context.segments.length, next.segments.length)
          && sameSegment(context.segments[common], next.segments[common])) common++
      }
      if (context && common === context.segments.length && next.segments.length > common) {
        const wasComplete = state.completed
        context = next
        emit({ profile: next.profile, completed: false })
        if ((wasComplete || atBoundary) && !pending && !timer) void advance(state.index + 1)
        return true
      }
      const target = Math.min(Math.max(0, state.index + 1), common, next.segments.length - 1)
      abort(); context = next; controller = new AbortController()
      emit({ active: true, completed: false, profile: next.profile, loading: true, error: '',
        pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null })
      void advance(target)
      return true
  }
  return {
    show(result, { replaceInvalid = false } = {}) {
      if (disposed || dismissed) return false
      let next
      try { next = planningDemoGeometry(result) } catch (error) { this.dismiss(); onError(error); return false }
      if (next.identity === context?.identity) { context = next; emit({ profile: next.profile }); return true }
      if (context && !replaceInvalid && !extendsPath(context.points, next.points)) return false
      if (context && replaceInvalid && (timer || pending || state.loading)) { queued = next; return true }
      return apply(next)
    },
    acknowledgeMap(key) { if (key && key === pending?.key && !dismissed) commit() },
    retry() { if (state.active && state.error) void advance(Math.min(state.index + 1, context.segments.length - 1)) },
    dismiss() { abort(); dismissed = true; context = undefined; emit(initial()) },
    reset() { this.dismiss(); dismissed = false },
    dispose() { this.dismiss(); disposed = true },
  }
}
