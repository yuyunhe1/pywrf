import test from 'node:test'
import assert from 'node:assert/strict'
import {createPageRouteCache,createPagePlanningGate} from './pagePlanning.js'

const payload={source:'wrf',level:'100m AGL',cycle:'2026-11-10 06:00 UTC',valid_time:'2026-11-10 21:00 UTC',
  start:[118,30],end:[119,31],planner_type:'wa_lpa_star',aircraft_model:'fixed_wing',planning_strategy:'wind_avoidance',
  thresholds:{safe:1.5,danger:7.9},wind_shear:{enabled:true},average_ground_speed_mps:10,departure_at:'2026-11-11T01:00:00Z'}
const version={forecast_revision:'files-1',planning_runtime_id:'server-1'}
const result={points:[[118,30],[118.3,30.6],[119,31]],source:'wrf',level:'100m AGL',dataset_id:'snapshot',
  analysis:{navigation_allowed:true,samples:Array(100).fill({lon:118,lat:30})},flight_profile:{large:'profile'},
  planning_diagnostics:{large:Array(1000).fill(1)},segments:[{large:'segments'}]}

test('page route cache separates all flight, strategy and weather identities',()=>{
  const cache=createPageRouteCache()
  cache.put(payload,version,result)
  assert.deepEqual(cache.get(payload,version).points,result.points)
  for (const changes of [{start:[118.01,30]},{end:[119.01,31]},{source:'gfs'},{level:'200m AGL'},
    {planning_strategy:'distance_priority'},{planner_type:'astar'},{average_ground_speed_mps:50},
    {departure_at:'2026-11-11T02:00:00Z'},{thresholds:{safe:1.5,danger:7.8}}]) {
    assert.equal(cache.get({...payload,...changes},version),undefined)
  }
  assert.equal(cache.get(payload,{...version,forecast_revision:'files-2'}),undefined)
  assert.equal(cache.get(payload,{...version,planning_runtime_id:'server-2'}),undefined)
})

test('cache keeps only immutable geometry and offers related strategies as validation seeds',()=>{
  const cache=createPageRouteCache()
  cache.put(payload,version,result)
  const read=cache.get(payload,version)
  assert.equal(read.flight_profile,undefined)
  assert.equal(read.analysis,undefined)
  assert.equal(read.planning_diagnostics,undefined)
  read.points[0][0]=5
  assert.equal(cache.get(payload,version).points[0][0],118)
  assert.deepEqual(cache.seeds({...payload,planning_strategy:'distance_priority',departure_at:null},version),[result.points])
  assert.deepEqual(cache.seeds({...payload,level:'200m AGL'},version),[])
  assert.deepEqual(cache.seeds(payload,{...version,forecast_revision:'new'}),[])
})

test('first-solution runs cannot be short-circuited by a different cached strategy or algorithm',()=>{
  const cache=createPageRouteCache()
  cache.put(payload,version,{...result,planning_strategy:payload.planning_strategy,planner_type:payload.planner_type})
  const first={...payload,stop_on_first_feasible:true,departure_at:null}
  assert.deepEqual(cache.seeds(first,version),[result.points])
  assert.deepEqual(cache.seeds({...first,planning_strategy:'distance_priority'},version),[])
  assert.deepEqual(cache.seeds({...first,planner_type:'astar'},version),[])
  assert.deepEqual(cache.seeds({...first,stop_on_first_feasible:false,optimize_during_display:true,planning_strategy:'distance_priority'},version),[])
  cache.dispose()
})

test('pagehide releases the entire cache and rejects late writes from the previous page epoch',()=>{
  const listeners=new Map()
  const cache=createPageRouteCache({target:{addEventListener:(key,fn)=>listeners.set(key,fn),removeEventListener:key=>listeners.delete(key)}})
  const epoch=cache.epoch
  cache.put(payload,version,result,epoch)
  listeners.get('pagehide')()
  cache.put(payload,version,result,epoch)
  assert.equal(cache.stats().entries,0)
  assert.equal(cache.stats().bytes,0)
  cache.put(payload,version,result,cache.epoch)
  assert.equal(cache.stats().entries,1)
  cache.dispose()
  assert.equal(listeners.size,0)
})

test('cache is bounded by LRU count and bytes and rejects unverified or mixed-source routes',()=>{
  const cache=createPageRouteCache({maxEntries:2})
  const a=payload,b={...payload,planning_strategy:'balanced'},c={...payload,planning_strategy:'distance_priority'}
  cache.put(a,version,result);cache.put(b,version,result);cache.get(a,version);cache.put(c,version,result)
  assert.equal(cache.get(b,version),undefined)
  assert.ok(cache.get(a,version))
  const small=createPageRouteCache({maxBytes:10})
  small.put(a,version,result)
  assert.equal(small.stats().entries,0)
  for (const invalid of [{...result,analysis:{navigation_allowed:false}},{...result,reference_route:true},
    {...result,source:'gfs'},{...result,level:'200m AGL'}]) {
    const fresh=createPageRouteCache();fresh.put(a,version,invalid);assert.equal(fresh.stats().entries,0)
  }
})

const owner=()=>{let sequence=0;return{clientId:'one-page',nextSequence:()=>++sequence}}
const turn=()=>new Promise(resolve=>setImmediate(resolve))
test('a new planning request waits for old computation release before entering execute',async()=>{
  const events=[]
  let stopped=false
  const gate=createPagePlanningGate({owner:owner(),cancelPage:async(_id,seq)=>{
    events.push(`cancel-${seq}`);return{compute_finished:seq!==1 || stopped}
  },wait:async()=>{events.push('released');stopped=true}})
  const first=gate.run(({signal})=>new Promise((_resolve,reject)=>{
    events.push('create-1');signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})))
  })).catch(reason=>reason.name)
  await turn()
  const second=gate.run(async()=>{events.push('create-2');return'new'})
  assert.equal(await first,'AbortError')
  assert.equal(await second,'new')
  assert.ok(events.indexOf('released')<events.indexOf('create-2'))
})

test('a lost POST response does not block supersession and cannot publish an obsolete completion',async()=>{
  let finishOld
  const cancelled=[]
  const gate=createPagePlanningGate({owner:owner(),cancelPage:async(_id,seq)=>{cancelled.push(seq);return{compute_finished:true}}})
  const old=gate.run(()=>new Promise(resolve=>{finishOld=resolve})).catch(reason=>reason.name)
  await turn()
  assert.equal(await gate.run(async()=> 'current'),'current')
  finishOld('obsolete')
  assert.equal(await old,'AbortError')
  assert.ok(cancelled.includes(1) && cancelled.includes(2))
})
