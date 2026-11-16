import assert from 'node:assert/strict'
import test from 'node:test'
import { pollRouteJob } from './routePlanning.js'

const route = (distance) => ({ points: [[118, 31], [119, 32]], distance_km: distance })
const queued = { job_id: 'test', status: 'queued', revision: 0, result: null, timeout_seconds: 600 }

test('first official publication latches off corridors for the rest of the task', async () => {
  let clock = 0, published = false, released = false
  const progress = []
  const snapshots = [
    { ...queued, status: 'running', revision: 1, result: route(100), progress_revision: 1, search_progress: { forward: [1] } },
    { ...queued, status: 'running', revision: 2, result: route(90), progress_revision: 2, search_progress: { forward: [2] } },
    { ...queued, status: 'completed', revision: 3, result: route(80), progress_revision: 3, search_progress: null },
  ]
  await pollRouteJob({ create: async () => queued, get: async () => snapshots.shift(), cancel: async () => {},
    initialDisplayDelayMs: 30000, intervalMs: 30000, now: () => clock, wait: async ms => { clock += ms },
    onCreated: () => () => { released = true }, onProgress: () => { published = true },
    onSearchProgress: value => { if (published) assert.equal(value, null); progress.push(value) },
  })
  assert.ok(progress.some(value => value?.forward))
  assert.ok(published && released)
})

test('search progress has independent revisions, no official route publication, and terminal cleanup', async () => {
  const search = [], publications = [], queried = []
  let count = 0
  const result = await pollRouteJob({
    create: async () => ({ ...queued, progress_revision: 1, search_progress: { forward: [{ points: [[0, 0], [1, 0]] }] } }),
    get: async (id, revision, signal, progressRevision) => {
      queried.push([revision, progressRevision])
      count += 1
      return count === 1 ? { ...queued, status: 'running', progress_revision: 1, search_progress: null }
        : { ...queued, status: 'completed', revision: 1, progress_revision: 2, search_progress: null, result: route(100) }
    },
    onSearchProgress: value => search.push(value), onProgress: value => publications.push(value),
    cancel: async () => {}, wait: async () => {}, initialDisplayDelayMs: 30000,
  })
  assert.equal(publications.length, 1)
  assert.equal(result.distance_km, 100)
  assert.ok(search[0].forward)
  assert.equal(search.at(-1), null)
  assert.deepEqual(queried, [[-1, 1], [-1, 1]])
})
const state = (revision, distance, status = 'running') => ({ ...queued, revision, status, result: distance === null ? null : route(distance), message: status })

test('unverified temporal geometry cannot replace a verified route or become a preview', async () => {
  const verified = { ...route(100), time_aware: true, analysis: { navigation_allowed: true } }
  const reference = { ...route(20), time_aware: true, reference_route: true, analysis: { navigation_allowed: false } }
  const updates = []
  const result = await pollRouteJob({
    create: async () => ({ ...state(1, 100), result: verified }),
    get: async () => ({ ...state(2, 20, 'completed'), result: reference }),
    onProgress: candidate => updates.push(candidate), cancel: async () => {}, wait: async () => {},
  })
  assert.deepEqual(updates, [verified])
  assert.equal(result.distance_km, 100)
})

test('missing weather diagnostics return without drawing a reference preview', async () => {
  const diagnostic = { time_aware: true, points: [], route_status: 'data_insufficient', planning_message: '缺少小时风场' }
  const result = await pollRouteJob({
    create: async () => ({ ...state(1, null, 'completed'), result: diagnostic }),
    onProgress: () => assert.fail('diagnostic must not be drawn'), cancel: async () => {},
  })
  assert.deepEqual(result.points, [])
  assert.equal(result.route_status, 'data_insufficient')
  assert.equal(result.planning_message, '缺少小时风场')
})

