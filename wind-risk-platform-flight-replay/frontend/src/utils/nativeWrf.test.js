import test from 'node:test'
import assert from 'node:assert/strict'
import {frameBracket,interpolateWindFrames,NATIVE_WRF_TIME_MODEL as NATIVE} from './flightReplay.js'

const base=Date.parse('2026-11-11T00:00:00Z')
const at=minutes=>new Date(base+minutes*60000).toISOString()
const frame=(minute,value)=>({available:true,metadata:{dataset_id:'native',cycle:at(0),level:'100m AGL',
  forecast_hour:minute/60,forecast_minutes:minute,valid_time:at(minute),wind_time_model:NATIVE,
  wind_time_step_seconds:600,coverage_end:at(60)},velocity:[value,0].map(v=>({
    header:{nx:2,ny:2,lo1:117,la1:31,dx:.01,dy:.01},data:Array(4).fill(v)}))})

test('native WRF selects the previous actual ten-minute frame and never a future or synthetic one',()=>{
  const times=[at(0),at(10),at(20)]
  assert.deepEqual(frameBracket(times,at(3),NATIVE),[at(0),at(0)])
  assert.deepEqual(frameBracket(times,at(9.999),NATIVE),[at(0),at(0)])
  assert.deepEqual(frameBracket(times,at(10),NATIVE),[at(10),at(10)])
  assert.equal(frameBracket([at(0),at(20)],at(10),NATIVE),null)
  assert.equal(frameBracket(times,at(20.1),NATIVE),null)
})

test('native WRF map copies original vectors without blending neighbouring snapshots',()=>{
  const a=frame(0,2),b=frame(10,8)
  assert.deepEqual(interpolateWindFrames(a,a,at(3)).wind.velocity[0].data,[2,2,2,2])
  assert.deepEqual(interpolateWindFrames(a,a,at(9.999)).heatmap.wind_speed.data,[2,2,2,2])
  assert.deepEqual(interpolateWindFrames(b,b,at(10)).wind.velocity[0].data,[8,8,8,8])
  assert.equal(interpolateWindFrames(a,b,at(5)),null)
  assert.equal(interpolateWindFrames(a,a,at(10)),null)
  const last=frame(60,2)
  assert.equal(interpolateWindFrames(last,last,at(60.1)),null)
})
