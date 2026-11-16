import test from 'node:test'
import assert from 'node:assert/strict'
import { createCustomRouteDrawingController, customRouteName, customRoutePayload, savedRouteFromExport,
  MAX_CUSTOM_ROUTE_POINTS } from './customRouteDrawing.js'

const selection = { source: 'gfs', level: '100m AGL', cycle: '2026-11-04 00:00 UTC', forecastHour: 9 }
const points = [[117.1, 30.1], [117.2, 30.3], [117.4, 30.2]]
const setup = (options = {}) => {
  let state
  const requests = [], activated = [], activationErrors = []
  const drawing = createCustomRouteDrawingController({ now: () => Date.parse('2026-11-04T16:01:02Z'),
    onChange: value => { state = value },
    save: async payload => { requests.push(payload); return { ...payload, route_id: 'saved-id' } },
    onSaved: saved => activated.push(saved), onSavedError: saved => activationErrors.push(saved), ...options })
  drawing.begin()
  return { drawing, state: () => state, requests, activated, activationErrors }
}
const addPoints = app => points.forEach(point => app.drawing.add(point))

test('default custom route names use Beijing date and time', () => {
  assert.equal(customRouteName(Date.parse('2026-11-04T16:01:02Z')), '自定义航线_20261105_000102')
})

test('click order survives dragging and undo removes the most recently added point', () => {
  const app = setup()
  addPoints(app)
  const before = app.state().points
  app.drawing.move(1, [117.25, 30.35])
  assert.deepEqual(app.state().points, [points[0], [117.25, 30.35], points[2]])
  assert.deepEqual(before, points)
  app.drawing.undo()
  assert.deepEqual(app.state().points, [points[0], [117.25, 30.35]])
  app.drawing.clear()
  assert.deepEqual(app.state().points, [])
  assert.equal(app.state().name, '自定义航线_20261105_000102')
})

test('duplicate clicks and invalid coordinates do not introduce extra waypoints', () => {
  const app = setup()
  app.drawing.add(points[0])
  app.drawing.add(points[0])
  app.drawing.add([NaN, 30])
  assert.equal(app.state().points.length, 1)
  assert.match(app.state().error, /经纬度/)
  app.drawing.move(0, [181, 30])
  assert.deepEqual(app.state().points, [points[0]])
  app.drawing.add(points[1])
  assert.equal(app.state().error, '')
})

test('saving preserves exact ordered geometry, current source and height without requiring weather', async () => {
  const app = setup()
  addPoints(app)
  app.drawing.rename('  指定固定航线  ')
  const saved = await app.drawing.save({ ...selection, source: 'wrf', level: '300m AGL', forecastHour: 3 })
  assert.equal(app.requests.length, 1)
  assert.deepEqual(saved.points, points)
  assert.deepEqual(saved.start, points[0])
  assert.deepEqual(saved.end, points[2])
  assert.equal(saved.source, 'wrf')
  assert.equal(saved.level, '300m AGL')
  assert.equal(saved.name, '指定固定航线')
  assert.equal(app.state().active, false)
  assert.equal(app.activated[0], saved)
})

test('a closed loop retains the final return to its starting position', () => {
  const payload = customRoutePayload({ name: '闭环', points: [...points, points[0]] }, selection)
  assert.deepEqual(payload.points, [...points, points[0]])
  assert.deepEqual(payload.end, payload.start)
})

test('overlapping dragged waypoints are saved in their exact draft order', () => {
  const app = setup()
  addPoints(app)
  app.drawing.move(1, points[0])
  const draft = app.state()
  assert.deepEqual(customRoutePayload(draft, selection).points, [points[0], points[0], points[2]])
})

