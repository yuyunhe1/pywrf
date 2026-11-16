export const WIND_FRAME_MIME = 'application/vnd.wind-frame.v1'

export const decodeWindPacket = (buffer) => {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 12) throw Error('风场数据包不完整')
  const view = new DataView(buffer)
  const signature = new Uint8Array(buffer, 0, 8)
  if (![87, 70, 82, 77, 48, 48, 49, 0].every((value, i) => signature[i] === value)) throw Error('不支持的风场数据格式')
  const headerLength = view.getUint32(8, true)
  if (headerLength > 1024 * 1024 || 12 + headerLength > buffer.byteLength) throw Error('风场元数据不完整')
  const description = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 12, headerLength)))
  if (description.version !== 1 || !Array.isArray(description.frames) || description.frames.length < 1
    || description.frames.length > 2) throw Error('不支持的风场数据版本')
  const start = Math.ceil((12 + headerLength) / 8) * 8
  return description.frames.map((frame) => {
    if (!frame?.metadata?.frame_id) throw Error('风场缺少版本标识')
    if (!frame.available) return { ...frame, velocity: [] }
    const header = frame.header
    const count = header?.nx * header?.ny
    if (!Number.isSafeInteger(count) || count < 1 || !Number.isInteger(header.nx) || !Number.isInteger(header.ny)
      || !Array.isArray(frame.components) || frame.components.length !== 2) throw Error('风场网格尺寸无效')
    const velocity = frame.components.map((part, index) => {
      const size = part.dtype === '<f4' ? 4 : part.dtype === '<f8' ? 8 : 0
      const offset = start + part.offset
      if (!size || part.count !== count || part.parameterNumber !== index + 2 || !Number.isSafeInteger(part.offset)
        || part.offset < 0 || offset % size || offset + count * size > buffer.byteLength) throw Error('风场数组不完整')
      // DataView explicitly handles little endian on all browsers. Returning
      // normal arrays preserves null/missing semantics in Leaflet and point queries.
      const data = Array.from({ length: count }, (_, i) => {
        const value = size === 4 ? view.getFloat32(offset + i * size, true) : view.getFloat64(offset + i * size, true)
        return Number.isFinite(value) ? value : null
      })
      return { header: { ...header, parameterNumber: index + 2 }, data }
    })
    return { available: true, metadata: frame.metadata, velocity }
  })
}

export const windFrameFields = (frame) => {
  if (!frame?.available || frame.velocity?.length !== 2) return null
  const [u, v] = frame.velocity
  let min = Infinity, max = -Infinity
  const speeds = u.data.map((value, i) => {
    if (!Number.isFinite(value) || !Number.isFinite(v.data[i])) return null
    const speed = Math.hypot(value, v.data[i])
    min = Math.min(min, speed); max = Math.max(max, speed)
    return speed
  })
  return { wind: { metadata: frame.metadata, velocity: frame.velocity },
    heatmap: { metadata: frame.metadata, wind_speed: { header: { ...u.header, unit: 'm/s' }, data: speeds,
      min: Number.isFinite(min) ? min : null, max: Number.isFinite(max) ? max : null } } }
}
