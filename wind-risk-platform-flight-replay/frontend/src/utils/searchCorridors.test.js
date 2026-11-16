import assert from 'node:assert/strict'
import test from 'node:test'
import { mergeSearchCorridors } from './searchCorridors.js'

test('collinear overlapping pieces merge across both search directions and retain forks', () => {
  const lines = mergeSearchCorridors({
    forward: [{ points: [[0, 0], [1, 0], [2, 0]] }, { points: [[0, 0], [1, 0], [1, 1]] }],
    backward: [{ points: [[2, 0], [0.5, 0]] }],
  })
  const horizontal = lines.filter((line) => line.points.every((point) => Math.abs(point[1]) < 1e-8))
  assert.equal(horizontal.reduce((sum, line) => sum + Math.abs(line.points[1][0] - line.points[0][0]), 0), 2)
  assert.ok(horizontal.some((line) => line.direction === 'both'))
  assert.ok(lines.some((line) => line.points[1][1] > .9))
})

test('display keeps at most three corridors per side and never mutates search data', () => {
  const progress = { forward: Array.from({ length: 5 }, (_, i) => ({ points: [[0, i], [1, i]] })) }
  const before = JSON.stringify(progress)
  assert.equal(mergeSearchCorridors(progress).length, 3)
  assert.equal(JSON.stringify(progress), before)
  assert.deepEqual(mergeSearchCorridors(null), [])
})
