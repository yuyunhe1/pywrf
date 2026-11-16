import test from 'node:test'
import assert from 'node:assert/strict'
import { createFlightReplayController, defaultReplayDeparture, flightPosition, frameBracket, fromBeijingInput,
  interpolateWindFrames, toBeijingInput, PARTICLE_MAP_UPDATE_INTERVAL_SECONDS, NATIVE_WRF_TIME_MODEL } from './flightReplay.js'

const base = '2026-11-01T00:00:00.000Z'
const hour = (h) => new Date(Date.parse(base) + h * 3600000).toISOString()
const windFrame = (h, u = 2, v = 0, id = 'one') => ({ available: true,
  metadata: { dataset_id: id, valid_time: hour(h), cycle: base, level: '100m AGL', forecast_hour: h },
  velocity: [u, v].map((value) => ({ header: { nx: 2, ny: 2, lo1: 0, la1: 1, dx: 1, dy: 1 },
    data: Array(4).fill(value) })),
})
const route = { route_id: 'fixed', points: [[0, 0], [.1, 0]], level: '100m AGL' }
const configuration = { average_ground_speed_mps: 10, thresholds: { danger: 7.9 } }
const windows = { dataset_id: 'one', evaluated_at: base }
const result = (payload = {}) => ({ dataset_id: payload.dataset_id || 'one', departure_at: payload.departure_at || base,
  flight_duration_seconds: 600, frame_times: [hour(0), hour(1), hour(2)],
  analysis: { samples: [
    { lon: 0, lat: 0, elapsed_seconds: 0, flight_heading: 90 },
    { lon: .05, lat: 0, elapsed_seconds: 300, flight_heading: 90 },
    { lon: .05, lat: .05, elapsed_seconds: 600, flight_heading: 0 },
  ] },
})

test('Beijing departure inputs have the same UTC meaning in any browser timezone', () => {
  assert.equal(toBeijingInput(base), '2026-11-01T08:00')
  assert.equal(toBeijingInput('2026-11-05T11:13:23.456Z'), '2026-11-05T19:13')
  assert.equal(fromBeijingInput('2026-11-05T19:13'), '2026-11-05T11:13:00.000Z')
  assert.equal(fromBeijingInput('2026-11-01T08:00:00'), base)
  assert.equal(fromBeijingInput(''), null)
  assert.equal(toBeijingInput('bad'), '')
})

test('map interpolates opposing u/v to calm wind instead of interpolating speed', () => {
  const data = interpolateWindFrames(windFrame(0, 8), windFrame(1, -8), hour(.5))
  assert.deepEqual(data.wind.velocity[0].data, [0, 0, 0, 0])
  assert.deepEqual(data.heatmap.wind_speed.data, [0, 0, 0, 0])
  assert.equal(data.wind.metadata.valid_time, hour(.5))
  assert.equal(data.wind.metadata.forecast_hour, .5)
})

test('missing vectors remain missing in BOTH wind components and heatmap', () => {
  const a = windFrame(0), b = windFrame(1)
  b.velocity[1].data[2] = null
  const data = interpolateWindFrames(a, b, hour(.5))
  assert.equal(data.wind.velocity[0].data[2], null)
  assert.equal(data.wind.velocity[1].data[2], null)
  assert.equal(data.heatmap.wind_speed.data[2], null)
})

test('map refuses extrapolation, skipped hours, mixed revisions and mixed heights', () => {
  assert.equal(interpolateWindFrames(windFrame(0), windFrame(1), hour(2)), null)
  assert.equal(interpolateWindFrames(windFrame(0), windFrame(2), hour(.5)), null)
  assert.equal(interpolateWindFrames(windFrame(0), windFrame(1, 2, 0, 'new'), hour(.5)), null)
  const other = windFrame(1)
  other.metadata.level = '300m AGL'
  assert.equal(interpolateWindFrames(windFrame(0), other, hour(.5)), null)
  assert.equal(frameBracket([hour(0), hour(2)], hour(1)), null)
  assert.equal(frameBracket([hour(0), hour(1)], hour(-1)), null)
  assert.deepEqual(frameBracket([hour(0), hour(2)], hour(2)), [hour(2), hour(2)])
})

