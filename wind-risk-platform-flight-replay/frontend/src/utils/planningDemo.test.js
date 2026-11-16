import test from 'node:test'
import assert from 'node:assert/strict'
import { planningDemoGeometry, createPlanningDemoController, createEarlyPlanningDemo } from './planningDemo.js'
import { pollRouteJob } from './routePlanning.js'
import { pollRecommendationJob } from './recommendations.js'

const base = Date.parse('2026-11-15T00:00:00Z')
const at = minutes => new Date(base + minutes * 60000).toISOString()
const native = 'NATIVE_WRF_TEN_MINUTE_HOLD_V1'
const arc = (a, b) => {
  const rad = Math.PI / 180, h = Math.sin((b[1] - a[1]) * rad / 2) ** 2
    + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin((b[0] - a[0]) * rad / 2) ** 2
  return 6371000 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h))
}
const route = (points = [[0, 0], [.04, 0], [.04, .04], [.08, .04]], offset = 3, id = 'one') => ({
  points, dataset_id: id, average_ground_speed_mps: 10,
  flight_profile: { dataset_id: id, departure_at: at(offset), level: '100m AGL', average_ground_speed_mps: 10,
    flight_duration_seconds: points.slice(1).reduce((sum, p, i) => sum + arc(points[i], p) / 10, 0),
    frame_times: Array.from({ length: 20 }, (_, i) => at(i * 10)), wind_time_model: native, wind_time_step_seconds: 600,
    analysis: { samples: [] } },
})
const frame = (id, time, model = native, value = 2) => ({ available: true,
  metadata: { dataset_id: id, valid_time: time, cycle: at(0), level: '100m AGL', forecast_hour: (Date.parse(time) - base) / 3600000,
    wind_time_model: model, wind_time_step_seconds: 600 },
  velocity: [value, 0].map(v => ({ header: { nx: 2, ny: 2, lo1: 0, la1: 1, dx: 1, dy: 1 }, data: [v, v, v, v] })),
})
const flush = async () => { for (let i = 0; i < 50; i++) await Promise.resolve() }
const setup = (options = {}) => {
  let state, sequence = 0
  const timers = new Map(), requests = [], batches = [], errors = []
  const controller = createPlanningDemoController({
    requestFrame: async (id, time) => { requests.push([id, time]); return frame(id, time) },
    requestFrames: async (id, times) => { batches.push(times); return times.map(t => frame(id, t)) },
    onChange: value => { state = value }, onError: error => errors.push(error),
    setTimer: (callback, ms) => { assert.equal(ms, 2000); timers.set(++sequence, callback); return sequence },
    clearTimer: id => timers.delete(id), ...options,
  })
  const acknowledge = () => controller.acknowledgeMap(state.pendingMapKey)
  const tick = async () => { const [id, run] = timers.entries().next().value; timers.delete(id); run(); await flush() }
  return { controller, state: () => state, timers, requests, batches, errors, acknowledge, tick }
}

test('ten-minute geometry uses UTC boundaries, keeps every bend and a short final leg', () => {
  const original = route(), before = JSON.stringify(original)
  const geometry = planningDemoGeometry(original)
  assert.equal(geometry.segments.length, 3)
  assert.deepEqual(geometry.segments.map(s => s.begin), [0, 420, 1020])
  assert.equal(geometry.segments[0].end, 420)
  assert.equal(geometry.segments[2].end, original.flight_profile.flight_duration_seconds)
  assert.deepEqual(geometry.segments.map(s => s.at), [at(0), at(10), at(20)])
  assert.ok(geometry.segments[1].points.some(p => p[0] === .04 && p[1] === 0))
  assert.ok(geometry.segments[1].points.some(p => p[0] === .04 && p[1] === .04))
  assert.deepEqual(geometry.segments.at(-1).points.at(-1), original.points.at(-1))
  assert.equal(JSON.stringify(original), before)
})

test('short routes are one exact leg; inconsistent timing cannot produce an invented prefix', () => {
  const short = route([[0, 0], [.001, 0]], 0)
  assert.equal(planningDemoGeometry(short).segments.length, 1)
  assert.deepEqual(planningDemoGeometry(short).segments[0].points, short.points)
  short.flight_profile.flight_duration_seconds += 10
  assert.throws(() => planningDemoGeometry(short), /不一致/)
})

