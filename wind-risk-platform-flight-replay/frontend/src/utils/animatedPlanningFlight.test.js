import test from 'node:test'
import assert from 'node:assert/strict'
import { createPlanningDemoController, createAnimatedPlanningFlight } from './planningDemo.js'

const at = minutes => new Date(Date.parse('2026-11-15T00:00:00Z') + minutes * 60000).toISOString()
const level = '100m AGL', model = 'NATIVE_WRF_TEN_MINUTE_HOLD_V1'
const arc = (a, b) => {
  const r = Math.PI / 180
  return 12742000 * Math.asin(Math.sqrt(Math.sin((b[1]-a[1])*r/2)**2
    + Math.cos(a[1]*r)*Math.cos(b[1]*r)*Math.sin((b[0]-a[0])*r/2)**2))
}
const route = (turn = .04, cost = 100) => {
  const points = [[0,0],[.04,0],[.04,turn],[.08,.04]]
  const duration = points.slice(1).reduce((sum,p,i)=>sum+arc(points[i],p)/10,0)
  return { points, objective_cost: cost, objective_model: 'SUPERCOVER_WIND_EXPOSURE_V5', dataset_id: 'frozen',
    time_aware: true, departure_at: at(3), level, average_ground_speed_mps: 10, analysis: { navigation_allowed: true },
    flight_profile: { dataset_id: 'frozen', level, departure_at: at(3), flight_duration_seconds: duration,
      average_ground_speed_mps: 10, frame_times: Array.from({length:20},(_,i)=>at(i*10)), wind_time_model:model,
      analysis: { samples: points.map((p,i)=>({lon:p[0],lat:p[1],elapsed_seconds:i*300})) } } }
}
const wind = time => ({available:true,metadata:{dataset_id:'frozen',level,cycle:at(0),valid_time:time,
  wind_time_model:model,wind_time_step_seconds:600},velocity:[2,0].map(value=>({
    header:{nx:2,ny:2,lo1:0,la1:1,dx:1,dy:1},data:Array(4).fill(value)}))})
const flush = async () => { for(let i=0;i<40;i++) await Promise.resolve() }
const setup = (options={}) => {
  let clock=0, sequence=0, state, current=true, finishCalls=0, plays=0, animation
  const timers=new Map(), requests=[], paints=[], published=[]
  const demo=createPlanningDemoController({requestFrame:async(_id,time)=>{requests.push(time);return wind(time)},
    onChange:value=>{state=value},onComplete:()=>animation.drawn(),
    setTimer:(run,ms)=>{assert.equal(ms,2000);timers.set(++sequence,{run,at:clock+ms});return sequence},clearTimer:id=>timers.delete(id)})
  animation=createAnimatedPlanningFlight({isCurrent:()=>current,show:(value,options)=>demo.show(value,options),
    stopPreview:()=>demo.dismiss(),publish:value=>published.push(value),play:()=>plays++,...options})
  animation.registerFinish(async()=>{finishCalls++})
  const ack=()=>{if(state.pendingMapKey){demo.acknowledgeMap(state.pendingMapKey);paints.push({at:clock,index:state.index,profile:state.profile})}}
  const tick=async()=>{
    const [id,task]=[...timers].sort((a,b)=>a[1].at-b[1].at)[0]
    timers.delete(id);clock=task.at;task.run();await flush();ack();await flush()
  }
  const drain=async()=>{for(let i=0;timers.size&&i<30;i++) await tick()}
  return {animation,demo,timers,requests,paints,published,state:()=>state,ack,tick,drain,
    clock:()=>clock,finishCalls:()=>finishCalls,plays:()=>plays,obsolete:()=>{current=false;animation.cancel()}}
}

test('departure-range drawing completion keeps comparing and accepts a better later departure',async()=>{
  const app=setup({compareDepartureRange:true})
  const first={...route(),mode:'departure_range',selection_revision:1,departure_start_at:at(0)}
  const later={...route(.05,200),mode:'departure_range',selection_revision:2,departure_start_at:at(0),departure_at:at(33)}
  later.flight_profile={...later.flight_profile,departure_at:later.departure_at}
  try{
    app.animation.receive(first);await flush();app.ack();await app.drain()
    assert.equal(app.finishCalls(),0)
    assert.equal(app.plays(),0)
    assert.ok(app.published.length)
    assert.equal(app.animation.receive(later),true)
    assert.equal(app.animation.receive(first),false)
    await flush();app.ack();await app.drain()
    assert.equal(app.finishCalls(),0)
    await app.animation.terminal(later)
    assert.equal(app.plays(),1)
    assert.equal(app.published.at(-1).departure_at,at(33))
  }finally{app.demo.dispose()}
})