test('aircraft follows route bends and clamps to endpoints', () => {
  assert.equal(flightPosition(result(), 450).lon, .05)
  assert.equal(flightPosition(result(), 450).lat, .025)
  assert.equal(flightPosition(result(), -100).lon, 0)
  assert.equal(flightPosition(result(), 900).lat, .05)
})

const setup = (overrides = {}) => {
  let state
  const profiles = [], frameRequests = [], timers = new Map()
  let timerId = 0
  const controller = createFlightReplayController({
    requestProfile: async (_id, payload) => { profiles.push(payload); return result(payload) },
    requestFrame: async (id, at) => { frameRequests.push([id, at]); return windFrame((Date.parse(at) - Date.parse(base)) / 3600000, 2, 0, id) },
    onChange: (next) => { state = next },
    setTimer: (callback) => { timers.set(++timerId, callback); return timerId },
    clearTimer: (id) => timers.delete(id), ...overrides,
  })
  return { controller, profiles, frameRequests, timers, state: () => state }
}
const flushWeather = async () => { for (let i = 0; i < 40; i++) await Promise.resolve() }

const longFlightResult = (payload) => {
  const profile = result(payload)
  return { ...profile, flight_duration_seconds: 7200,
    analysis: { samples: profile.analysis.samples.map((sample) => ({ ...sample, elapsed_seconds: sample.elapsed_seconds * 12 })) } }
}
const particleReplay = (overrides = {}) => setup({ mapUpdateIntervalSeconds: PARTICLE_MAP_UPDATE_INTERVAL_SECONDS,
  requestProfile: async (_id, payload) => longFlightResult(payload), ...overrides })

test('a drawn route defaults its profile and background to the earliest confirmed departure window', async () => {
  const app=particleReplay()
  const futureWindows={...windows,departure_windows:[{start:hour(1),end:hour(1.5)},{start:hour(.5),end:hour(.75)}]}
  await app.controller.setContext({...route,fromDrawing:true,planning_key:'drawing:1'},configuration)
  await app.controller.setContext({...route,fromDrawing:true,planning_key:'drawing:1'},configuration,futureWindows)
  assert.equal(app.state().result.departure_at,hour(.5))
  assert.equal(app.state().departureAt,hour(.5))
  assert.equal(app.state().mapTime,hour(.5))
  assert.equal(app.state().seconds,0)
  assert.equal(futureWindows.evaluated_at,base) // The independent "now" verdict stays intact.
  app.controller.dispose()
})

test('a drawn route with no confirmed window retains current weather rather than inventing a departure', async () => {
  const app=setup()
  await app.controller.setContext({...route,fromDrawing:true},configuration,{...windows,departure_windows:[],coverage_incomplete:true})
  assert.equal(app.state().departureAt,base)
  assert.equal(app.state().mapTime,base)
  assert.equal(defaultReplayDeparture({...route,fromDrawing:true},{...windows,departure_windows:[
    {start:hour(-1),end:hour(-.5)},{start:'invalid',end:hour(1)}]}),base)
  app.controller.dispose()
})

test('window refresh and normalized drawing identity preserve a manually selected departure', async () => {
  const app=setup()
  const drawn={...route,fromDrawing:true,planning_key:'drawing:2'}
  await app.controller.setContext(drawn,configuration,{...windows,departure_windows:[{start:hour(.5),end:hour(1)}]})
  await app.controller.chooseDeparture(hour(.75))
  await app.controller.setContext({...drawn,route_id:'normalized'},configuration,{...windows,
    departure_windows:[{start:hour(1),end:hour(1.5)}]})
  assert.equal(app.state().departureAt,hour(.75))
  assert.equal(app.state().mapTime,hour(.75))
  app.controller.dispose()
})

test('planned and explicitly adopted departures take precedence over drawing defaults', () => {
  const response={...windows,departure_windows:[{start:hour(.5),end:hour(1)}]}
  assert.equal(defaultReplayDeparture({...route,fromDrawing:true,departure_at:hour(1.25)},response),hour(1.25))
  assert.equal(defaultReplayDeparture(route,response),hour(.5))
  assert.equal(defaultReplayDeparture({...route,ephemeral:true},response),base)
})

