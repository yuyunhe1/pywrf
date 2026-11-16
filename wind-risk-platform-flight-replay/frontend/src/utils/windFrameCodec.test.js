import test from 'node:test'
import assert from 'node:assert/strict'
import { decodeWindPacket, windFrameFields } from './windFrameCodec.js'
import { createFrameRenderGate } from './frameRenderGate.js'

const packet = (vectors = [[1, NaN, 3, 4], [2, 3, 4, 5]], dtypes = ['<f4', '<f8']) => {
  const descriptors = []
  let bytes = 0
  vectors.forEach((values, i) => {
    descriptors.push({ count: 4, offset: bytes, dtype: dtypes[i], parameterNumber: i + 2 })
    bytes += values.length * (dtypes[i] === '<f4' ? 4 : 8)
    bytes = Math.ceil(bytes / 8) * 8
  })
  const frame = { metadata: { frame_id: 'fixed', level: '100m AGL' }, available: true,
    header: { nx: 2, ny: 2, lo1: 117, la1: 31, dx: .1, dy: .1 }, components: descriptors }
  const json = new TextEncoder().encode(JSON.stringify({ version: 1, frames: [frame] }))
  const start = Math.ceil((12 + json.byteLength) / 8) * 8
  const result = new ArrayBuffer(start + bytes), view = new DataView(result)
  new Uint8Array(result, 0, 8).set([87, 70, 82, 77, 48, 48, 49, 0])
  view.setUint32(8, json.byteLength, true)
  new Uint8Array(result, 12, json.byteLength).set(json)
  descriptors.forEach((part, i) => vectors[i].forEach((value, j) => {
    if (part.dtype === '<f4') view.setFloat32(start + part.offset + j * 4, value, true)
    else view.setFloat64(start + part.offset + j * 8, value, true)
  }))
  return result
}

test('mixed lossless component types preserve exact Float64 values and missing cells', () => {
  const frame = decodeWindPacket(packet([[1, 2, 3, 4], [Math.PI, NaN, Math.E, -0]]))[0]
  assert.deepEqual(frame.velocity[0].data, [1, 2, 3, 4])
  assert.deepEqual(frame.velocity[1].data, [Math.PI, null, Math.E, -0])
  const fields = windFrameFields(frame)
  assert.equal(fields.wind.metadata.frame_id, fields.heatmap.metadata.frame_id)
  assert.equal(fields.heatmap.wind_speed.data[1], null)
  assert.equal(fields.heatmap.wind_speed.data[0], Math.hypot(1, Math.PI))
})

test('truncated or unsupported packets cannot produce weather arrays', () => {
  const value = packet()
  assert.throws(() => decodeWindPacket(value.slice(0, value.byteLength - 4)), /不完整/)
  new Uint8Array(value)[0] = 0
  assert.throws(() => decodeWindPacket(value), /不支持/)
})

test('canvas completion needs both visible renderers and ignores obsolete frame callbacks', () => {
  const calls = [], gate = createFrameRenderGate((key) => calls.push(key))
  gate.reset('a', ['velocity', 'heatmap'])
  gate.ready('a', 'heatmap')
  assert.deepEqual(calls, [])
  gate.reset('b', ['velocity', 'heatmap'])
  gate.ready('a', 'velocity')
  gate.ready('b', 'velocity')
  assert.deepEqual(calls, [])
  gate.ready('b', 'heatmap')
  gate.ready('b', 'velocity')
  assert.deepEqual(calls, ['b'])
})