test('completed reference routes retain their explanation instead of a generic completion message', async () => {
  const reference = { ...route(100), reference_route: true, planning_message: '预报不足，显示参考线' }
  const result = await pollRouteJob({
    create: async () => ({ ...state(1, 100, 'completed'), result: reference }),
    get: async () => { throw new Error('must not poll a completed job') }, cancel: async () => {},
  })
  assert.equal(result.planning_message, reference.planning_message)
})

test('renders the first route before optimization finishes and replaces it on revision changes', async () => {
  const updates = []
  const snapshots = [state(1, 100), state(1, null), state(2, 90), state(2, null, 'completed')]
  let cancelled = false
  const result = await pollRouteJob({
    create: async () => queued,
    get: async (_, revision) => {
      if (snapshots.length === 3) assert.deepEqual(updates, [100])
      if (snapshots.length === 1) assert.equal(revision, 2)
      return snapshots.shift()
    },
    onProgress: (candidate) => updates.push(candidate.distance_km),
    cancel: async () => { cancelled = true }, wait: async () => {},
  })
  assert.deepEqual(updates, [100, 90])
  assert.equal(result.distance_km, 90)
  assert.equal(result.planning_status, 'completed')
  assert.equal(cancelled, false)
})

for (const status of ['timed_out', 'failed']) {
  test(`retains the displayed route when optimization is ${status}`, async () => {
    const result = await pollRouteJob({
      create: async () => state(1, 100),
      get: async () => state(1, null, status), cancel: async () => {}, wait: async () => {},
    })
    assert.equal(result.distance_km, 100)
    assert.equal(result.planning_status, status)
  })
  test(`reports ${status} if no route has been found`, async () => {
    await assert.rejects(pollRouteJob({
      create: async () => queued, get: async () => state(0, null, status),
      cancel: async () => {}, wait: async () => {},
    }), new RegExp(status))
  })
}

test('aborting prevents later results and cancels the server job', async () => {
  const controller = new AbortController()
  const updates = []
  let cancelled
  await assert.rejects(pollRouteJob({
    create: async () => state(1, 100),
    signal: controller.signal,
    onProgress: (result) => updates.push(result.distance_km),
    get: async () => { throw new Error('must not poll after abort') },
    wait: async () => { controller.abort() },
    cancel: async (jobId) => { cancelled = jobId },
  }), { name: 'AbortError' })
  assert.deepEqual(updates, [100])
  assert.equal(cancelled, 'test')
})

test('brief polling failures preserve the candidate and retry', async () => {
  let attempts = 0
  const updates = []
  const result = await pollRouteJob({
    create: async () => state(1, 100),
    onProgress: (candidate) => updates.push(candidate.distance_km),
    get: async () => {
      if (++attempts === 1) throw new Error('network disconnected')
      return state(2, 90, 'completed')
    },
    wait: async () => {}, cancel: async () => {},
  })
  assert.equal(attempts, 2)
  assert.equal(result.distance_km, 90)
  assert.deepEqual(updates, [100, 90])
})

test('client deadline retains the best candidate and stops the worker', async () => {
  let clock = 0
  let cancelled = false
  const result = await pollRouteJob({
    create: async () => state(1, 100), get: async () => state(1, null),
    now: () => clock, wait: async () => { clock = 700000 },
    cancel: async () => { cancelled = true },
  })
  assert.equal(result.distance_km, 100)
  assert.equal(result.planning_status, 'timed_out')
  assert.equal(cancelled, true)
})

test('server two-hour budget keeps polling beyond the former eleven-minute deadline', async () => {
  let clock = 0
  let cancelled = false
  let polls = 0
  const updates = []
  const result = await pollRouteJob({
    create: async () => ({ ...state(1, 100), timeout_seconds: 7200 }),
    get: async () => {
      polls++
      return { ...state(polls === 1 ? 1 : 2, polls === 1 ? null : 90, polls === 1 ? 'running' : 'completed'), timeout_seconds: 7200 }
    },
    initialDisplayDelayMs: 30000, now: () => clock,
    wait: async () => { clock += 900000 },
    onProgress: candidate => updates.push(candidate.distance_km),
    cancel: async () => { cancelled = true },
  })
  assert.equal(clock, 1800000)
  assert.equal(result.planning_status, 'completed')
  assert.equal(result.distance_km, 90)
  assert.deepEqual(updates, [100, 90])
  assert.equal(cancelled, false)
})

