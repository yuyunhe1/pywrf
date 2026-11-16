import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { activeRelease, buildRelease } from '../../scripts/release.mjs'

test('an incomplete production build cannot replace the previous usable build', () => {
  const parent = resolve('.cache/release-tests')
  mkdirSync(parent, { recursive: true })
  const root = mkdtempSync(join(parent, 'release-'))
  mkdirSync(join(root, 'src'))
  writeFileSync(join(root, 'src/main.js'), 'example source')
  const build = (path) => { mkdirSync(path); writeFileSync(join(path, 'index.html'), 'ready'); return 0 }
  const first = buildRelease({ root, builder: build })
  const registered = readFileSync(join(root, '.releases/current.json'), 'utf8')
  assert.throws(() => buildRelease({ root, builder: () => 1 }), /构建未完成/)
  assert.equal(readFileSync(join(root, '.releases/current.json'), 'utf8'), registered)
  assert.equal(activeRelease(root).path, first.path)
  const second = buildRelease({ root, builder: build })
  assert.notEqual(second.path, first.path)
  assert.equal(JSON.parse(readFileSync(join(root, '.releases/previous.json'), 'utf8')).directory, first.directory)
  assert.equal(readFileSync(join(first.path, 'index.html'), 'utf8'), 'ready')
})