test('an expired range recommendation can be invalidated without replaying the old route',async()=>{
  const app=setup({compareDepartureRange:true})
  try{
    app.animation.receive({...route(),mode:'departure_range',selection_revision:1,departure_start_at:at(0)})
    await flush();app.ack()
    app.animation.invalidate()
    assert.equal(app.animation.hasCandidate,false)
    await app.animation.terminal()
    assert.equal(app.plays(),0)
    assert.equal(app.finishCalls(),0)
  }finally{app.demo.dispose()}
})

test('manually leaving range drawing for the chart does not stall terminal playback',async()=>{
  const app=setup({compareDepartureRange:true})
  const first={...route(),mode:'departure_range',selection_revision:1,departure_start_at:at(0)}
  try{
    app.animation.receive(first);await flush();app.ack()
    app.animation.drawn();app.demo.dismiss()
    await app.animation.terminal(first)
    assert.equal(app.finishCalls(),0)
    assert.equal(app.plays(),1)
  }finally{app.demo.dispose()}
})

test('two-second segments coalesce improvements and redraw from the first changed leg, then freeze and replay',async()=>{
  const app=setup(), first=route(), second=route(.06,90), best=route(.05,80)
  try{
    app.animation.receive(first);await flush();app.ack()
    assert.equal(app.state().index,0)
    app.animation.receive(second);app.animation.receive(best)
    assert.equal(app.state().profile,first.flight_profile)
    assert.equal(app.requests.length,1)
    await app.tick()
    assert.equal(app.clock(),2000)
    assert.equal(app.state().index,1)
    assert.equal(app.state().profile,best.flight_profile)
    assert.equal(app.requests.filter(time=>time===at(0)).length,1)
    await app.drain()
    assert.equal(app.finishCalls(),1)
    assert.equal(app.published.length,0) // Await the server's frozen final result.
    await app.animation.terminal({...best,planning_message:'comparison complete'})
    assert.equal(app.published.length,1)
    assert.equal(app.plays(),1)
    assert.deepEqual(app.published[0].points,best.points)
    for(let i=1;i<app.paints.length;i++) assert.ok(app.paints[i].at-app.paints[i-1].at>=2000)
    app.animation.receive(route(.08,70))
    assert.equal(app.plays(),1)
  }finally{app.demo.dispose()}
})

test('a newly improved frozen result redraws its changed portion before replay, without requesting a second finish',async()=>{
  const app=setup(), first=route(), late=route(.06,70)
  try{
    app.animation.receive(first);await flush();app.ack();await app.drain()
    assert.equal(app.finishCalls(),1)
    const complete=app.animation.terminal(late)
    await flush();app.ack()
    assert.equal(app.state().index,1)
    assert.equal(app.plays(),0)
    await app.drain();await complete
    assert.equal(app.finishCalls(),1)
    assert.equal(app.plays(),1)
    assert.deepEqual(app.published[0].points,late.points)
  }finally{app.demo.dispose()}
})

test('early backend completion still displays every segment and holds the final leg for two seconds',async()=>{
  const app=setup(), first=route()
  try{
    app.animation.drawn() // A completed local prefix cannot finish an unverified voyage.
    assert.equal(app.animation.hasCandidate,false)
    assert.equal(app.finishCalls(),0)
    const complete=app.animation.terminal(first)
    await flush();app.ack()
    assert.equal(app.plays(),0)
    await app.drain();await complete
    assert.equal(app.finishCalls(),0)
    assert.equal(app.plays(),1)
    assert.equal(app.clock(),app.paints.length*2000)
  }finally{app.demo.dispose()}
})

test('same geometry, worse scores, mixed forecasts and obsolete tasks cannot restart the animation',async()=>{
  const app=setup(), first=route()
  try{
    app.animation.receive(first);await flush();app.ack()
    const timers=[...app.timers.keys()]
    app.animation.receive({...first,objective_cost:90})
    app.animation.receive(route(.07,110))
    app.animation.receive({...route(.07,70),dataset_id:'other'})
    assert.deepEqual([...app.timers.keys()],timers)
    assert.equal(app.requests.length,1)
    app.obsolete()
    assert.equal(app.timers.size,0)
    app.animation.receive(route(.07,70))
    await app.animation.done
    assert.equal(app.plays(),0)
    assert.equal(app.published.length,0)
  }finally{app.demo.dispose()}
})
