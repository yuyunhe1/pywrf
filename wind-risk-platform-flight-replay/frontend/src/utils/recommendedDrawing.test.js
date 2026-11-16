import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { baseParse } from '@vue/compiler-dom'
import { departureRangeBounds, recommendedRangeRequest, completeRecommendationDrawing } from './departureRange.js'
import { createCustomRouteDrawingController } from './customRouteDrawing.js'

const now=Date.parse('2026-11-16T07:00Z')
const settings=()=>({enabled:true,departureStartAt:'2026-11-16T16:00',departureEndAt:'2026-11-16T18:00',requiredWaypoints:[]})
const selection={source:'wrf',level:'100m AGL'}
const points=[[117,30],[117.2,30.1],[117.5,30.3]]

test('time range can be validated before drawing, and invalid bounds cannot enter the range flow',()=>{
  assert.deepEqual(departureRangeBounds(settings(),now),{
    departure_start_at:'2026-11-16T08:00:00.000Z',departure_end_at:'2026-11-16T10:00:00.000Z'})
  for(const change of [{departureStartAt:''},{departureEndAt:'2026-11-16T15:00'},{departureEndAt:'2026-11-15T18:00'}]) {
    assert.throws(()=>departureRangeBounds({...settings(),...change},now))
  }
  assert.throws(()=>recommendedRangeRequest(settings(),now),/绘制/)
})

test('confirming a range drawing starts exactly one bounded recommendation after edit mode ends, without saving',async()=>{
  const range=settings(),calls=[]
  let state,release,saved=0
  const confirmed=new Promise(resolve=>{release=resolve})
  const drawing=createCustomRouteDrawingController({now:()=>now,onChange:value=>{state=value},
    save:async()=>{saved++},onComplete:async payload=>{
      await confirmed
      range.requiredWaypoints=payload.points
    }})
  drawing.begin();points.forEach(p=>drawing.add(p));drawing.move(1,[117.21,30.11])
  const done=completeRecommendationDrawing({complete:()=>drawing.complete(selection),isDrawing:()=>state.active,
    purpose:'departure_range',recommend:async()=>{
      assert.equal(state.active,false)
      assert.equal(state.saving,false)
      calls.push(recommendedRangeRequest(range,now))
    }})
  assert.equal(state.active,true)
  assert.equal(state.saving,true)
  assert.equal(calls.length,0)
  release();await done
  assert.equal(saved,0)
  assert.equal(calls.length,1)
  assert.equal(calls[0].mode,'departure_range')
  assert.equal(calls[0].departure_start_at,'2026-11-16T08:00:00.000Z')
  assert.deepEqual(calls[0].required_waypoints,[[117,30],[117.21,30.11],[117.5,30.3]])
})

test('unrestricted drawings keep fixed-path evaluation and do not launch range search',async()=>{
  let state,evaluated=0,searched=0,saved=0
  const drawing=createCustomRouteDrawingController({onChange:value=>{state=value},save:async()=>{saved++},
    onComplete:async payload=>{evaluated++;assert.deepEqual(payload.points,points)}})
  drawing.begin();points.forEach(p=>drawing.add(p))
  await completeRecommendationDrawing({complete:()=>drawing.complete(selection),isDrawing:()=>state.active,
    purpose:'fixed',recommend:async()=>{searched++}})
  assert.equal(evaluated,1);assert.equal(searched,0);assert.equal(saved,0)
})

test('invalid drawing does not submit a task and expiration after drawing preserves the confirmed anchors',async()=>{
  let state,calls=0
  const drawing=createCustomRouteDrawingController({onChange:value=>{state=value},onComplete:async()=>{}})
  drawing.begin();drawing.add(points[0])
  await completeRecommendationDrawing({complete:()=>drawing.complete(selection),isDrawing:()=>state.active,
    purpose:'departure_range',recommend:async()=>{calls++}})
  assert.equal(calls,0);assert.equal(state.active,true)
  const range={...settings(),requiredWaypoints:points}
  assert.throws(()=>recommendedRangeRequest(range,Date.parse('2026-11-17T00:00Z')),/已过/)
  assert.deepEqual(range.requiredWaypoints,points)
  assert.throws(()=>recommendedRangeRequest({...range,enabled:false},now),/启用/)
})

const allNodes=node=>[node,...(node.children||[]).flatMap(allNodes)]
const attribute=(node,name)=>node.props?.find(p=>p.type===6&&p.name===name)?.value?.content
const model=node=>node.props?.find(p=>p.type===7&&p.name==='model')?.exp?.content
test('range controls belong to recommended planning and precede drawing; specified planning only selects one time',()=>{
  const source=readFileSync(new URL('../components/ControlPanel.vue',import.meta.url),'utf8')
  const template=baseParse(parse(source).descriptor.template.content)
  const nodes=allNodes(template)
  const specified=nodes.find(n=>n.tag==='section'&&attribute(n,'class')==='route-planning-section')
  const recommended=nodes.find(n=>n.tag==='section'&&attribute(n,'class')==='route-recommendation-section')
  assert.ok(specified&&recommended)
  const single=allNodes(specified),multi=allNodes(recommended)
  const departureSelect=single.find(n=>n.tag==='select'&&model(n)==='planner.departureMode')
  assert.deepEqual(allNodes(departureSelect).filter(n=>n.tag==='option').map(n=>attribute(n,'value')),['now','scheduled'])
  assert.ok(!single.some(n=>model(n)?.startsWith('recommendationRange')))
  assert.ok(multi.some(n=>model(n)==='recommendationRange.enabled'))
  const start=multi.find(n=>model(n)==='recommendationRange.departureStartAt')
  const end=multi.find(n=>model(n)==='recommendationRange.departureEndAt')
  const draw=multi.find(n=>n.tag==='button'&&n.props.some(p=>p.type===7&&p.exp?.content.includes("'draw-custom-route'")))
  assert.ok(start.loc.start.offset<draw.loc.start.offset&&end.loc.start.offset<draw.loc.start.offset)
  assert.equal(attribute(start,'step'),'60');assert.equal(attribute(end,'step'),'60')
})
