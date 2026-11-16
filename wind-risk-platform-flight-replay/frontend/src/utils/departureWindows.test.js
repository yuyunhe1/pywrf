import assert from 'node:assert/strict'
import test from 'node:test'
import { createDepartureWindowEvaluator, departureWindowDecision, emptyDepartureWindowMessage, formatDepartureTime } from './departureWindows.js'

const clock = Date.parse('2026-11-01T00:00:00Z')
const route = { route_id: 'fixed-a', name: '航线 A', level: '100m AGL' }
const config = (speed = 10) => ({
  average_ground_speed_mps: speed,
  thresholds: { safe: 1.5, notice: 3.3, warning: 5.4, danger: 7.9 },
  wind_shear: { enabled: true, horizontal: { hard_delta_wind_vector_ms: 5.4 } },
})
const result = (id = 'fixed-a') => ({
  route_id: id, evaluated_at: '2026-11-01T00:00:00Z', refresh_at: '2026-11-01T00:01:00Z',
  current_departure: { status: 'allowed', reason: '全程可飞' }, departure_windows: [],
})

const harness = (request, now = () => clock) => {
  const states = []
  const timers = new Map()
  let timerId = 0
  const evaluator = createDepartureWindowEvaluator({ request, onChange: (state) => states.push(state), now,
    setTimer: (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id },
    clearTimer: (id) => timers.delete(id),
  })
  const tick = async () => {
    const [id, timer] = timers.entries().next().value
    timers.delete(id)
    await timer.fn()
  }
  return { evaluator, states, timers, tick }
}

test('loading, missing data and failures never reuse an old allowed decision', () => {
  const active = { active: true, result: result() }
  assert.equal(departureWindowDecision(active).level, '允许通航')
  assert.equal(departureWindowDecision({ ...active, loading: true }).level, '待评估')
  assert.equal(departureWindowDecision({ ...active, error: '连接失败' }).level, '待评估')
  assert.equal(departureWindowDecision({ active: true, result: { current_departure: { status: 'unknown' } } }).level, '数据不足')
  assert.equal(departureWindowDecision({ active: true, result: { current_departure: { status: 'blocked' } } }).level, '禁止通航')
  assert.equal(departureWindowDecision({ active: false }), null)
})

test('saved route selection evaluates at 10 m/s and schedules a current-time refresh', async () => {
  const calls = []
  const h = harness(async (...args) => { calls.push(args); return result() })
  await h.evaluator.select(route, config())
  assert.equal(calls[0][0], route.route_id)
  assert.equal(calls[0][1].average_ground_speed_mps, 10)
  assert.equal(h.states[0].loading, true)
  assert.equal(h.states.at(-1).result.current_departure.status, 'allowed')
  assert.equal([...h.timers.values()][0].delay, 60000)
  h.evaluator.dispose()
})

test('an unsaved plan displays its own prepared windows then refreshes using geometry', async () => {
  const calls = []
  const transient = { ...route, ephemeral: true, points: [[0, 0], [.1, 0]], dataset_id: 'pinned' }
  const h = harness(async (...args) => { calls.push(args); return result() })
  await h.evaluator.select(transient, config(), result())
  assert.equal(calls.length, 0)
  assert.equal(h.states.at(-1).result.route_id, transient.route_id)
  await h.tick()
  assert.equal(calls[0][0], transient)
  assert.equal(calls[0][1].average_ground_speed_mps, 10)
  h.evaluator.dispose()
})

test('speed and threshold edits clear the old verdict and debounce for 300 milliseconds', async () => {
  const calls = []
  const h = harness(async (id, settings) => { calls.push(settings); return result() })
  await h.evaluator.select(route, config())
  h.evaluator.update(config(15))
  const latest = config(20)
  latest.thresholds.danger = 8
  h.evaluator.update(latest)
  assert.equal(calls.length, 1)
  assert.equal(h.states.at(-1).result, null)
  assert.equal(h.states.at(-1).loading, true)
  assert.equal(h.timers.size, 1)
  assert.equal([...h.timers.values()][0].delay, 300)
  await h.tick()
  assert.equal(calls.at(-1).average_ground_speed_mps, 20)
  assert.equal(calls.at(-1).thresholds.danger, 8)
  h.evaluator.dispose()
})