test('particle replay advances the aircraft without rebuilding the background inside each 30 minute interval', async () => {
  const app = particleReplay()
  await app.controller.setContext(route, configuration, windows)
  const first = app.state().mapWind, firstHeatmap = app.state().mapHeatmap
  for (const seconds of [30, 300, 900, 1799.999]) {
    await app.controller.seek(seconds)
    assert.equal(app.state().seconds, seconds)
    assert.equal(app.state().mapWind, first)
    assert.equal(app.state().mapHeatmap, firstHeatmap)
    assert.equal(app.state().mapTime, base)
    assert.equal(app.state().mapLoading, false)
  }
  assert.ok(flightPosition(app.state().result, app.state().seconds).lon > 0)
  assert.equal(app.frameRequests.length, 1)
  await app.controller.seek(1800)
  const next = app.state().mapWind
  assert.notEqual(next, first)
  assert.equal(app.state().mapTime, hour(.5))
  await app.controller.seek(3599)
  assert.equal(app.state().mapWind, next)
  await app.controller.seek(3600)
  assert.equal(app.state().mapTime, hour(1))
  await app.controller.seek(900)
  assert.equal(app.state().seconds, 900)
  assert.equal(app.state().mapTime, base)
  assert.equal(app.frameRequests.length, 2)
  app.controller.dispose()
})

test('particle refresh boundaries follow elapsed flight time rather than UTC half hours', async () => {
  const app = particleReplay()
  await app.controller.setContext({ ...route, departure_at: hour(.25) }, configuration, windows)
  assert.equal(app.state().mapTime, hour(.25))
  await app.controller.seek(1799)
  assert.equal(app.state().mapTime, hour(.25))
  await app.controller.seek(1800)
  assert.equal(app.state().mapTime, hour(.75))
  app.controller.dispose()
})

test('short particle replays reach arrival without refreshing the departure background', async () => {
  const app = particleReplay({ requestProfile: async (_id, payload) => result(payload) })
  await app.controller.setContext(route, configuration, windows)
  const wind = app.state().mapWind
  await app.controller.seek(600)
  assert.equal(app.state().seconds, 600)
  assert.equal(flightPosition(app.state().result, app.state().seconds).lat, .05)
  assert.equal(app.state().mapWind, wind)
  app.controller.dispose()
})

test('automatic particle playback uses flight minutes at accelerated playback rates', async () => {
  const app = particleReplay()
  await app.controller.setContext(route, configuration, windows)
  const wind = app.state().mapWind
  app.controller.setRate(600)
  await app.controller.play()
  for (let tick = 1; tick <= 6; tick++) {
    const [id, callback] = app.timers.entries().next().value
    app.timers.delete(id)
    await callback()
    assert.equal(app.state().seconds, tick * 300)
    assert.equal(app.state().playing, true)
    if (tick < 6) assert.equal(app.state().mapWind, wind)
  }
  assert.equal(app.state().mapTime, hour(.5))
  assert.notEqual(app.state().mapWind, wind)
  app.controller.dispose()
})

test('switching between particles and heatmap refreshes the current background without changing the flight', async () => {
  const app = particleReplay()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.seek(900)
  await app.controller.play()
  const profile = app.state().result
  await app.controller.setMapUpdateInterval(0)
  assert.equal(app.state().mapTime, hour(.25))
  assert.equal(app.state().mapUpdateIntervalSeconds, 0)
  await app.controller.seek(1200, true)
  assert.equal(app.state().mapTime, hour(1 / 3))
  await app.controller.setMapUpdateInterval(PARTICLE_MAP_UPDATE_INTERVAL_SECONDS)
  assert.equal(app.state().mapTime, base)
  assert.equal(app.state().seconds, 1200)
  assert.equal(app.state().playing, true)
  assert.equal(app.state().result, profile)
  app.controller.dispose()
})

test('a late particle refresh cannot overwrite a backward seek within a previous interval', async () => {
  let finish
  const app = particleReplay({ requestFrame: async (id, at) => at === hour(1)
    ? new Promise((resolve) => { finish = () => resolve(windFrame(1, 2, 0, id)) }) : windFrame(0, 2, 0, id) })
  await app.controller.setContext(route, configuration, windows)
  const pending = app.controller.seek(1800)
  assert.equal(app.state().seconds, 1800)
  await flushWeather() // Simulate an already dispatched response that arrives after cancellation.
  await app.controller.seek(900)
  finish()
  await pending
  assert.equal(app.state().seconds, 900)
  assert.equal(app.state().mapTime, base)
  assert.equal(app.state().mapLoading, false)
  app.controller.dispose()
})

