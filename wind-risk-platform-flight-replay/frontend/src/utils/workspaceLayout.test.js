import test from 'node:test'
import assert from 'node:assert/strict'
import { defaultLayout, fitDesktopPanels, normalizeLayout } from './workspaceLayout.js'
import { exportForRoute, routeSnapshotKey, routeDistanceKm, durationLabel, planningStageLabel } from './routeRecords.js'

test('restored layout clamps bad widths and preserves explicit collapsed panels', () => {
  assert.deepEqual(normalizeLayout({ leftWidth: 10, rightWidth: 900, leftOpen: false }), { leftWidth: 280, rightWidth: 480, leftOpen: false, rightOpen: true })
  assert.deepEqual(normalizeLayout({ leftWidth: NaN, rightWidth: 'bad', rightOpen: 'false' }), defaultLayout())
})
test('both expanded panels leave at least 400px of usable map across desktop widths', () => {
  for (const width of [1201, 1366, 1920]) {
    const layout = fitDesktopPanels({ leftWidth: 480, rightWidth: 480 }, width)
    assert.ok(width - layout.leftWidth - layout.rightWidth - 48 >= 400)
    assert.ok(layout.leftWidth >= 280 && layout.rightWidth >= 280)
  }
  const preferred = { leftWidth: 480, rightWidth: 480, leftOpen: false }
  assert.equal(fitDesktopPanels(preferred, 1201).rightWidth, 480)
  assert.deepEqual(preferred, { leftWidth: 480, rightWidth: 480, leftOpen: false })
})
const route = { route_id: 'one', name: '同名', points: [[117, 30], [118, 30]], source: 'gfs', level: '100m AGL' }
test('saved snapshot changes with path, source, altitude, name and native trajectory identity', () => {
  const key = routeSnapshotKey(route)
  for (const change of [{ points: [...route.points].reverse() }, { source: 'wrf' }, { level: '200m AGL' }, { name: '另一条' }, { v3: { trajectory_id: 'new' } }]) {
    assert.notEqual(routeSnapshotKey({ ...route, ...change }), key)
  }
  assert.equal(routeSnapshotKey({ ...route, created_at: 'yesterday', cycle: 'new forecast', mission_items: [] }), key)
})
test('exports use ID and latest revision and never guess between duplicate names', () => {
  const other = { ...route, route_id: 'two' }
  const files = [{ route_id: 'two', route_name: '同名', timestamp: 9 }, { route_id: 'one', timestamp: 1 }, { route_id: 'one', timestamp: 3 }]
  assert.equal(exportForRoute(route, files, [route, other]), files[2])
  assert.equal(exportForRoute(route, [files[0], { route_name: '同名' }], [route, other]), undefined)
  const legacy = { route_name: '同名' }
  assert.equal(exportForRoute(route, [legacy], [route]), legacy)
})
test('distance metadata respects every waypoint and missing coordinates stay unknown', () => {
  assert.ok(routeDistanceKm([[0, 0], [1, 0], [1, 1]]) > routeDistanceKm([[0, 0], [1, 1]]))
  assert.equal(routeDistanceKm([[0, 0], [NaN, 0]]), null)
  assert.equal(routeDistanceKm([]), null)
  assert.equal(durationLabel(null), '—')
  assert.equal(durationLabel(7260), '2 小时 1 分钟')
})
test('candidate-found status cannot claim a route has been displayed before publication', () => {
  assert.equal(planningStageLabel({ status: 'running', feasible: true }, false), '已找到候选 · 正在比较')
  assert.equal(planningStageLabel({ status: 'running', feasible: true }, true), '已展示阶段结果 · 继续优化')
  assert.equal(planningStageLabel({ status: 'timed_out' }, true), '已达到时限')
})
