import test from 'node:test'
import assert from 'node:assert/strict'
import { createFirstFeasibleFlight, createPlanningSearchPreview, createPlanningDemoController } from './planningDemo.js'
import { createFlightReplayController } from './flightReplay.js'
import { pollRouteJob } from './routePlanning.js'
import { pollRecommendationJob } from './recommendations.js'

const at = minutes => new Date(Date.parse('2026-11-15T00:00:00Z') + minutes * 60000).toISOString()
const native = 'NATIVE_WRF_TEN_MINUTE_HOLD_V1', level = '100m AGL'
const context = { dataset_id: 'first', source: 'wrf', level, departure_at: at(0), average_ground_speed_mps: 10,
  frame_times: [at(0), at(10), at(20), at(30)], wind_time_model: native, wind_time_step_seconds: 600 }
const candidate = () => ({ ...context, time_aware: true, points: [[0, 0], [.1, 0]], analysis: { navigation_allowed: true },
  flight_profile: { ...context, flight_duration_seconds: 1200, analysis: { navigation_allowed: true, samples: [
    { lon: 0, lat: 0, elapsed_seconds: 0 }, { lon: .1, lat: 0, elapsed_seconds: 1200 },
  ] } },
})
const frame = time => ({ available: true, metadata: { ...context, valid_time: time },
  velocity: [2, 0].map(value => ({ header: { nx: 2, ny: 2, lo1: 0, la1: 1, dx: 1, dy: 1 }, data: Array(4).fill(value) })),
})
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve() }

for (const [label, poll] of [['classic jobs', pollRouteJob], ['specified-time workflow', pollRecommendationJob]]) {
  test(`${label}: first verified candidate publishes and plays before 30s, exactly once`, async () => {
    let clock = 0, polls = 0, cancelled = 0
    const events = [], result = candidate()
    const complete = createFirstFeasibleFlight({ isCurrent: () => true,
      stopPreview: () => events.push(['stop-preview', clock]), publish: () => events.push(['publish', clock]),
      play: () => events.push(['play', clock]) })
    const end = await poll({ initialDisplayDelayMs: 0, intervalMs: 5000, now: () => clock,
      create: async () => ({ job_id: 'first', status: 'running', revision: 0 }),
      get: async () => ++polls === 1 ? { status: 'running', revision: 1, result }
        : { status: 'completed', revision: 2, result: { ...result, planning_message: '搜索结束' } },
      wait: async ms => { clock += ms }, cancel: async () => { cancelled++ },
      onCandidate: complete, onProgress: complete,
    })
    assert.deepEqual(events, [['stop-preview', 5000], ['publish', 5000], ['play', 5000]])
    assert.equal(cancelled, 0) // Completion is not disguised as user cancellation.
    assert.equal(end.result?.planning_message ?? end.planning_message, '搜索结束')
  })
}

test('unsafe, reference, stale and version-mismatched routes cannot trigger first-solution playback', () => {
  let current = false, plays = 0
  const done = createFirstFeasibleFlight({ isCurrent: () => current, stopPreview: () => {}, publish: () => {}, play: () => plays++ })
  done(candidate()); current = true
  for (const bad of [{ ...candidate(), reference_route: true }, { ...candidate(), analysis: { navigation_allowed: false } },
    { ...candidate(), flight_profile: { ...candidate().flight_profile, dataset_id: 'other' } }]) assert.equal(done(bad), false)
  assert.equal(plays, 0)
  assert.equal(done(candidate()), true)
  done(candidate())
  assert.equal(plays, 1)
})

