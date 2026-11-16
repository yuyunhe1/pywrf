import assert from 'node:assert/strict'
import test from 'node:test'
import { createTaskOwner } from './taskOwner.js'

function fixture() {
  const listeners = new Map(), sockets = [], requests = [], timers = []
  const target = { location: { protocol: 'https:', host: 'example.test' },
    addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }
  class Socket {
    constructor(url) { this.url = url; this.messages = []; sockets.push(this) }
    send(value) { this.messages.push(JSON.parse(value)) }
    close() { this.closed = true; this.onclose?.() }
  }
  let id = 0
  const owner = createTaskOwner({ target, Socket, uuid: () => `page-${++id}`,
    fetcher: (url, init) => { requests.push([url, init]); return Promise.resolve() },
    schedule: fn => { timers.push(fn); return fn }, unschedule: () => {} })
  return { owner, listeners, sockets, requests, timers }
}

test('owner connection acknowledges server messages without visibility timers', () => {
  const f = fixture()
  const release = f.owner.attach('job-A')
  const socket = f.sockets[0]
  socket.onopen()
  assert.equal(socket.url, 'wss://example.test/api/route/plan/jobs/job-A/owner')
  assert.deepEqual(socket.messages[0], { client_instance_id: 'page-1' })
  socket.onmessage({ data: JSON.stringify({ type: 'heartbeat', seq: 3 }) })
  assert.deepEqual(socket.messages[1], { type: 'ack', seq: 3 })
  assert.equal(f.timers.length, 0)
  assert.equal(f.listeners.has('visibilitychange'), false)
  release()
  assert.ok(socket.closed)
})

test('pagehide uses keepalive cancellation only for jobs owned by this page', () => {
  const f = fixture()
  f.owner.attach('job-A'); f.owner.attach('job-B')
  f.listeners.get('pagehide')()
  assert.equal(f.requests.length, 2)
  assert.ok(f.requests.every(([, options]) => options.method === 'DELETE' && options.keepalive))
  assert.ok(f.sockets.every(s => s.closed))
  assert.equal(f.timers.length, 0)
})

test('a late creation after leaving is cancelled and bfcache return gets a new owner', () => {
  const f = fixture()
  f.listeners.get('pagehide')()
  f.owner.attach('late-job')
  assert.equal(f.requests[0][0], '/api/route/plan/jobs/late-job')
  assert.equal(f.sockets.length, 0)
  f.listeners.get('pageshow')()
  assert.equal(f.owner.clientId, 'page-2')
  f.owner.attach('new-job')
  assert.equal(f.sockets.length, 1)
})

test('temporary connection failure reconnects without cancelling the job', () => {
  const f = fixture()
  const release = f.owner.attach('job-A')
  f.sockets[0].close()
  assert.equal(f.requests.length, 0)
  f.timers.shift()()
  assert.equal(f.sockets.length, 2)
  release()
  assert.equal(f.requests.length, 0)
})

test('recommendation ownership and pagehide cancellation use their own endpoint', () => {
  const f=fixture()
  f.owner.attach('recommend-a','/api/route/recommendation/jobs')
  f.sockets[0].onopen()
  assert.equal(f.sockets[0].url,'wss://example.test/api/route/recommendation/jobs/recommend-a/owner')
  f.listeners.get('pagehide')()
  assert.equal(f.requests[0][0],'/api/route/recommendation/jobs/recommend-a')
  assert.equal(f.requests[0][1].keepalive,true)
})

test('pagehide also cancels a pending creation and forgets page-owned payloads', () => {
  const f=fixture()
  assert.equal(f.owner.nextSequence(),1)
  assert.equal(f.owner.nextSequence(),2)
  f.listeners.get('pagehide')()
  assert.equal(f.requests.length,1)
  assert.equal(f.requests[0][0],'/api/route/planning-clients/page-1?through=2&forget=true')
  assert.deepEqual(f.requests[0][1],{method:'DELETE',keepalive:true})
  f.listeners.get('pageshow')()
  assert.equal(f.owner.clientId,'page-2')
  assert.equal(f.owner.nextSequence(),1)
})
