import test from 'node:test'
import assert from 'node:assert/strict'
import { pollV3Windows, createWindowRequestCoordinator } from './v3Windows.js'
import { createDepartureWindowEvaluator, departureWindowDecision } from './departureWindows.js'
import { createFlightReplayController } from './flightReplay.js'

const profile = { dataset_id: 'v3_dataset', departure_at: '2026-11-03T01:00:00Z', arrival_at: '2026-11-03T01:01:00Z',
  time_model: 'constant_airspeed', flight_duration_seconds: 60, analysis: { samples: [] }, frame_times: [] }
const configuration = { time_model: 'constant_airspeed', airspeed_mps: 10,
  thresholds: { safe: 1.5, notice: 3.3, warning: 5.4, danger: 7.9 },
  wind_shear: { horizontal: { hard_delta_wind_vector_ms: 5.4 } } }
const route = { route_id: 'curve-a', ephemeral: true, v3: { trajectory_id: 'curve-a' },
  initial_profile: profile, profile_signature: JSON.stringify(configuration) }

test('refresh of the same geometry waits for cancellation acknowledgement without blocking another route', async () => {
  const coordinate = createWindowRequestCoordinator()
  const controller = new AbortController()
  let acknowledge, created = false
  const cancellation = new Promise((resolve) => { acknowledge = resolve })
  const first = coordinate('same', controller.signal, async () => {
    await cancellation
    throw Object.assign(new Error('cancelled'), { name: 'AbortError' })
  })
  const rejected = assert.rejects(first, { name: 'AbortError' })
  controller.abort()
  const replacement = coordinate('same', new AbortController().signal, () => { created = true; return 'new window job' })
  assert.equal(await coordinate('other', undefined, () => 'another route'), 'another route')
  assert.equal(created, false)
  acknowledge()
  await rejected
  assert.equal(await replacement, 'new window job')
})

test('V3 first solution adopts its profile before future windows are ready', async () => {
  let calls = 0, state
  const replay = createFlightReplayController({ requestProfile: async () => { calls++; return profile },
    requestFrame: async () => null, onChange: (value) => { state = value } })
  await replay.setContext(route, configuration)
  assert.equal(calls, 0)
  assert.equal(state.result, profile)
  replay.dispose()
})

test('V3 future departure requests keep the curve and change only its timing', async () => {
  const calls = []
  const replay = createFlightReplayController({ requestProfile: async (geometry, config) => {
    calls.push([geometry, config]); return { ...profile, departure_at: config.departure_at }
  }, requestFrame: async () => null, onChange: () => {} })
  await replay.setContext(route, configuration)
  await replay.chooseDeparture('2026-11-03T02:00:00Z')
  assert.equal(calls[0][0], route)
  assert.equal(calls[0][1].departure_at, '2026-11-03T02:00:00Z')
  assert.equal(calls[0][1].airspeed_mps, 10)
  replay.dispose()
})

test('window polling publishes certified partial results and retains them on timeout', async () => {
  const updates = []
  const partial = { time_model: 'constant_airspeed', complete: false, departure_windows: [{ start: 'a', end: 'b' }] }
  const states = [{ job_id: 'w1', status: 'running', revision: 1, result: partial },
    { job_id: 'w1', status: 'timed_out', revision: 2, result: partial, message: '其余时段未确认' }]
  const result = await pollV3Windows({ create: async () => states.shift(), get: async () => states.shift(),
    cancel: () => assert.fail('finished job must not be cancelled'), onProgress: (value) => updates.push(value), wait: async () => {} })
  assert.equal(updates.length, 2)
  assert.equal(result.departure_windows.length, 1)
  assert.equal(result.window_job_status, 'timed_out')
})

test('window cancellation aborts the server task and discards later updates', async () => {
  const controller = new AbortController(), cancelled = []
  await assert.rejects(pollV3Windows({ signal: controller.signal,
    create: async () => ({ job_id: 'cancel-me', status: 'running', revision: 0 }),
    get: async () => assert.fail('aborted job must not poll again'), cancel: async (id) => cancelled.push(id),
    wait: async () => controller.abort(),
  }), { name: 'AbortError' })
  assert.deepEqual(cancelled, ['cancel-me'])
})

test('a late partial certificate cannot replace another selected route', async () => {
  const requests = [], changes = []
  const evaluator = createDepartureWindowEvaluator({ request: (geometry, config, signal, onProgress) => {
    requests.push({ geometry, signal, onProgress }); return new Promise(() => {})
  }, onChange: (state) => changes.push(state), setTimer: () => 1, clearTimer: () => {} })
  evaluator.select(route, configuration)
  evaluator.select({ ...route, route_id: 'curve-b', v3: { trajectory_id: 'curve-b' } }, configuration)
  const count = changes.length
  requests[0].onProgress({ time_model: 'constant_airspeed', current_departure: { status: 'allowed' } })
  assert.equal(changes.length, count)
  assert.equal(requests[0].signal.aborted, true)
  evaluator.dispose()
})

test('only current V3 partial certificates may display an allowed verdict while loading', () => {
  const result = { time_model: 'constant_airspeed', current_departure: { status: 'allowed', reason: '已确认' } }
  assert.equal(departureWindowDecision({ active: true, loading: true, result }).level, '允许通航')
  assert.equal(departureWindowDecision({ active: true, loading: true, result: { current_departure: result.current_departure } }).level, '待评估')
})
