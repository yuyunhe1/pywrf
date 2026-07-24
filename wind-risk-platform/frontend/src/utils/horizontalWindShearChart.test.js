import assert from 'node:assert/strict'
import test from 'node:test'

import {
  HORIZONTAL_WIND_SHEAR_DISPLAY_EPSILON,
  buildHorizontalWindShearChartModel,
  formatMaxHorizontalWindShear,
} from './horizontalWindShearChart.js'

const segment = (center, shear, index = 0, end = center * 2) => ({
  segment_index: index,
  center_distance_km: center,
  end_distance_km: end,
  horizontal_wind_shear: shear,
})

test('uniform wind profile produces no visible shear bars', () => {
  const model = buildHorizontalWindShearChartModel([segment(1.2, 0), segment(3.8, 0)], 5.4)

  assert.deepEqual(model.events, [])
  assert.equal(model.threshold, 5.4)
  assert.deepEqual(formatMaxHorizontalWindShear(0), { value: '0.00', unit: 'm/s' })
})

test('zero filtering preserves real cumulative-distance positions', () => {
  const model = buildHorizontalWindShearChartModel([
    segment(0.5, 0, 0),
    segment(4.25, 1.62, 1),
    segment(5.1, HORIZONTAL_WIND_SHEAR_DISPLAY_EPSILON, 2),
    segment(13.7, 2.31, 3, 15.1),
    segment(24.0, 0, 4, 30.0),
  ], 5.4)

  assert.deepEqual(model.barData.map((item) => item.value), [[4.25, 1.62], [13.7, 2.31]])
  assert.deepEqual(model.events.map((item) => item.segment_index), [1, 3])
  assert.equal(model.routeDistanceKm, 30.0)
})

test('threshold changes are reflected without changing event data', () => {
  const profile = [segment(2.4, 1.5)]
  const first = buildHorizontalWindShearChartModel(profile, 5.4)
  const second = buildHorizontalWindShearChartModel(profile, 3.2)

  assert.deepEqual(first.barData, second.barData)
  assert.equal(first.threshold, 5.4)
  assert.equal(second.threshold, 3.2)
})

test('missing and blocked routes never display a false zero maximum', () => {
  assert.deepEqual(formatMaxHorizontalWindShear(null), { value: '-', unit: 'm/s' })
  assert.deepEqual(formatMaxHorizontalWindShear(2.31, true), { value: '超过阈值', unit: '' })
})