test('missing particle frames clear the background at the boundary while keeping aircraft movement', async () => {
  const app = particleReplay({ requestProfile: async (_id, payload) => ({ ...longFlightResult(payload), frame_times: [hour(0), hour(2)] }) })
  await app.controller.setContext(route, configuration, windows)
  await app.controller.seek(1800)
  assert.equal(app.state().mapWind, null)
  assert.equal(app.state().mapHeatmap, null)
  assert.match(app.state().mapError, /数据不足/)
  await app.controller.seek(2700)
  assert.equal(app.state().seconds, 2700)
  assert.equal(app.state().mapTime, hour(.5))
  assert.equal(app.state().mapWind, null)
  await app.controller.seek(7200)
  assert.ok(app.state().mapWind)
  assert.equal(app.state().mapTime, hour(2))
  app.controller.dispose()
})

test('unsaved plans adopt the validated profile at the planned departure, independently of window evaluation time', async () => {
  const app = setup()
  const prepared = result({ departure_at: hour(1) })
  const planned = { ...route, ephemeral: true, planning_key: 'job:1', departure_at: hour(1),
    initial_profile: prepared, profile_signature: JSON.stringify(configuration) }
  await app.controller.setContext(planned, configuration, windows)
  assert.equal(app.profiles.length, 0)
  assert.equal(app.state().result, prepared)
  assert.equal(app.state().departureAt, hour(1))
  assert.equal(app.state().mapTime, hour(1))
  // Same geometry and forecast, but a new planning result has a new departure.
  await app.controller.setContext({ ...planned, planning_key: 'job:2', departure_at: hour(2),
    initial_profile: result({ departure_at: hour(2) }) }, configuration, windows)
  assert.equal(app.state().departureAt, hour(2))
  assert.equal(app.state().mapTime, hour(2))
  app.controller.dispose()
})

test('a changed configuration cannot adopt the old plan profile and unsaved requests include geometry', async () => {
  const calls = []
  const app = setup({ requestProfile: async (route, payload) => { calls.push({ route, payload }); return result(payload) } })
  const planned = { ...route, ephemeral: true, departure_at: hour(1), initial_profile: result(),
    profile_signature: JSON.stringify(configuration) }
  const changed = { ...configuration, average_ground_speed_mps: 20 }
  await app.controller.setContext(planned, changed, windows)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].route, planned)
  assert.equal(calls[0].payload.average_ground_speed_mps, 20)
  assert.equal(calls[0].payload.departure_at, hour(1))
  app.controller.dispose()
})

test('selecting a window updates departure and re-evaluates the full flight', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.chooseWindow({ start: hour(1), end: hour(2) })
  assert.equal(app.profiles.at(-1).departure_at, hour(1))
  assert.equal(app.state().mapTime, hour(1))
  assert.equal(app.state().mode, 'flight')
  app.controller.dispose()
})

test('seeking updates time, particles and heatmap together and reuses hourly frames', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.seek(120)
  await app.controller.seek(300)
  assert.equal(app.state().seconds, 300)
  assert.equal(app.state().mapTime, hour(1 / 12))
  assert.equal(app.state().mapWind.metadata.valid_time, app.state().mapHeatmap.metadata.valid_time)
  assert.equal(app.frameRequests.length, 2)
  app.controller.dispose()
})

test('a periodic window refresh preserves the unchanged selected-flight profile and playback position', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.seek(240)
  const profile = app.state().result
  await app.controller.setContext(route, configuration, undefined)
  assert.equal(app.state().result, profile)
  assert.equal(app.state().seconds, 240)
  await app.controller.setContext(route, configuration, { ...windows, evaluated_at: hour(.01) })
  assert.equal(app.state().result, profile)
  assert.equal(app.state().seconds, 240)
  app.controller.dispose()
})