test('a delayed response from the previous route cannot replace the newly selected route', async () => {
  const pending = []
  const h = harness((id, settings, signal) => new Promise((resolve) => pending.push({ id, signal, resolve })))
  const first = h.evaluator.select(route, config())
  const second = h.evaluator.select({ ...route, route_id: 'fixed-b' }, config())
  assert.equal(pending[0].signal.aborted, true)
  pending[1].resolve(result('fixed-b'))
  await second
  pending[0].resolve(result('fixed-a'))
  await first
  assert.equal(h.states.at(-1).route.route_id, 'fixed-b')
  assert.equal(h.states.at(-1).result.route_id, 'fixed-b')
  h.evaluator.dispose()
})

test('invalid speeds do not send requests or show an old green verdict', async () => {
  let calls = 0
  const h = harness(async () => { calls += 1; return result() })
  for (const speed of [0, -1, '', NaN, Infinity]) {
    await h.evaluator.select(route, config(speed))
    assert.match(h.states.at(-1).error, /速度/)
    assert.equal(h.states.at(-1).result, null)
  }
  assert.equal(calls, 0)
  h.evaluator.dispose()
})

test('refreshes at an impending weather boundary and rejects expired responses', async () => {
  const h = harness(async () => ({ ...result(), refresh_at: '2026-11-01T00:00:07Z' }))
  await h.evaluator.select(route, config())
  assert.equal([...h.timers.values()][0].delay, 7000)
  h.evaluator.dispose()
  const expired = harness(async () => ({ ...result(), refresh_at: '2026-10-31T23:59:00Z' }))
  await expired.evaluator.select(route, config())
  assert.equal(expired.states.at(-1).result, null)
  assert.equal(expired.states.at(-1).loading, true)
  expired.evaluator.dispose()
})

test('failed requests are retried and clearing the route cancels future work', async () => {
  let calls = 0
  const h = harness(async () => { calls += 1; if (calls === 1) throw new Error('offline'); return result() })
  await h.evaluator.select(route, config())
  assert.ok(h.states.at(-1).error)
  await h.tick()
  assert.equal(h.states.at(-1).result.current_departure.status, 'allowed')
  h.evaluator.clear()
  assert.equal(h.states.at(-1).active, false)
  assert.equal(h.timers.size, 0)
})

test('visitor clock skew does not reject a valid server result', async () => {
  const h = harness(async () => result(), () => clock + 120000)
  await h.evaluator.select(route, config())
  assert.equal(h.states.at(-1).result.current_departure.status, 'allowed')
  assert.equal([...h.timers.values()][0].delay, 60000)
  h.evaluator.dispose()
})

test('positive speeds below 0.1 m/s remain valid', async () => {
  let sentSpeed
  const h = harness(async (id, settings) => { sentSpeed = settings.average_ground_speed_mps; return result() })
  await h.evaluator.select(route, config(0.05))
  assert.equal(sentSpeed, 0.05)
  assert.equal(h.states.at(-1).error, '')
  h.evaluator.dispose()
})

test('a result expiring before the next render stays pending instead of showing a stale green verdict', async () => {
  const h = harness(async () => ({ ...result(), refresh_at: '2026-11-01T00:00:00.007Z' }))
  await h.evaluator.select(route, config())
  assert.equal(h.states.at(-1).result, null)
  assert.equal(departureWindowDecision(h.states.at(-1)).level, '待评估')
  assert.equal([...h.timers.values()][0].delay, 100)
  h.evaluator.dispose()
})

test('all displayed times use Beijing time and empty results distinguish missing forecasts', () => {
  assert.match(formatDepartureTime('2026-11-01T16:05:00Z'), /11\/02.*00:05/)
  assert.equal(formatDepartureTime(null), '—')
  assert.match(emptyDepartureWindowMessage('data_insufficient'), /预报/)
  assert.match(emptyDepartureWindowMessage('conditions_unsuitable'), /限制/)
  assert.match(emptyDepartureWindowMessage('short_window'), /过短/)
})