test('blue prefix and background commit only after matching draw acknowledgment; pause for two seconds', async () => {
  const app = setup(), original = route(), before = JSON.stringify(original)
  app.controller.show(original); await flush()
  assert.equal(app.state().index, -1)
  assert.deepEqual(app.state().points, [])
  assert.equal(app.state().mapTime, null)
  assert.equal(app.timers.size, 0)
  app.controller.acknowledgeMap('obsolete-key')
  assert.equal(app.state().index, -1)
  app.acknowledge()
  assert.equal(app.state().index, 0)
  assert.equal(app.state().mapTime, at(0))
  assert.equal(app.state().seconds, 420)
  assert.equal(app.timers.size, 1)
  assert.ok(app.batches.every(batch => batch.length <= 2))
  await app.tick()
  assert.equal(app.state().index, 0) // The next field is not yet drawn.
  app.acknowledge()
  assert.equal(app.state().index, 1)
  assert.equal(app.state().mapTime, at(10))
  await app.tick(); app.acknowledge()
  assert.equal(app.state().completed, true)
  assert.equal(app.timers.size, 0)
  assert.deepEqual(app.state().points.at(-1), original.points.at(-1))
  assert.equal(JSON.stringify(original), before) // Save/window inputs were never modified.
  app.controller.dispose()
})

test('same or differently ranked geometry does not repeat; explicit invalidation can replace its changed leg', async () => {
  const app = setup(), original = route()
  app.controller.show(original); await flush(); app.acknowledge()
  await app.tick(); app.acknowledge(); await app.tick(); app.acknowledge()
  const count = app.requests.length
  app.controller.show({ ...original, objective_cost: 99 })
  assert.equal(app.state().completed, true)
  assert.equal(app.requests.length, count)
  app.controller.show(route([[0, 0], [.04, 0], [.04, .06], [.08, .04]]))
  assert.equal(app.state().completed, true)
  assert.equal(app.requests.length, count)
  app.controller.show(route([[0, 0], [.04, 0], [.04, .06], [.08, .04]]), { replaceInvalid: true })
  await flush()
  const oldTime = app.state().mapTime
  assert.equal(oldTime, at(20))
  app.acknowledge()
  assert.equal(app.state().index, 1)
  assert.equal(app.state().mapTime, at(10))
  assert.ok(app.state().points.some(p => p[0] === .04 && p[1] === 0))
  app.controller.dispose()
})

test('dismiss/new task cancels pending work and a late response cannot grow the next route', async () => {
  let resolveOld, oldSignal
  const app = setup({ requestFrame: (id, time, signal) => id === 'old'
    ? (oldSignal = signal, new Promise(resolve => { resolveOld = resolve })) : Promise.resolve(frame(id, time)) })
  app.controller.show(route(undefined, 3, 'old')); await flush()
  app.controller.reset()
  assert.equal(oldSignal.aborted, true)
  app.controller.show(route(undefined, 3, 'new')); await flush(); app.acknowledge()
  resolveOld(frame('old', at(0))); await flush()
  assert.equal(app.state().mapWind.metadata.dataset_id, 'new')
  app.controller.dismiss()
  app.controller.show(route())
  assert.equal(app.state().active, false)
  assert.equal(app.timers.size, 0)
  app.controller.dispose()
})

test('missing/mixed frames pause without relabelling the old field; retry resumes that leg', async () => {
  let fail = false
  const app = setup({ requestFrames: undefined, requestFrame: async (id, time) => {
    if (fail) return { available: false }
    return frame(id, time)
  } })
  app.controller.show(route()); await flush(); app.acknowledge()
  fail = true; await app.tick()
  assert.ok(app.state().error)
  assert.equal(app.state().mapTime, at(0))
  assert.equal(app.state().index, 0)
  assert.equal(app.timers.size, 0)
  fail = false; app.controller.retry(); await flush(); app.acknowledge()
  assert.equal(app.state().index, 1)
  assert.equal(app.state().error, '')
  app.controller.dispose()
})

