import { spawn } from 'node:child_process'
import { join } from 'node:path'
import { activeRelease, frontendRoot } from './release.mjs'

const release = activeRelease()
console.log(`[frontend] Production build: ${release.source_id || 'previous dist'} (${release.path})`)
const child = spawn(process.execPath, [join(frontendRoot, 'node_modules/vite/bin/vite.js'), 'preview',
  '--outDir', release.path, '--host', '0.0.0.0', '--port', process.env.FRONTEND_PORT || '5174', '--strictPort'],
{ cwd: frontendRoot, stdio: 'inherit', env: process.env })
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
child.on('exit', (code) => { process.exitCode = code || 0 })
child.on('error', (error) => { console.error(error.message); process.exitCode = 1 })
