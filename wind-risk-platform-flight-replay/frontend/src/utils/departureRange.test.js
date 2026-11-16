import test from 'node:test'
import assert from 'node:assert/strict'
import { departureRangeRequest, acceptsRangeRevision } from './departureRange.js'
import { createPageRouteCache } from './pagePlanning.js'

const planner = { departureStartAt:'2026-11-15T16:00', departureEndAt:'2026-11-15T19:00',
  requiredWaypoints:[[1,1],[2,2],[3,3]] }
test('range request keeps middle anchors and uses Beijing minute bounds without a distance cap',()=>{
  const value = departureRangeRequest(planner,[1.1,1],[3,3.1],Date.parse('2026-11-15T07:00Z'))
  assert.equal(value.departure_start_at,'2026-11-15T08:00:00.000Z')
  assert.equal(value.departure_end_at,'2026-11-15T11:00:00.000Z')
  assert.deepEqual(value.required_waypoints,[[1.1,1],[2,2],[3,3.1]])
  assert.equal(value.max_route_distance_km,undefined)
})
test('invalid, reversed, expired and duplicate anchor requests are rejected',()=>{
  for(const p of [{...planner,departureStartAt:''},{...planner,departureEndAt:'2026-11-15T15:00'},
    {...planner,requiredWaypoints:[[1,1],[1,1],[3,3]]}]) {
    assert.throws(()=>departureRangeRequest(p,[1,1],[3,3],Date.parse('2026-11-15T07:00Z')))
  }
  assert.throws(()=>departureRangeRequest(planner,[1,1],[3,3],Date.parse('2026-11-16T00:00Z')))
})
test('range revisions may change departure and increase distance after the peak reference improves',()=>{
  const old={mode:'departure_range',selection_revision:3,departure_at:'a',objective_cost:10}
  assert.ok(acceptsRangeRevision(old,{...old,selection_revision:4,departure_at:'b',objective_cost:20}))
  assert.equal(acceptsRangeRevision(old,{...old,selection_revision:2}),false)
})
test('page cache separates intervals, waypoint order and objective identities',()=>{
  const cache=createPageRouteCache({target:null})
  const version={forecast_revision:'native-sha',planning_runtime_id:'runtime',wind_time_model:'native'}
  const payload={source:'wrf',level:'100m AGL',start:[1,1],end:[3,3],mode:'departure_range',
    required_waypoints:planner.requiredWaypoints,departure_start_at:'a',departure_end_at:'b',selection_model:'peak1'}
  const result={points:[[1,1],[2,2],[3,3]],source:'wrf',level:'100m AGL',analysis:{navigation_allowed:true}}
  cache.put(payload,version,result)
  assert.ok(cache.get(payload,version))
  for(const changes of [{departure_end_at:'c'},{required_waypoints:[[1,1],[2,1],[3,3]]},{selection_model:'old'}]) {
    assert.equal(cache.get({...payload,...changes},version),undefined)
  }
  cache.clear();assert.equal(cache.stats().entries,0)
})