test('30 second publication shows the initial best and final best without intervening replacements', async () => {
  let clock = 0
  const updates = []
  const snapshots = [state(1, 100), state(2, 90), state(3, 80), state(4, 70), state(5, 60), state(6, 50, 'completed')]
  const result = await pollRouteJob({
    create: async () => queued, get: async () => snapshots.shift(),
    initialDisplayDelayMs: 30000, intervalMs: 10000, now: () => clock,
    wait: async (delay) => { clock += delay }, cancel: async () => {},
    onProgress: candidate => updates.push([clock, candidate.distance_km]),
  })
  assert.deepEqual(updates, [[30000, 80], [60000, 50]])
  assert.equal(result.distance_km, 50)
})

test('optimization completing within 30 seconds publishes its final route immediately', async () => {
  let clock = 0
  const updates = []
  const result = await pollRouteJob({
    create: async () => state(1, 100), get: async () => state(2, 80, 'completed'),
    initialDisplayDelayMs: 30000, now: () => clock, wait: async () => { clock = 4000 }, cancel: async () => {},
    onProgress: candidate => updates.push([clock, candidate.distance_km]),
  })
  assert.deepEqual(updates, [[4000, 80]])
  assert.equal(result.distance_km, 80)
})

test('a first route found after 30 seconds appears immediately then waits for the final result', async () => {
  let clock = 0
  const updates = []
  const snapshots = [state(1, 100), state(2, 90), state(3, 80, 'completed')]
  await pollRouteJob({
    create: async () => queued, get: async () => snapshots.shift(),
    initialDisplayDelayMs: 30000, now: () => clock, wait: async () => { clock += 40000 }, cancel: async () => {},
    onProgress: candidate => updates.push([clock, candidate.distance_km]),
  })
  assert.deepEqual(updates, [[40000, 100], [120000, 80]])
})

test('a timeout before the initial publication still returns the best verified candidate', async () => {
  const updates = []
  const result = await pollRouteJob({
    create: async () => state(1, 100), get: async () => state(2, 90, 'timed_out'),
    initialDisplayDelayMs: 30000, now: () => 0, wait: async () => {}, cancel: async () => {},
    onProgress: candidate => updates.push(candidate.distance_km),
  })
  assert.deepEqual(updates, [90])
  assert.equal(result.planning_status, 'timed_out')
})

test('cancelling before 30 seconds cannot publish a delayed preview', async () => {
  const controller = new AbortController()
  let clock = 0, cancelled = false
  await assert.rejects(pollRouteJob({
    create: async () => state(1, 100), get: async () => assert.fail('cancelled jobs do not poll'),
    signal: controller.signal, initialDisplayDelayMs: 30000, now: () => clock,
    wait: async () => { clock = 40000; controller.abort() },
    onProgress: () => assert.fail('cancelled jobs do not publish'), cancel: async () => { cancelled = true },
  }), { name: 'AbortError' })
  assert.equal(cancelled, true)
})

test('verified candidates enter the page cache before publication without replacing a restored route',async()=>{
  let clock=0
  const candidates=[],published=[]
  const snapshots=[state(2,90),state(3,80,'completed')]
  await pollRouteJob({create:async()=>state(1,100),get:async()=>snapshots.shift(),cancel:async()=>{},
    hasInitialResult:true,initialDisplayDelayMs:30000,now:()=>clock,wait:async()=>{clock+=31000},
    onCandidate:value=>candidates.push(value.distance_km),onProgress:value=>published.push(value.distance_km),
    onSearchProgress:value=>assert.equal(value,null)})
  assert.deepEqual(candidates,[100,90,80])
  assert.deepEqual(published,[80])
})