test('two identical positions, invalid settings and excessive point counts cannot be submitted', async () => {
  const app = setup()
  await app.drawing.save(selection)
  assert.equal(app.requests.length, 0)
  assert.match(app.state().error, /至少/)
  assert.throws(() => customRoutePayload({ name: 'a', points: [points[0], points[0]] }, selection), /至少/)
  assert.throws(() => customRoutePayload({ name: '', points }, selection), /名称/)
  assert.throws(() => customRoutePayload({ name: 'a', points }, { ...selection, source: '' }), /数据源/)
  assert.throws(() => customRoutePayload({ name: 'a', points }, { ...selection, level: '300m MSL' }), /高度/)
  assert.throws(() => customRoutePayload({ name: 'a', points: Array(MAX_CUSTOM_ROUTE_POINTS + 1).fill(points[0]) }, selection), /数量/)
})

test('saving disables repeat submission and editing until the request finishes', async () => {
  let finish
  const sent = []
  const app = setup({ save: payload => { sent.push(payload); return new Promise(resolve => { finish = () => resolve({ ...payload, route_id: 'one' }) }) } })
  addPoints(app)
  const pending = app.drawing.save(selection)
  assert.equal(app.state().saving, true)
  await app.drawing.save(selection)
  app.drawing.add([118, 31])
  app.drawing.move(0, [118, 31])
  app.drawing.undo()
  app.drawing.clear()
  app.drawing.rename('改名')
  app.drawing.cancel()
  app.drawing.begin()
  assert.equal(sent.length, 1)
  assert.deepEqual(app.state().points, points)
  assert.equal(app.state().saving, true)
  finish()
  await pending
  assert.equal(app.state().active, false)
})

test('save failure retains the editable draft and retry does not change its coordinates', async () => {
  let attempts = 0
  const app = setup({ save: async payload => {
    if (++attempts === 1) throw new Error('保存服务暂时不可用')
    return { ...payload, route_id: 'retry' }
  } })
  addPoints(app)
  await app.drawing.save(selection)
  assert.equal(app.state().active, true)
  assert.equal(app.state().saving, false)
  assert.deepEqual(app.state().points, points)
  assert.match(app.state().error, /暂时/)
  assert.equal(app.activated.length, 0)
  await app.drawing.save(selection)
  assert.equal(app.activated[0].route_id, 'retry')
  assert.deepEqual(app.activated[0].points, points)
})

test('an evaluation failure after saving does not reopen the draft or create duplicate records', async () => {
  const app = setup({ onSaved: () => { throw new Error('气象数据不足') } })
  addPoints(app)
  const saved = await app.drawing.save(selection)
  assert.equal(app.state().active, false)
  assert.equal(app.activationErrors[0], saved)
  await app.drawing.save(selection)
  assert.equal(app.requests.length, 1)
})

test('cancel clears an unsaved draft without persisting anything', () => {
  const app = setup()
  addPoints(app)
  app.drawing.cancel()
  assert.equal(app.state().active, false)
  assert.deepEqual(app.state().points, [])
  assert.equal(app.requests.length, 0)
})

test('a save response after disposal cannot activate an obsolete route', async () => {
  let finish
  const app = setup({ save: payload => new Promise(resolve => { finish = () => resolve({ ...payload, route_id: 'late' }) }) })
  addPoints(app)
  const pending = app.drawing.save(selection)
  app.drawing.dispose()
  finish()
  await pending
  assert.equal(app.activated.length, 0)
})

test('old evaluation errors cannot overwrite a new drawing session', async () => {
  let reject
  const app = setup({ onSaved: () => new Promise((_, fail) => { reject = fail }) })
  addPoints(app)
  const pending = app.drawing.save(selection)
  await Promise.resolve()
  app.drawing.begin()
  reject(new Error('上一次评估失败'))
  await pending
  assert.equal(app.state().active, true)
  assert.equal(app.activationErrors.length, 0)
})

test('history selects by ID and never guesses between same-name routes', () => {
  const rows = [{ route_id: 'first', name: '同名' }, { route_id: 'second', name: '同名' }]
  assert.equal(savedRouteFromExport({ route_id: 'second', route_name: '同名' }, rows), rows[1])
  assert.equal(savedRouteFromExport({ route_id: 'removed', route_name: '同名' }, rows), undefined)
  assert.equal(savedRouteFromExport({ route_name: '同名' }, rows), undefined)
  assert.equal(savedRouteFromExport({ route_name: '同名' }, rows.slice(0, 1)), rows[0])
})
