import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { assessRouteQuality, isDisplayableRoute, qualityRetryLabel } from './routeQuality.js'
import { isPlanningDemoCandidate,createAnimatedPlanningFlight,createPlanningSearchPreview } from './planningDemo.js'
import { createPageRouteCache } from './pagePlanning.js'

const user=JSON.parse(readFileSync(new URL('../../../backend/tests/fixtures/route_quality_user_20261115.json',import.meta.url),'utf8'))
const points=user.points
const raw={...user,time_aware:true,analysis:{navigation_allowed:true},flight_profile:{...user,
  analysis:{samples:[{lon:points[0][0],lat:points[0][1]},{lon:points.at(-1)[0],lat:points.at(-1)[1]}]}}}

test('the captured user route cannot be drawn, replayed, cached or used as a displayed prefix',()=>{
  assert.equal(isDisplayableRoute(raw),false)
  assert.equal(isPlanningDemoCandidate(raw),false)
  let drawings=0
  const flight=createAnimatedPlanningFlight({isCurrent:()=>true,show:()=>drawings++,stopPreview:()=>{},publish:()=>drawings++,play:()=>drawings++})
  assert.equal(flight.receive(raw),false)
  const preview=createPlanningSearchPreview({isCurrent:()=>true,isPublished:()=>false,start:points[0],source:user.source,level:user.level,show:()=>drawings++})
  preview({preview_context:{...user,frame_times:[]},forward:[{points}]})
  assert.equal(drawings,0)
  const payload={...user,start:points[0],end:points.at(-1),prefer_simple_route:true}
  const version={forecast_revision:'frozen',planning_runtime_id:'test'}
  const cache=createPageRouteCache()
  cache.put(payload,version,raw)
  assert.equal(cache.stats().entries,0)
  flight.cancel();cache.dispose()
})

test('spirals, crossings and reversals are hidden without rejecting ordinary weather bypasses',()=>{
  const spiral=Array.from({length:49},(_,i)=>[(.025-i*.012/48)*Math.cos(i*Math.PI/12),(.025-i*.012/48)*Math.sin(i*Math.PI/12)])
  for(const route of [spiral,[[0,0],[.03,0],[.01,.001],[.06,.03]],[[0,0],[.04,.04],[0,.04],[.04,0],[.08,0]]]) assert.equal(assessRouteQuality(route).acceptable,false)
  for(const route of [[[0,0],[.04,.08],[.14,.08],[.2,0]],[[0,0],[.001,.001],[.002,.0005],[.003,.0015]],[[0,0],[.01,0],[.00999999999,0],[.02,0]]]) assert.equal(assessRouteQuality(route).acceptable,true)
})

test('an old loopy cache entry is excluded from simple-route seeds',()=>{
  const payload={...user,start:points[0],end:points.at(-1)},version={forecast_revision:'frozen',planning_runtime_id:'test'}
  const cache=createPageRouteCache()
  cache.put(payload,version,raw)
  assert.equal(cache.stats().entries,1)
  assert.deepEqual(cache.seeds({...payload,prefer_simple_route:true},version),[])
  cache.dispose()
})

test('the existing status shows remaining extra search only while it is active',()=>{
  const state={status:'running',route_quality:{status:'searching_alternative',remaining_seconds:9.7}}
  assert.match(qualityRetryLabel(state),/剩余 10 秒/)
  assert.equal(qualityRetryLabel({...state,status:'cancelled'}),undefined)
  assert.equal(qualityRetryLabel({...state,route_quality:{...state.route_quality,remaining_seconds:0}}),undefined)
  assert.equal(qualityRetryLabel({...state,route_quality:{status:'resolved'}}),undefined)
})
