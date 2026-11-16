import test from 'node:test'
import assert from 'node:assert/strict'
import { pollRecommendationJob } from './recommendations.js'
import { createCustomRouteDrawingController } from './customRouteDrawing.js'

const route = { points: [[1,2],[3,4]], analysis: { navigation_allowed: true } }

test('a forecast change invalidates a retained route before terminal publication',async()=>{
  let candidates=0,publications=0
  await assert.rejects(pollRecommendationJob({initialDisplayDelayMs:0,
    create:async()=>({job_id:'range',status:'running',revision:1,result:route,workflow_revision:1,
      workflow:{mode:'departure_range',stage:'range_search'}}),
    get:async()=>({job_id:'range',status:'cancelled',revision:2,result:route,workflow_revision:2,
      workflow:{mode:'departure_range',stage:'forecast_changed'}}),
    cancel:async()=>{},wait:async()=>{},onCandidate:()=>candidates++,onProgress:()=>publications++
  }),{name:'AbortError'})
  assert.equal(candidates,1)
  assert.equal(publications,1)
})
test('future recommendations never auto-publish an official route', async () => {
  let published = 0, latest
  const workflow = { stage: 'future_search', recommendations: [{ id: 'future', first_departure_at: '2026-11-08T09:00Z' }] }
  const result = await pollRecommendationJob({ create: async () => ({ job_id:'test', status:'completed', revision:0,
    workflow_revision:1,workflow }), get: async () => {}, cancel: async () => {},
    onProgress: () => published++, onWorkflow: value => { latest = value } })
  assert.equal(published, 0)
  assert.equal(result.result, undefined)
  assert.deepEqual(latest, workflow)
})

test('specified route retains 30 second and final publication cadence', async () => {
  let time = 0, polls = 0
  const publications = []
  const result = await pollRecommendationJob({ now: () => time, intervalMs:10000,
    create: async () => ({ job_id:'test',status:'running',revision:1,result:route }),
    get: async () => ++polls < 4 ? { status:'running',revision:2,result:{ ...route, cost:1 } }
      : { status:'completed',revision:3,result:{ ...route, cost:0 } },
    cancel: async () => {}, wait: async ms => { time += ms }, onProgress: value => publications.push([time,value.cost]) })
  assert.deepEqual(publications, [[30000,1],[40000,0]])
  assert.equal(result.result.cost,0)
})

test('older workflow revisions cannot restore stale recommendations', async () => {
  const workflows = []
  await pollRecommendationJob({ create:async () => ({ job_id:'a',status:'running',workflow_revision:3,
    workflow:{ recommendations:[] } }), get:async () => ({ status:'completed',workflow_revision:2,workflow:{ recommendations:[{id:'old'}] } }),
    cancel:async () => {}, wait:async () => {}, onWorkflow: value => workflows.push(value) })
  assert.deepEqual(workflows,[{recommendations:[]}])
})

test('completing a drawing evaluates ordered geometry without saving it', async () => {
  let saved = 0, evaluated, state
  const drawing = createCustomRouteDrawingController({ save:async () => { saved++ }, onChange:value => { state=value },
    onComplete:async payload => { evaluated=payload } })
  drawing.begin();drawing.add([117,30]);drawing.add([118,31]);drawing.move(1,[118.1,31])
  await drawing.complete({source:'wrf',level:'100m AGL'})
  assert.equal(saved,0)
  assert.deepEqual(evaluated.points,[[117,30],[118.1,31]])
  assert.equal(state.active,false)
})

test('a failed drawing evaluation keeps its draft editable and retryable', async () => {
  let state
  const drawing = createCustomRouteDrawingController({ onChange:value => { state=value }, onComplete:async () => { throw new Error('network') } })
  drawing.begin();drawing.add([117,30]);drawing.add([118,31])
  await drawing.complete({source:'gfs',level:'100m AGL'})
  assert.equal(state.active,true)
  assert.equal(state.saving,false)
  assert.equal(state.error,'network')
  assert.equal(state.points.length,2)
})
