import { createWindRequestCache } from './windRequestCache.js'

// Native WRF holds real ten-minute frames; legacy/GFS interpolate U/V only.
export const PARTICLE_MAP_UPDATE_INTERVAL_SECONDS = 30 * 60
export const NATIVE_WRF_TIME_MODEL = 'NATIVE_WRF_TEN_MINUTE_HOLD_V1'

export const formatReplayTime = (value) => {
  if (!value || !Number.isFinite(Date.parse(value))) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(new Date(value))
}

export const toBeijingInput = (value) => Number.isFinite(Date.parse(value))
  ? new Date(Date.parse(value) + 8 * 3600000).toISOString().slice(0, 16) : ''
export const fromBeijingInput = (value) => {
  const stamp = Date.parse(`${value}+08:00`)
  return Number.isFinite(stamp) ? new Date(stamp).toISOString() : null
}

export const frameBracket = (times, at, model) => {
  const stamp = Date.parse(at)
  if(model===NATIVE_WRF_TIME_MODEL) {
    if(!Number.isFinite(stamp) || stamp<Date.parse(times[0]) || stamp>Date.parse(times.at(-1))) return null
    const frame=times.find(value=>Date.parse(value)===Math.floor(stamp/600000)*600000)
    return frame ? [frame,frame] : null
  }
  const index = times.findIndex((value) => Date.parse(value) >= stamp)
  if (index < 0 || !Number.isFinite(stamp)) return null
  if (Date.parse(times[index]) === stamp) return [times[index], times[index]]
  if (index === 0 || Date.parse(times[index]) - Date.parse(times[index - 1]) !== 3600000) return null
  return [times[index - 1], times[index]]
}

export const interpolateWindFrames = (left, right, at) => {
  if (!left?.available || !right?.available || left.metadata.dataset_id !== right.metadata.dataset_id
    || left.metadata.level !== right.metadata.level || left.metadata.cycle !== right.metadata.cycle) return null
  const start = Date.parse(left.metadata.valid_time)
  const end = Date.parse(right.metadata.valid_time)
  const requested = Date.parse(at)
  const step = Number(left.metadata.wind_time_step_seconds || 0) * 1000
  if (step !== Number(right.metadata.wind_time_step_seconds || 0) * 1000) return null
  const stamp = step > 0 ? Math.floor(requested / step) * step : requested
  const native=left.metadata.wind_time_model===NATIVE_WRF_TIME_MODEL
  if(left.metadata.wind_time_model!==right.metadata.wind_time_model) return null
  if(native && (start!==end || start!==stamp || step!==600000)) return null
  if (![start, end, stamp, requested].every(Number.isFinite) || requested < start || (!native && requested > end)
    || (native && (requested>=start+step || left.metadata.coverage_end && requested>Date.parse(left.metadata.coverage_end)))
    || stamp < start || stamp > end
    || (end !== start && end - start !== 3600000)) return null
  const alpha = start === end ? 0 : (stamp - start) / (end - start)
  const header = left.velocity?.[0]?.header
  const other = right.velocity?.[0]?.header
  if (!header || !other || ['nx', 'ny', 'lo1', 'la1', 'dx', 'dy'].some((key) => header[key] !== other[key])) return null
  const count = header.nx * header.ny
  const a = left.velocity.map((value) => value.data)
  const b = right.velocity.map((value) => value.data)
  if (a.length !== 2 || b.length !== 2 || [...a, ...b].some((data) => data.length !== count)) return null
  const u = [], v = [], speed = []
  let minimum = Infinity, maximum = -Infinity
  for (let i = 0; i < count; i += 1) {
    const required = alpha === 0 ? [a[0][i], a[1][i]] : alpha === 1 ? [b[0][i], b[1][i]]
      : [a[0][i], a[1][i], b[0][i], b[1][i]]
    if (!required.every(Number.isFinite)) {
      u.push(null); v.push(null); speed.push(null)
      continue
    }
    u.push(alpha === 0 ? a[0][i] : alpha === 1 ? b[0][i] : a[0][i] + alpha * (b[0][i] - a[0][i]))
    v.push(alpha === 0 ? a[1][i] : alpha === 1 ? b[1][i] : a[1][i] + alpha * (b[1][i] - a[1][i]))
    const magnitude = Math.hypot(u[i], v[i])
    speed.push(magnitude)
    minimum = Math.min(minimum, magnitude)
    maximum = Math.max(maximum, magnitude)
  }
  const displayedAt = step > 0 ? new Date(stamp).toISOString() : at
  const metadata = { ...left.metadata, valid_time: displayedAt, requested_time: at, valid_time_bj: undefined,
    forecast_hour: left.metadata.forecast_hour + (stamp - start) / 3600000,
    forecast_minutes: native ? left.metadata.forecast_minutes : undefined }
  const velocity = [u, v].map((data, index) => ({
    header: { ...left.velocity[index].header, refTime: displayedAt }, data,
  }))
  return {
    wind: { metadata, velocity },
    heatmap: { metadata, wind_speed: { header, data: speed,
      min: Number.isFinite(minimum) ? minimum : null, max: Number.isFinite(maximum) ? maximum : null } },
  }
}

