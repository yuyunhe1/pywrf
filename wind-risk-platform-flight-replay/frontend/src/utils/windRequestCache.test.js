import test from 'node:test'
import assert from 'node:assert/strict'
import { createWindRequestCache } from './windRequestCache.js'

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }

test('weather LRU evicts by memory and count without retaining an oversized frame', async () => {
  const cache = createWindRequestCache({ maxBytes: 8, maxEntries: 2, sizeOf: (value) => value.length })
  let loads = 0
  const load = async () => { loads++; return '1234' }
  await cache.get('a', load); await cache.get('b', load); await cache.get('a', load)
  await cache.get('c', load)
  assert.equal(loads, 3)
  assert.equal(cache.stats().bytes, 8)
  await cache.get('b', load)
  assert.equal(loads, 4)
  await cache.get('large', async () => '123456789')
  await cache.get('large', async () => '123456789')
  assert.equal(cache.stats().entries, 2)
  assert.equal(cache.stats().bytes, 8)
})

test('concurrent readers share one fetch and one cancellation leaves other readers active', async () => {
  const cache = createWindRequestCache()
  let finish, loads = 0, sharedSignal
  const loader = (signal) => { loads++; sharedSignal = signal; return new Promise((resolve) => { finish = resolve }) }
  const one = new AbortController(), two = new AbortController()
  const first = cache.get('frame', loader, { signal: one.signal }).catch((error) => error.name)
  const second = cache.get('frame', loader, { signal: two.signal })
  await flush()
  one.abort()
  assert.equal(await first, 'AbortError')
  assert.equal(sharedSignal.aborted, false)
  finish({ ok: true })
  assert.deepEqual(await second, { ok: true })
  assert.equal(loads, 1)
})

test('one speculative fetch leaves capacity for the visible frame and queued foreground work goes first', async () => {
  const cache = createWindRequestCache({ concurrency: 2 })
  const started = [], finishes = new Map()
  const load = (name) => () => new Promise((resolve) => { started.push(name); finishes.set(name, resolve) })
  const p1 = cache.get('p1', load('p1'), { priority: 0 })
  const p2 = cache.get('p2', load('p2'), { priority: 0 })
  const shown = cache.get('visible', load('visible'))
  await flush()
  assert.deepEqual(started, ['p1', 'visible'])
  assert.equal(cache.stats().active, 2)
  finishes.get('visible')({}); await shown; await flush()
  assert.equal(started.length, 2)
  finishes.get('p1')({}); await p1; await flush()
  assert.deepEqual(started, ['p1', 'visible', 'p2'])
  finishes.get('p2')({}); await p2
})

test('last reader cancellation aborts transport and a late response cannot populate a cleared cache', async () => {
  const cache = createWindRequestCache()
  const reader = new AbortController()
  let signal, finish
  const pending = cache.get('old', (value) => { signal = value; return new Promise((resolve) => { finish = resolve }) },
    { signal: reader.signal }).catch((error) => error.name)
  await flush()
  reader.abort()
  assert.equal(await pending, 'AbortError')
  assert.equal(signal.aborted, true)
  finish({ old: true }); await flush()
  assert.equal(cache.stats().entries, 0)
  assert.equal(cache.stats().active, 0)
})

test('forecast invalidation clears snapshots while keeping immutable replay frames', async () => {
  const cache = createWindRequestCache()
  await cache.get('snapshot:wrf:old', async () => ({}))
  await cache.get('replay:hash:time', async () => ({}))
  cache.invalidate((key) => key.startsWith('snapshot:wrf:'))
  assert.equal(cache.stats().entries, 1)
  assert.deepEqual(await cache.get('replay:hash:time', () => { throw Error('must be cached') }), {})
})
