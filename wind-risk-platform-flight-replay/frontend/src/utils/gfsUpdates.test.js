import test from 'node:test'
import assert from 'node:assert/strict'
import { GFS_CHECK_INTERVAL_MS, gfsPollDelay, chooseGfsTime } from './gfsUpdates.js'

const now = Date.parse('2026-11-15T04:00:00Z')

test('GFS checks every thirty minutes and follows the server schedule', () => {
  assert.equal(GFS_CHECK_INTERVAL_MS, 1800000)
  assert.equal(gfsPollDelay(undefined, now), 1800000)
  assert.equal(gfsPollDelay({ next_check_at: '2026-11-15T04:10:00Z' }, now), 601000)
  assert.equal(gfsPollDelay({ next_check_at: '2026-11-15T05:00:00Z' }, now), 1800000)
  assert.equal(gfsPollDelay({ next_check_at: '2026-11-15T03:59:00Z' }, now), 1000)
  assert.equal(gfsPollDelay({ next_check_at: 'invalid' }, now), 1800000)
})

test('GFS background refresh preserves the chosen future time and takes its newer cycle', () => {
  const chosen = { label: '未来', valid_time: '2026-11-15 06:00 UTC', cycle: 'new' }
  const fallback = { label: '默认', valid_time: '2026-11-15 07:00 UTC' }
  assert.equal(chooseGfsTime([chosen, fallback], '未来', fallback, now), chosen)
})

test('expired or removed GFS selections move to the current default', () => {
  const expired = { label: '已过期', valid_time: '2026-11-15 03:00 UTC' }
  const fallback = { label: '默认', valid_time: '2026-11-15 07:00 UTC' }
  assert.equal(chooseGfsTime([expired, fallback], '已过期', fallback, now), fallback)
  assert.equal(chooseGfsTime([fallback], '不存在', fallback, now), fallback)
  assert.equal(chooseGfsTime([], '', undefined, now), undefined)
})