export const flightPosition = (profile, seconds) => {
  const samples = profile?.analysis?.samples
  if (!samples?.length) return null
  const offset = Math.max(0, Math.min(profile.flight_duration_seconds, seconds))
  let low = 0, high = samples.length - 1
  while (low < high) {
    const middle = Math.floor((low + high) / 2)
    if (samples[middle].elapsed_seconds < offset) low = middle + 1
    else high = middle
  }
  const end = samples[low], start = samples[Math.max(0, low - 1)]
  const span = end.elapsed_seconds - start.elapsed_seconds
  const fraction = span ? (offset - start.elapsed_seconds) / span : 0
  return { lon: start.lon + (end.lon - start.lon) * fraction,
    lat: start.lat + (end.lat - start.lat) * fraction, flight_heading: offset === end.elapsed_seconds ? end.flight_heading : start.flight_heading,
    elapsed_seconds: offset, sampleIndex: Math.abs(start.elapsed_seconds - offset) < Math.abs(end.elapsed_seconds - offset) ? Math.max(0, low - 1) : low }
}

export const defaultReplayDeparture = (route, windows) => {
  if (route.departure_at) return route.departure_at
  // Saved fixed paths have no explicit departure either. Reloading a drawing
  // uses the same default without relying on its editable name or a DB flag.
  if ((route.fromDrawing || !route.ephemeral && !route.v3) && windows) {
    const evaluated = Date.parse(windows.evaluated_at)
    const earliest = (windows.departure_windows || []).filter(row => Number.isFinite(Date.parse(row.start))
      && Date.parse(row.end) >= Date.parse(row.start) && Date.parse(row.start) >= evaluated)
      .sort((a,b) => Date.parse(a.start)-Date.parse(b.start))[0]
    if (earliest) return earliest.start
  }
  return windows?.evaluated_at
}

