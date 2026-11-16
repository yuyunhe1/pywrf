import test from 'node:test'
import assert from 'node:assert/strict'
import { chooseWrfTime, wrfPollDelay } from './wrfUpdates.js'

const now = Date.parse('2026-10-11T04:20:00Z')
const old = { label: '旧数据', valid_time: '2026-10-10 03:00 UTC', cycle: 'old' }
const near = { label: '13:00 北京时间', valid_time: '2026-10-11 05:00 UTC', cycle: 'new' }
const target = { label: '16:00 北京时间', valid_time: '2026-10-11 08:00 UTC', cycle: 'new' }
const lookahead = { target_valid_time: target.valid_time, available: true }

test('waits for next ten-minute check and polls locally during downloads', () => {
  assert.equal(wrfPollDelay(undefined, now), 600000)
  assert.equal(wrfPollDelay({ running: true }, now), 10000)
  assert.equal(wrfPollDelay({ next_check_at: '2026-10-11T04:30:00Z' }, now), 600000)
  assert.equal(wrfPollDelay({ next_check_at: '2026-10-11T04:21:00Z' }, now), 61000)
  assert.equal(wrfPollDelay({ next_check_at: '2026-10-11T04:00:00Z' }, now), 1000)
})

test('replaces historical fallback with the newly available three-hour product', () => {
  assert.equal(chooseWrfTime([old, near, target], old.label, lookahead, { available: false }, now), target)
})

test('keeps a user-selected future time when a newer cycle arrives', () => {
  const updated = { ...near, cycle: 'newer' }
  assert.equal(chooseWrfTime([old, updated, target], near.label, lookahead, lookahead, now), updated)
})

test('selects the latest historical product while waiting for current forecasts', () => {
  const latest = { ...old, label: '较新的历史数据', valid_time: '2026-10-10 04:00 UTC' }
  assert.equal(chooseWrfTime([old, latest], '', { available: false }, undefined, now), latest)
})
