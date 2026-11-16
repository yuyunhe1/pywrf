import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { baseParse } from '@vue/compiler-dom'
import { createRangeRecommendationSelection,rangeSelectionResult,rangeCardProfileKey } from './rangeRecommendationSelection.js'

const base=Date.parse('2026-11-16T08:00Z')
const at=minute=>new Date(base+minute*60000).toISOString()
const card=(id,minute=Number(id)*10)=>({id:String(id),first_departure_at:at(minute),recommended_departure_at:at(minute),
  max_contact_wind_speed_mps:Number(id),selection_revision:1,
  plan:{points:[[117,30],[117.1,30+Number(id)*.01],[117.2,30]],dataset_id:'native-sha',level:'100m AGL',source:'wrf',
    departure_at:at(minute),average_ground_speed_mps:10,distance_km:20+Number(id),
    route_context:{route_id:`route-${id}`,ephemeral:true}},
  windows:{low_wind_certified:true,dataset_id:'native-sha',peak_wind_limit_mps:4,
    departure_windows:[{start:at(minute),end:at(minute+5)}]}})
const workflow=(cards=[card(1),card(2),card(3)],revision=1)=>({mode:'departure_range',stage:'range_search',dataset_id:'native-sha',
  baseline_version:'draft',departure_start_at:at(0),departure_end_at:at(60),peak_wind_limit_mps:4,
  min_verified_peak_mps:3,selection_revision:revision,recommendations:cards})
const profile=card=>({dataset_id:card.plan.dataset_id,source:card.plan.source,level:card.plan.level,
  departure_at:card.first_departure_at,arrival_at:at(50),average_ground_speed_mps:10,flight_duration_seconds:100,
  analysis:{navigation_allowed:true,max_wind_speed:.5,samples:[{lon:117,lat:30},{lon:117.2,lat:30}]},segments:[]})
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}}
const flush=async()=>{for(let i=0;i<20;i++) await Promise.resolve()}
const setup=(options={})=>{
  const applications=[],requests=[],errors=[],defaults=[],states=[]
  const selector=createRangeRecommendationSelection({now:()=>base,onState:s=>states.push(s),onSelect:r=>applications.push(r),
    requestProfile:async(c,signal)=>{requests.push({card:c,signal});return profile(c)},onError:m=>errors.push(m),
    onDefault:value=>{defaults.push(value);return true},...options})
  selector.update(workflow())
  return{selector,applications,requests,errors,defaults,states,state:()=>states.at(-1)}
}

test('default follows scheme 1 without making profile requests; a click changes the actual route/profile/windows',async()=>{
  const app=setup()
  assert.equal(app.state().selectedId,'1');assert.equal(app.requests.length,0);assert.equal(app.selector.pinned,false)
  assert.equal(await app.selector.select('2'),true)
  const result=app.applications.at(-1)
  assert.equal(app.state().selectedId,'2');assert.equal(app.selector.pinned,true)
  assert.deepEqual(result.points,card(2).plan.points)
  assert.equal(result.departure_at,at(20));assert.deepEqual(result.departure_windows,card(2).windows)
  assert.equal(result.analysis.max_wind_speed,2);assert.equal(result.analysis.interpolated_max_wind_speed,.5)
  assert.equal(result.flight_profile.analysis,result.analysis)
  assert.equal(result.route_context.departure_at,result.flight_profile.departure_at)
  assert.equal(result.route_context.dataset_id,result.flight_profile.dataset_id)
  assert.equal(result.selected_recommendation_id,'2')
})

test('manual selection follows the geometry ID when ranks change, including an explicit click on scheme 1',async()=>{
  const app=setup()
  await app.selector.select('2')
  app.selector.update(workflow([card(3),card(1),card(2)],2))
  assert.equal(app.state().selectedId,'2');assert.equal(app.applications.length,1)
  app.selector.update(workflow([card(1),card(2),card(3)],1)) // Out-of-order update.
  assert.equal(app.state().selectedId,'2')
  await app.selector.select('1')
  app.selector.update(workflow([card(3),card(2),card(1)],3))
  assert.equal(app.state().selectedId,'1');assert.equal(app.selector.pinned,true)
})

test('rapid 2 then 3 selection aborts and discards the late scheme-2 response',async()=>{
  const waiting=new Map(),signals=[]
  const app=setup({requestProfile:(c,signal)=>{const p=deferred();waiting.set(c.id,p);signals.push(signal);return p.promise}})
  const second=app.selector.select('2'),third=app.selector.select('3')
  assert.equal(signals[0].aborted,true)
  waiting.get('3').resolve(profile(card(3)));await third
  waiting.get('2').resolve(profile(card(2)));await second
  assert.deepEqual(app.applications.map(r=>r.selected_recommendation_id),['3'])
  assert.equal(app.state().selectedId,'3');assert.equal(app.state().pendingId,null)
})