export const createFlightReplayController = ({ requestProfile, requestFrame, requestFrames, onChange,
  setTimer = setTimeout, clearTimer = clearTimeout, mapUpdateIntervalSeconds = 0,
  prefetchFrames = 2, frameCacheBytes = 16 * 1024 * 1024, requireMapReady = false }) => {
  let mapInterval = Number.isFinite(mapUpdateIntervalSeconds) && mapUpdateIntervalSeconds > 0 ? mapUpdateIntervalSeconds : 0
  const initial = () => ({ active: false, mode: 'flight', loading: false, error: '', notice: '',
    result: null, departureAt: null, seconds: 0, playing: false, rate: 60,
    mapWind: null, mapHeatmap: null, mapTime: null, mapLoading: false, mapError: '', mapUpdateIntervalSeconds: mapInterval,
    pendingMapKey: null, pendingMapTime: null, pendingMapWind: null, pendingMapHeatmap: null, mapKey: null })
  let state = initial(), context, signature, generation = 0, seekGeneration = 0
  let controller, mapController, prefetchController, requestedMapTime, timer, disposed = false
  let mapFlight, queuedSeconds
  const prefetched = new Map()
  const frames = createWindRequestCache({ maxBytes: frameCacheBytes, maxEntries: 8 })
  const emit = (changes) => { state = { ...state, ...changes }; if (!disposed) onChange(state) }
  const pause = () => { clearTimer(timer); timer = undefined; emit({ playing: false }) }
  const abort = () => {
    generation += 1; seekGeneration += 1
    controller?.abort(); mapController?.abort(); prefetchController?.abort()
    mapFlight = undefined; queuedSeconds = undefined; prefetched.clear()
    requestedMapTime = undefined
    pause()
  }
  const frame = (time, datasetId, signal, prefetch = false) => {
    const key = `${datasetId}|${time}`
    return frames.get(key, async (requestSignal) => {
      const pending = prefetched.get(key)
      if (pending) {
        try { return await pending } catch (error) { if (requestSignal.aborted) throw error }
      }
      return requestFrame(datasetId, time, requestSignal, { prefetch })
    },
      { signal, priority: prefetch ? 0 : 1 })
  }
  const prefetchNext = (seconds, profile) => {
    const step = Number(profile.wind_time_step_seconds || 0)
    if (!step || !controller || controller.signal.aborted) return
    prefetchController?.abort()
    prefetchController = new AbortController()
    const interval = mapInterval || step
    const wanted = new Set()
    for (let i = 1; i <= Math.min(2, Math.max(0, prefetchFrames)); i++) {
      const futureSeconds = (Math.floor(seconds / interval) + i) * interval
      if (futureSeconds > profile.flight_duration_seconds) break
      const stamp = Date.parse(profile.departure_at) + futureSeconds * 1000
      if (stamp > Date.parse(profile.frame_times.at(-1))) break
      const at = new Date(Math.floor(stamp / (step * 1000)) * step * 1000).toISOString()
      const pair = frameBracket(profile.frame_times, at, profile.wind_time_model)
      for (const time of new Set(pair || [])) {
        // A speculative failure is reported only if the user actually needs
        // that frame. Never fill a missing native frame or change its time.
        if (!frames.peek(`${profile.dataset_id}|${time}`)) wanted.add(time)
      }
    }
    const times = [...wanted].slice(0, 2)
    if (requestFrames && times.length) {
      const signal = prefetchController.signal, current = generation
      const batch = requestFrames(profile.dataset_id, times, signal)
      times.forEach((time, index) => {
        const key = `${profile.dataset_id}|${time}`
        const pending = batch.then((values) => {
          const value = values[index]
          if (!value || value.metadata?.dataset_id !== profile.dataset_id) throw Error('预加载风场版本不一致')
          if (!signal.aborted && generation === current) frames.prime(key, value)
          return value
        })
        prefetched.set(key, pending)
        const clear = () => { if (prefetched.get(key) === pending) prefetched.delete(key) }
        void pending.then(clear, clear)
      })
    } else {
      times.forEach((time) => { void frame(time, profile.dataset_id, prefetchController.signal, true).catch(() => {}) })
    }
  }
  const drainLatest = () => {
    if (queuedSeconds === undefined || mapFlight || state.pendingMapKey || disposed) return
    const next = queuedSeconds
    queuedSeconds = undefined
    void seek(next, true, { automatic: true })
  }
  const seek = async (value, keepPlaying = false, { automatic = false } = {}) => {
    if (!state.result || disposed) return
    if (!keepPlaying) pause()
    const profile = state.result
    const seconds = Math.max(0, Math.min(profile.flight_duration_seconds, Number(value)))
    if (!Number.isFinite(seconds)) return
    if (automatic && (mapFlight || state.pendingMapKey)) {
      queuedSeconds = seconds
      emit({ seconds })
      return mapFlight?.promise
    }
    if (!automatic) queuedSeconds = undefined
    const current = ++seekGeneration, currentGeneration = generation
    // Only the background is held between flight-time boundaries. Preserve its
    // object identity so Leaflet does not rebuild the particle field each tick.
    const mapSeconds = mapInterval ? Math.floor(seconds / mapInterval) * mapInterval : seconds
    const requestedStamp = Date.parse(profile.departure_at) + mapSeconds * 1000
    const step = Number(profile.wind_time_step_seconds || 0) * 1000
    const covered = requestedStamp >= Date.parse(profile.frame_times[0])
      && requestedStamp <= Date.parse(profile.frame_times.at(-1))
    const at = new Date(step > 0 && covered ? Math.floor(requestedStamp / step) * step : requestedStamp).toISOString()
    if (state.mapTime === at && !state.mapLoading) { emit({ seconds }); return }
    if (requestedMapTime !== at || mapController?.signal.aborted) {
      mapController?.abort()
      mapController = new AbortController()
      requestedMapTime = at
    }
    const mapSignal = mapController.signal
    const key = `${generation}:${current}:${at}`
    const flight = { at }
    mapFlight = flight
    emit({ seconds, mapLoading: true, mapError: '', pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null })
    flight.promise = (async () => {
      try {
        const pair = frameBracket(profile.frame_times, at, profile.wind_time_model)
        let data = null
        if (pair) {
          const [left, right] = await Promise.all(pair.map((time) => frame(time, profile.dataset_id, mapSignal)))
          if (disposed || current !== seekGeneration || generation !== currentGeneration) return
          data = interpolateWindFrames(left, right, at)
        }
        if (disposed || current !== seekGeneration || generation !== currentGeneration) return
        if (data && requireMapReady) {
          emit({ pendingMapKey: key, pendingMapTime: at, pendingMapWind: data.wind, pendingMapHeatmap: data.heatmap })
        } else {
          emit({ mapTime: at, mapKey: key, mapLoading: false, mapWind: data?.wind || null, mapHeatmap: data?.heatmap || null,
            mapError: data ? '' : '该播放时刻风场数据不足，底图风场暂不显示。' })
        }
        if (data) prefetchNext(seconds, profile)
      } catch (reason) {
        if (disposed || current !== seekGeneration || generation !== currentGeneration) return
        pause()
        emit({ mapTime: at, mapLoading: false, mapWind: null, mapHeatmap: null,
          pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null,
          mapError: reason.response?.data?.detail || '回放风场加载失败，请重试。' })
      } finally {
        if (mapFlight === flight) { mapFlight = undefined; drainLatest() }
      }
    })()
    return flight.promise
  }
  const evaluate = async () => {
    if (!context?.route || !context?.windows || !state.departureAt) return
    abort()
    controller = new AbortController()
    const current = generation
    emit({ loading: true, error: '', result: null, seconds: 0, mapTime: null, mapWind: null, mapHeatmap: null, mapError: '',
      pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null, mapKey: null })
    try {
      const prepared = context.route.initial_profile
      const canAdopt = prepared && prepared.dataset_id === context.windows.dataset_id
        && prepared.departure_at === state.departureAt
        && context.route.profile_signature === JSON.stringify(context.configuration)
      const result = canAdopt ? prepared : await requestProfile(context.route.ephemeral || context.route.v3 ? context.route : context.route.route_id,
        { ...context.configuration, dataset_id: context.windows.dataset_id, departure_at: state.departureAt }, controller.signal)
      if (disposed || generation !== current) return
      emit({ result, loading: false, departureAt: result.departure_at })
      await seek(0)
    } catch (reason) {
      if (disposed || generation !== current) return
      emit({ loading: false, error: reason.response?.data?.detail || '飞行过程评估失败，请重新选择起飞时间。' })
    }
  }
  const tick = async () => {
    if (!state.playing || !state.result || disposed) return
    const current = generation
    const update = seek(state.seconds + state.rate / 2, true, { automatic: true })
    if (!state.playing || disposed || generation !== current) return
    if (state.seconds >= state.result.flight_duration_seconds) pause()
    else timer = setTimer(tick, 500)
    // Install the next aircraft tick immediately. A cold network frame must
    // not stretch a 500ms flight step into several seconds; mapTime continues
    // to identify the actual displayed background while mapLoading is true.
    return update
  }
  return {
    async setContext(route, configuration, windows) {
      if (!route) {
        abort(); context = undefined; signature = undefined; frames.invalidate(); emit(initial()); return
      }
      // A V3 first solution already has its independently evaluated profile.
      // Display it while optimization/window certification runs separately.
      if (!windows && route.initial_profile && (route.v3 || route.ephemeral && !route.fromDrawing
        && route.profile_signature === JSON.stringify(configuration))) windows = {
        dataset_id: route.initial_profile.dataset_id, evaluated_at: route.initial_profile.departure_at,
      }
      const baseSignature = (item, config) => JSON.stringify([item?.planning_key || item?.route_id, item?.points, config])
      // A periodic "depart now" refresh temporarily has no window response.
      // It must not clear an otherwise unchanged selected-flight replay.
      if (!windows && context?.windows
        && baseSignature(route, configuration) === baseSignature(context.route, context.configuration)) return
      const nextSignature = JSON.stringify([route.planning_key || route.route_id, route.points, configuration, windows?.dataset_id])
      if (nextSignature === signature) return
      const changedRoute = route.planning_key && context?.route?.planning_key
        ? route.planning_key !== context.route.planning_key : route.route_id !== context?.route?.route_id
      const changedDataset = context?.windows?.dataset_id && windows?.dataset_id && context.windows.dataset_id !== windows.dataset_id
      signature = nextSignature
      abort()
      if (changedDataset || changedRoute) frames.invalidate()
      context = { route, configuration, windows }
      emit({ active: true, result: null, loading: true, error: '', seconds: 0,
        mapWind: null, mapHeatmap: null, mapTime: null, mapLoading: false,
        pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null, mapKey: null,
        departureAt: changedRoute || !state.departureAt ? defaultReplayDeparture(route, windows) : state.departureAt,
        mode: changedRoute ? 'flight' : state.mode,
        notice: changedDataset ? '预报已更新，已暂停回放并重新评估。' : state.notice })
      if (windows) {
        if (!state.departureAt) emit({ departureAt: defaultReplayDeparture(route, windows) })
        await evaluate()
      }
    },
    chooseDeparture(value) {
      if (!Number.isFinite(Date.parse(value))) return
      emit({ departureAt: value, mode: 'flight', notice: '' })
      return evaluate()
    },
    chooseWindow(window) { return this.chooseDeparture(window.start) },
    setMapUpdateInterval(value) {
      const interval = Number(value)
      if (!Number.isFinite(interval) || interval < 0 || interval === mapInterval) return
      mapInterval = interval
      emit({ mapUpdateIntervalSeconds: interval })
      return seek(state.seconds, true)
    },
    setMode(mode) { pause(); emit({ mode: mode === 'snapshot' ? 'snapshot' : 'flight' }) },
    setRate(rate) { if (Number.isFinite(Number(rate)) && Number(rate) > 0) emit({ rate: Number(rate) }) },
    async play() {
      if (!state.result || state.loading || state.error) return
      pause()
      const current = generation
      if (state.seconds >= state.result.flight_duration_seconds) await seek(0)
      if (disposed || current !== generation || !state.result) return
      emit({ mode: 'flight', playing: true })
      timer = setTimer(tick, 500)
    },
    pause, seek,
    acknowledgeMap(key) {
      if (!key || key !== state.pendingMapKey || disposed) return
      emit({ mapWind: state.pendingMapWind, mapHeatmap: state.pendingMapHeatmap, mapTime: state.pendingMapTime,
        mapKey: key, pendingMapKey: null, pendingMapWind: null, pendingMapHeatmap: null, pendingMapTime: null, mapLoading: false })
      drainLatest()
    },
    setError(error) { abort(); emit({ loading: false, result: null, error, mapWind: null, mapHeatmap: null, mapTime: null }) },
    retry: evaluate,
    cacheStats: frames.stats,
    dispose() { abort(); disposed = true; context = undefined; frames.invalidate() },
  }
}