test('GFS demo preserves hourly-vector interpolation at each ten-minute display frame', async () => {
  const model = 'HOURLY_UV_TEN_MINUTE_HOLD_V1', original = route()
  original.flight_profile = { ...original.flight_profile, wind_time_model: model, frame_times: [at(0), at(60), at(120)] }
  const app = setup({ requestFrames: undefined,
    requestFrame: async (id, time) => frame(id, time, model, (Date.parse(time) - base) / 600000),
  })
  app.controller.show(original); await flush(); app.acknowledge()
  await app.tick(); app.acknowledge()
  assert.equal(app.state().mapWind.velocity[0].data[0], 1)
  assert.equal(app.state().mapWind.metadata.wind_time_model, model)
  app.controller.dispose()
})

const verified = points => {
  const result = route(points)
  result.flight_profile.analysis.samples = result.points.map((p, i) => ({ lon: p[0], lat: p[1], elapsed_seconds: i * 300 }))
  return { ...result, time_aware: true, level: result.flight_profile.level, planner_type: 'wa_lpa_star',
    departure_at: result.flight_profile.departure_at, analysis: { navigation_allowed: true } }
}

test('early preview rejects invalid/foreign/stale candidates and latches its first accepted route', () => {
  let current = false, published = false
  const shown = [], candidate = verified()
  const preview = createEarlyPlanningDemo({ show: result => shown.push(result), isCurrent: () => current, isPublished: () => published })
  preview(candidate)
  current = true
  for (const value of [null, { ...candidate, reference_route: true }, { ...candidate, v3: true },
    { ...candidate, analysis: { navigation_allowed: false } }, { ...candidate, flight_profile: null },
    { ...candidate, flight_profile: { ...candidate.flight_profile, dataset_id: 'different' } },
    { ...candidate, flight_profile: { ...candidate.flight_profile, level: '300m AGL' } }]) preview(value)
  assert.equal(shown.length, 0)
  published = true; preview(candidate)
  assert.equal(shown.length, 0)
  published = false; preview(candidate); preview(verified([[0, 0], [.03, .03], [.08, .04]]))
  assert.deepEqual(shown, [candidate])
})

for (const [label, poll] of [['classic job', pollRouteJob], ['specified-time workflow', pollRecommendationJob]]) {
  test(`${label}: first blue segment draws before thirty seconds while formal updates stay at thirty/end`, async () => {
    let clock = 0, demoState, firstBlueAt, firstCandidateAt, published = false, timerSequence = 0
    const timers = new Map(), publications = [], previews = []
    const demo = createPlanningDemoController({
      requestFrame: async (id, time) => frame(id, time),
      onChange: state => {
        demoState = state
        if (state.points.length >= 2 && firstBlueAt === undefined) firstBlueAt = clock
      },
      setTimer: (callback, ms) => { const id = ++timerSequence; timers.set(id, { callback, at: clock + ms }); return id },
      clearTimer: id => timers.delete(id),
    })
    const early = createEarlyPlanningDemo({ isCurrent: () => true, isPublished: () => published,
      show: value => { firstCandidateAt = clock; previews.push(value); demo.show(value) } })
    const first = verified(), improved = verified([[0, 0], [.04, 0], [.04, .06], [.08, .04]])
    const final = verified([[0, 0], [.04, 0], [.04, .05], [.08, .04]])
    const queued = { job_id: 'early-demo', revision: 0, status: 'running', timeout_seconds: 7200 }
    const snapshots = [
      { ...queued, revision: 1, result: first }, { ...queued, revision: 2, result: improved },
      ...Array.from({ length: 5 }, () => ({ ...queued, revision: 2 })),
      { ...queued, revision: 3, status: 'completed', result: final },
    ]
    try {
      await poll({ create: async () => queued, get: async () => snapshots.shift(), cancel: async () => {},
        initialDisplayDelayMs: 30000, intervalMs: 5000, now: () => clock,
        wait: async ms => {
          clock += ms
          await flush()
          for (const [id, task] of [...timers]) if (task.at <= clock) { timers.delete(id); task.callback() }
          await flush()
          if (demoState?.pendingMapKey) demo.acknowledgeMap(demoState.pendingMapKey)
        },
        onCandidate: early,
        onProgress: value => { published = true; publications.push([clock, value]); demo.show(value) },
      })
      assert.equal(firstCandidateAt, 5000)
      assert.equal(firstBlueAt, 10000)
      assert.ok(firstBlueAt < 30000)
      assert.deepEqual(previews, [first])
      assert.deepEqual(publications, [[30000, improved], [40000, final]])
    } finally { demo.dispose() }
  })
}
