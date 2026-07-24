import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildNavigationDecision,
  hasHorizontalWindShearRisk,
} from './navigationDecision.js'

test('horizontal shear risk produces a prohibited binary decision', () => {
  const analysis = {
    max_wind_speed: 3.2,
    wind_shear: { highest_shear_level: '禁飞' },
  }

  assert.equal(hasHorizontalWindShearRisk(analysis), true)
  assert.equal(buildNavigationDecision(analysis).level, '禁止通航')
})

test('safe analysis produces an allowed binary decision', () => {
  const analysis = {
    max_wind_speed: 5.0,
    navigation_allowed: true,
    wind_shear: { highest_shear_level: '安全', horizontal_shear_warning_count: 0 },
  }

  assert.equal(hasHorizontalWindShearRisk(analysis), false)
  assert.equal(buildNavigationDecision(analysis).level, '允许通航')
})

test('endpoint hard block stays prohibited without route samples', () => {
  const analysis = {
    max_wind_speed: 8.1,
    samples: [],
    endpoint_hard_block: { message: '起点风速超过硬上限' },
  }

  const decision = buildNavigationDecision(analysis, 7.9)
  assert.equal(decision.level, '禁止通航')
  assert.equal(decision.message, '起点风速超过硬上限')
})