test('replay starts immediately from the accepted profile while its cold wind frame and windows are still pending', async () => {
  let state, preparing, finishWind, timer, requests = 0
  const config = { average_ground_speed_mps: 10, thresholds: { danger: 7.9 } }
  const replay = createFlightReplayController({ requireMapReady: true, prefetchFrames: 0,
    requestProfile: async () => { throw Error('must reuse the independently verified profile') },
    requestFrame: async (_id, time) => { requests++; await new Promise(resolve => { finishWind = resolve }); return frame(time) },
    onChange: value => { state = value }, setTimer: run => { timer = run; return 1 }, clearTimer: () => { timer = undefined },
  })
  const accepted = candidate()
  const route = { route_id: 'unsaved-first', planning_key: 'job:first', points: accepted.points, level, ephemeral: true,
    departure_at: accepted.departure_at, initial_profile: accepted.flight_profile, profile_signature: JSON.stringify(config) }
  const done = createFirstFeasibleFlight({ isCurrent: () => true, stopPreview: () => {},
    publish: () => { preparing = replay.setContext(route, config) }, play: () => { void replay.play() } })
  done(accepted)
  assert.equal(state.result, accepted.flight_profile)
  assert.equal(state.playing, true)
  assert.equal(state.mapLoading, true)
  assert.ok(timer)
  await flush(); finishWind(); await preparing
  replay.acknowledgeMap(state.pendingMapKey)
  await replay.setContext({ ...route, route_id: 'window-generated-id' }, config, { dataset_id: 'first', evaluated_at: at(10) })
  assert.equal(state.playing, true)
  assert.equal(state.result, accepted.flight_profile)
  assert.equal(requests, 1)
  done({ ...accepted, objective_cost: 0 })
  assert.equal(state.playing, true)
  replay.dispose()
})

test('search preview appends one branch, ignores rank changes, and replaces only an explicitly invalidated branch', () => {
  let published = false, current = true
  const shown = []
  const preview = createPlanningSearchPreview({ isCurrent: () => current, isPublished: () => published,
    start: [0, 0], source: 'wrf', level, show: (value, options) => shown.push([value, options]) })
  const progress = points => ({ preview_context: context, forward: [{ points }] })
  preview(progress([[0, 0], [.03, 0]])) // Less than a ten-minute leg: no fabricated extension.
  assert.equal(shown.length, 0)
  preview(progress([[0, 0], [.03, 0], [.06, 0]]))
  assert.equal(shown.length, 1)
  assert.equal(shown[0][0].flight_profile.flight_duration_seconds, 600)
  preview(progress([[0, 0], [.03, .03], [.06, .03]]))
  assert.equal(shown.length, 1)
  preview(progress([[0, 0], [.03, 0], [.06, 0], [.12, 0]]))
  assert.equal(shown.length, 2)
  assert.equal(shown[1][0].flight_profile.flight_duration_seconds, 1200)
  preview({ ...progress([[0, 0], [.03, .03], [.12, .03]]), preview_invalidated: true })
  assert.equal(shown.length, 3)
  assert.equal(shown[2][1].replaceInvalid, true)
  published = true; preview(progress([[0, 0], [.03, 0], [.2, 0]]))
  published = false; current = false; preview(progress([[0, 0], [.03, 0], [.2, 0]]))
  assert.equal(shown.length, 3)
})

test('extending a partially drawn preview never redraws its earlier ten-minute frames', async () => {
  let state
  const requested = []
  const demo = createPlanningDemoController({ requireMapReady: false,
    requestFrame: async (_id, time) => { requested.push(time); return frame(time) }, onChange: value => { state = value } })
  const preview = createPlanningSearchPreview({ isCurrent: () => true, isPublished: () => false,
    start: [0, 0], source: 'wrf', level, show: (value, options) => demo.show(value, options) })
  preview({ preview_context: context, forward: [{ points: [[0, 0], [.03, 0], [.06, 0]] }] })
  await flush()
  assert.equal(state.index, 0)
  preview({ preview_context: context, forward: [{ points: [[0, 0], [.03, 0], [.06, 0], [.12, 0]] }] })
  await flush()
  assert.equal(state.index, 1)
  assert.deepEqual(requested, [at(0), at(10)])
  demo.dismiss()
  preview({ preview_context: context, forward: [{ points: [[0, 0], [.03, 0], [.06, 0], [.2, 0]] }] })
  assert.equal(state.active, false)
  demo.dispose()
})