test('speed and threshold changes preserve selected departure but invalidate profile', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.chooseDeparture(hour(1))
  await app.controller.setContext(route, { ...configuration, average_ground_speed_mps: 20 }, undefined)
  assert.equal(app.state().result, null)
  assert.equal(app.state().loading, true)
  await app.controller.setContext(route, { ...configuration, average_ground_speed_mps: 20 }, windows)
  assert.equal(app.profiles.at(-1).average_ground_speed_mps, 20)
  assert.equal(app.profiles.at(-1).departure_at, hour(1))
  app.controller.dispose()
})

test('new WRF revision pauses replay and refreshes profile and map as one dataset', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.play()
  assert.equal(app.state().playing, true)
  await app.controller.setContext(route, configuration, { ...windows, dataset_id: 'two' })
  assert.equal(app.state().playing, false)
  assert.equal(app.state().result.dataset_id, 'two')
  assert.equal(app.state().mapWind.metadata.dataset_id, 'two')
  assert.match(app.state().notice, /预报已更新/)
  assert.equal(app.timers.size, 0)
  app.controller.dispose()
})

test('late profile responses cannot overwrite a different selected route', async () => {
  let finish
  const app = setup({ requestProfile: (_id, payload) => payload.dataset_id === 'one'
    ? new Promise((resolve) => { finish = () => resolve(result(payload)) }) : Promise.resolve(result(payload)) })
  const first = app.controller.setContext(route, configuration, windows)
  await app.controller.setContext({ ...route, route_id: 'other' }, configuration, { ...windows, dataset_id: 'two' })
  finish()
  await first
  assert.equal(app.state().result.dataset_id, 'two')
  app.controller.dispose()
})

test('late seeks cannot overwrite a newer slider position', async () => {
  let finish
  const app = setup({ requestFrame: async (id, at) => at === hour(1)
    ? new Promise((resolve) => { finish = () => resolve(windFrame(1, 2, 0, id)) }) : windFrame(0, 2, 0, id) })
  await app.controller.setContext(route, configuration, windows)
  const first = app.controller.seek(300)
  await flushWeather()
  await app.controller.seek(0)
  finish()
  await first
  assert.equal(app.state().seconds, 0)
  assert.equal(app.state().mapTime, base)
  app.controller.dispose()
})

test('missing forecast time clears previous map and still updates flight position', async () => {
  const app = setup({ requestProfile: async (_id, payload) => ({ ...result(payload), frame_times: [hour(0), hour(2)] }) })
  await app.controller.setContext(route, configuration, windows)
  assert.ok(app.state().mapWind)
  await app.controller.seek(300)
  assert.equal(app.state().mapWind, null)
  assert.equal(app.state().mapHeatmap, null)
  assert.equal(app.state().seconds, 300)
  assert.match(app.state().mapError, /数据不足/)
  app.controller.dispose()
})

test('snapshot mode pauses animation and switching routes resets the profile', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  await app.controller.play()
  app.controller.setMode('snapshot')
  assert.equal(app.state().playing, false)
  assert.equal(app.state().mode, 'snapshot')
  await app.controller.setContext(null)
  assert.equal(app.state().active, false)
  assert.equal(app.state().mapWind, null)
  assert.equal(app.timers.size, 0)
  app.controller.dispose()
})

const steppedFrame = (...args) => {
  const frame = windFrame(...args)
  frame.metadata.wind_time_step_seconds = 600
  return frame
}

test('ten-minute wind frames stay constant until the next clock boundary', () => {
  const a = steppedFrame(0, 2), b = steppedFrame(1, 8)
  assert.equal(interpolateWindFrames(a, b, hour(9.999 / 60)).wind.velocity[0].data[0], 2)
  const changed = interpolateWindFrames(a, b, hour(10 / 60))
  assert.equal(changed.wind.velocity[0].data[0], 3)
  assert.equal(changed.wind.metadata.valid_time, hour(10 / 60))
  assert.equal(interpolateWindFrames(a, b, hour(19.999 / 60)).wind.velocity[0].data[0], 3)
  assert.equal(interpolateWindFrames(a, b, hour(30 / 60)).heatmap.wind_speed.data[0], 5)
})

test('a held first frame does not require a missing future vector', () => {
  const a = steppedFrame(0, 2), b = steppedFrame(1, 8)
  b.velocity[0].data[0] = null
  assert.equal(interpolateWindFrames(a, b, hour(5 / 60)).wind.velocity[0].data[0], 2)
  assert.equal(interpolateWindFrames(a, b, hour(10 / 60)).wind.velocity[0].data[0], null)
  assert.equal(interpolateWindFrames(a, steppedFrame(2), hour(.5)), null)
})

