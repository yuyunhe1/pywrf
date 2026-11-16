import { spawnSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

export const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export const sourceDigest = (root) => {
  const hash = createHash('sha256')
  const scan = (path) => {
    for (const item of readdirSync(path, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const file = join(path, item.name)
      if (item.isDirectory()) scan(file)
      else { hash.update(relative(root, file)); hash.update(readFileSync(file)) }
    }
  }
  scan(join(root, 'src'))
  for (const name of ['package.json', 'package-lock.json', 'vite.config.js', 'index.html']) {
    if (existsSync(join(root, name))) hash.update(readFileSync(join(root, name)))
  }
  return hash.digest('hex')
}

export const activeRelease = (root = frontendRoot) => {
  const releases = join(root, '.releases')
  const pointer = join(releases, 'current.json')
  if (existsSync(pointer)) {
    const info = JSON.parse(readFileSync(pointer, 'utf8'))
    const path = resolve(releases, info.directory)
    if (!path.startsWith(resolve(releases) + sep) || !existsSync(join(path, 'index.html'))) throw Error('前端构建登记无效')
    return { path, ...info }
  }
  if (existsSync(join(root, 'dist/index.html'))) return { path: join(root, 'dist'), legacy: true }
  throw Error('尚无可用前端构建，请先执行 npm run release')
}

export const buildRelease = ({ root = frontendRoot, builder } = {}) => {
  const releases = join(root, '.releases')
  mkdirSync(releases, { recursive: true })
  const directory = `release-${Date.now()}-${randomUUID().slice(0, 8)}`
  const target = join(releases, directory)
  const buildTemp = join(root, '.cache', 'tmp')
  mkdirSync(buildTemp, { recursive: true })
  const before = sourceDigest(root)
  const run = builder || ((path) => spawnSync(process.execPath, [join(root, 'node_modules/vite/bin/vite.js'),
    'build', '--outDir', path], { cwd: root, stdio: 'inherit', env: { ...process.env, TEMP: buildTemp, TMP: buildTemp } }).status)
  if (run(target) !== 0 || !existsSync(join(target, 'index.html'))) throw Error('构建未完成；继续保留原有前端构建')
  if (sourceDigest(root) !== before) throw Error('构建期间源码发生变化，未切换前端构建，请重新构建')
  const release = { directory, source_id: before, built_at: new Date().toISOString(), mode: 'production' }
  writeFileSync(join(target, 'release.json'), JSON.stringify(release))
  const current = join(releases, 'current.json')
  if (existsSync(current)) writeFileSync(join(releases, 'previous.json'), readFileSync(current))
  const pending = join(releases, `current-${randomUUID()}.tmp`)
  writeFileSync(pending, JSON.stringify(release))
  renameSync(pending, current)
  return { path: target, ...release }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(buildRelease())) } catch (error) { console.error(error.message); process.exitCode = 1 }
}