test('switching back reuses the same-version profile cache, and repeated current clicks do not reload',async()=>{
  const app=setup()
  for(const id of ['2','3','2','2']) await app.selector.select(id)
  assert.deepEqual(app.requests.map(r=>r.card.id),['2','3'])
  assert.equal(app.applications.length,3)
})

test('same-geometry window updates do not fetch a profile or change its replay identity',async()=>{
  let paused=0
  const app=setup({onPause:()=>paused++});await app.selector.select('2')
  const second=card(2);second.windows={...second.windows,departure_windows:[{start:at(20),end:at(30)}]}
  app.selector.update(workflow([card(1),second,card(3)],2))
  assert.equal(app.requests.length,1)
  assert.equal(app.applications.length,2)
  assert.equal(app.applications[0].range_profile_key,app.applications[1].range_profile_key)
  assert.deepEqual(app.applications[1].departure_windows,second.windows)
  assert.equal(paused,1)
})

test('an updated departure for the selected geometry is re-evaluated instead of using the previous time cache',async()=>{
  const app=setup();await app.selector.select('2')
  app.selector.update(workflow([card(1),card(2,25),card(3)],2));await flush()
  assert.equal(app.requests.length,2)
  assert.equal(app.applications.at(-1).departure_at,at(25))
  assert.notEqual(app.applications[0].range_profile_key,app.applications[1].range_profile_key)
})

test('a removed or peak-ineligible selection falls back to the current default with a notice',async()=>{
  const app=setup();await app.selector.select('3')
  app.selector.update({...workflow([card(1),card(2)],2),peak_wind_limit_mps:2.5})
  assert.equal(app.state().selectedId,'1');assert.equal(app.selector.pinned,false)
  assert.equal(app.defaults.length,1);assert.match(app.errors[0],/所选方案/)
})

test('new jobs and data versions reset pending selections and reject old response commits',async()=>{
  const waiting=deferred()
  const app=setup({requestProfile:()=>waiting.promise})
  const selecting=app.selector.select('2')
  app.selector.update({...workflow(),baseline_version:'new-draft',dataset_id:'next-sha',recommendations:[]})
  waiting.resolve(profile(card(2)));await selecting
  assert.equal(app.applications.length,0);assert.equal(app.state().selectedId,null);assert.equal(app.selector.pinned,false)
})

test('an invalid returned forecast identity or hard-constraint verdict cannot become the selected route',async()=>{
  for(const change of [{dataset_id:'wrong'},{source:'gfs'},{departure_at:at(21)},
    {average_ground_speed_mps:50},{analysis:{navigation_allowed:false}}]) {
    const app=setup({requestProfile:async c=>({...profile(c),...change})})
    assert.equal(await app.selector.select('2'),false)
    assert.equal(app.applications.length,0)
    assert.equal(app.state().selectedId,'1');assert.equal(app.errors.length,1)
  }
  assert.throws(()=>rangeSelectionResult({...card(2),windows:{...card(2).windows,dataset_id:'wrong'}},profile(card(2)),workflow(),base),/窗口/)
})

test('default-profile load failure terminates without a retry loop',async()=>{
  let count=0
  const app=setup({requestProfile:async c=>{count++;if(c.id==='1') throw Error('network');return profile(c)},onDefault:()=>false})
  await app.selector.select('2')
  app.selector.update(workflow([card(1)],2));await flush()
  assert.equal(count,2);assert.equal(app.state().pendingId,null);assert.equal(app.selector.pinned,false)
})

test('page departure cancels pending switches, and leaving then selecting a new job reloads profiles',async()=>{
  const app=setup();await app.selector.select('2')
  app.selector.cancelPending()
  assert.equal(app.state().selectedId,'2');assert.equal(app.selector.pinned,true)
  app.selector.reset();app.selector.update(workflow())
  await app.selector.select('2')
  assert.equal(app.requests.length,2)
  app.selector.dispose()
  assert.equal(await app.selector.select('3'),false)
})

const walk=node=>[node,...(node.children||[]).flatMap(walk)]
test('range cards expose mouse and keyboard switching and selected/pending feedback',()=>{
  const source=readFileSync(new URL('../components/RecommendationPanel.vue',import.meta.url),'utf8')
  const nodes=walk(baseParse(parse(source).descriptor.template.content))
  const panel=nodes.find(n=>n.tag==='div'&&n.props?.some(p=>p.name==='class'&&p.value?.content==='recommendation-card'))
  assert.ok(panel)
  for(const name of ['click','keydown']) assert.ok(panel.props.some(p=>p.type===7&&p.name==='on'&&p.arg?.content===name))
  for(const name of ['role','tabindex','aria-pressed','aria-busy']) assert.ok(panel.props.some(p=>p.type===7&&p.arg?.content===name))
  assert.match(source,/当前显示/);assert.match(source,/切换中/);assert.match(source,/默认推荐/)
  assert.notEqual(rangeCardProfileKey(card(2)),rangeCardProfileKey(card(2,25)))
})