test('heatmap reuses a ten-minute frame while aircraft time advances from a non-aligned departure', async () => {
  const app = setup({
    requestProfile: async (_id, payload) => ({ ...result(payload), wind_time_step_seconds: 600, flight_duration_seconds: 2400 }),
    requestFrame: async (id, at) => {
      const h = (Date.parse(at) - Date.parse(base)) / 3600000
      return steppedFrame(h, 2 + 6 * h, 0, id)
    },
  })
  await app.controller.setContext({ ...route, departure_at: hour(3 / 60) }, configuration, windows)
  const initial = app.state().mapWind
  assert.equal(app.state().mapTime, base)
  await app.controller.seek(300)
  assert.equal(app.state().seconds, 300)
  assert.equal(app.state().mapWind, initial)
  await app.controller.seek(420)
  assert.equal(app.state().seconds, 420)
  assert.equal(app.state().mapTime, hour(10 / 60))
  assert.equal(app.state().mapWind.velocity[0].data[0], 3)
  app.controller.dispose()
})

test('particle display retains thirty-minute refresh while sampling ten-minute model frames', async () => {
  const app = setup({
    mapUpdateIntervalSeconds: PARTICLE_MAP_UPDATE_INTERVAL_SECONDS,
    requestProfile: async (_id, payload) => ({ ...result(payload), wind_time_step_seconds: 600, flight_duration_seconds: 3600 }),
    requestFrame: async (id, at) => {
      const h = (Date.parse(at) - Date.parse(base)) / 3600000
      return steppedFrame(h, 2 + 6 * h, 0, id)
    },
  })
  await app.controller.setContext({ ...route, departure_at: hour(3 / 60) }, configuration, windows)
  const initial = app.state().mapWind
  await app.controller.seek(1200)
  assert.equal(app.state().mapWind, initial)
  await app.controller.seek(1800)
  assert.equal(app.state().mapTime, hour(.5))
  assert.equal(app.state().mapWind.velocity[0].data[0], 5)
  app.controller.dispose()
})

const nativeProfile = (payload) => ({ ...longFlightResult(payload), wind_time_step_seconds: 600,
  wind_time_model: NATIVE_WRF_TIME_MODEL, frame_times: Array.from({ length: 13 }, (_, i) => hour(i / 6)) })
const nativeFrame = (id, at) => {
  const h = (Date.parse(at) - Date.parse(base)) / 3600000
  const frame = steppedFrame(h, 2 + h, 0, id)
  frame.metadata.wind_time_model = NATIVE_WRF_TIME_MODEL
  return frame
}

test('native replay prefetches only the next two display frames and reuses them at their exact time', async () => {
  const requests = []
  const app = setup({ requestProfile: async (_id, payload) => nativeProfile(payload),
    requestFrame: async (id, at, _signal, options) => { requests.push({ at, prefetch: options.prefetch }); return nativeFrame(id, at) } })
  await app.controller.setContext(route, configuration, windows)
  await flushWeather()
  assert.deepEqual(requests.map((row) => row.at), [base, hour(1 / 6), hour(2 / 6)])
  assert.deepEqual(requests.map((row) => row.prefetch), [false, true, true])
  assert.equal(app.state().mapTime, base)
  await app.controller.seek(600)
  assert.equal(app.state().mapTime, hour(1 / 6))
  assert.equal(app.state().mapWind.velocity[0].data[0], 2 + 1 / 6)
  await flushWeather()
  assert.equal(requests.filter((row) => row.at === hour(1 / 6)).length, 1)
  assert.ok(app.controller.cacheStats().entries <= 8)
  app.controller.dispose()
  assert.equal(app.controller.cacheStats().bytes, 0)
})

test('native particle prefetch follows thirty flight minutes instead of downloading every ten-minute frame', async () => {
  const requests = []
  const app = particleReplay({ requestProfile: async (_id, payload) => nativeProfile(payload),
    requestFrame: async (id, at) => { requests.push(at); return nativeFrame(id, at) } })
  await app.controller.setContext(route, configuration, windows)
  await flushWeather()
  assert.deepEqual(requests, [base, hour(.5), hour(1)])
  assert.equal(app.state().mapTime, base)
  await app.controller.seek(1799)
  assert.equal(app.state().mapTime, base)
  app.controller.dispose()
})

test('a slow weather download does not block subsequent aircraft ticks or relabel the old background', async () => {
  const pending = new Map()
  const app = setup({ prefetchFrames: 0, requestProfile: async (_id, payload) => nativeProfile(payload),
    requestFrame: (id, at, signal) => at === base ? Promise.resolve(nativeFrame(id, at)) : new Promise((resolve) => {
      pending.set(at, { signal, finish: () => resolve(nativeFrame(id, at)) })
    }) })
  await app.controller.setContext(route, configuration, windows)
  app.controller.setRate(1200)
  await app.controller.play()
  const nextTick = () => { const [id, callback] = app.timers.entries().next().value; app.timers.delete(id); return callback() }
  const one = nextTick()
  await flushWeather()
  assert.equal(app.state().seconds, 600)
  assert.equal(app.state().mapTime, base)
  assert.equal(app.state().mapLoading, true)
  assert.equal(app.timers.size, 1)
  const two = nextTick()
  await flushWeather()
  assert.equal(app.state().seconds, 1200)
  assert.equal(pending.get(hour(1 / 6)).signal.aborted, false)
  assert.equal(pending.has(hour(2 / 6)), false)
  pending.get(hour(1 / 6)).finish()
  await one
  await flushWeather()
  assert.ok(pending.has(hour(2 / 6)))
  pending.get(hour(2 / 6)).finish()
  await two
  await flushWeather()
  assert.equal(app.state().mapTime, hour(2 / 6))
  assert.equal(app.state().playing, true)
  app.controller.dispose()
})

test('unchanged dataset frames survive a speed or risk-profile refresh', async () => {
  const app = setup()
  await app.controller.setContext(route, configuration, windows)
  const count = app.frameRequests.length
  await app.controller.setContext(route, { ...configuration, average_ground_speed_mps: 15 }, windows)
  assert.equal(app.profiles.length, 2)
  assert.equal(app.frameRequests.length, count)
  app.controller.dispose()
})

test('one batch prefetch supplies two native frames and is shared with the foreground consumer', async () => {
  const batches = [], foreground = []
  const app = setup({ requestProfile: async (_id, payload) => nativeProfile(payload),
    requestFrame: async (id, at) => { foreground.push(at); return nativeFrame(id, at) },
    requestFrames: async (id, times) => { batches.push(times); return times.map((at) => nativeFrame(id, at)) } })
  await app.controller.setContext(route, configuration, windows)
  await flushWeather()
  assert.deepEqual(foreground, [base])
  assert.deepEqual(batches, [[hour(1 / 6), hour(2 / 6)]])
  await app.controller.seek(600)
  assert.equal(app.state().mapTime, hour(1 / 6))
  assert.deepEqual(foreground, [base])
  app.controller.dispose()
})

test('drawing completion commits the background time only after the matching canvas has painted', async () => {
  const app = setup({ requireMapReady: true })
  await app.controller.setContext(route, configuration, windows)
  assert.equal(app.state().mapLoading, true)
  assert.equal(app.state().mapTime, null)
  const key = app.state().pendingMapKey
  app.controller.acknowledgeMap('obsolete')
  assert.equal(app.state().mapTime, null)
  app.controller.acknowledgeMap(key)
  assert.equal(app.state().mapTime, base)
  assert.equal(app.state().mapLoading, false)
  app.controller.dispose()
})

test('classic verified profile starts before windows and window completion never rewinds playback', async () => {
  const app = setup()
  const planned = { ...route, ephemeral: true, planning_key: 'classic-initial', initial_profile: result(),
    profile_signature: JSON.stringify(configuration) }
  await app.controller.setContext(planned, configuration)
  assert.equal(app.profiles.length, 0)
  assert.ok(app.state().result)
  await app.controller.seek(120)
  await app.controller.setContext({ ...planned, route_id: 'window-normalized-id' }, configuration, windows)
  assert.equal(app.state().seconds, 120)
  assert.equal(app.profiles.length, 0)
  app.controller.dispose()
})
