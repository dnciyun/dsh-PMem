/**
 * 浮现回执的端到端测试：临时库 → 种一条记忆并让它浮现 → 假会话产出回答 →
 * turn/end → 插件回调 `pmem receipt` → 库里出现 surface.used 与 token 样本。
 *
 * 不碰真实 data/pmem.db。
 */

import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from './index.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const py = process.env.PMEM_PYTHON || 'python'
const dir = mkdtempSync(join(tmpdir(), 'pmem-receipt-'))
const db = join(dir, 'pmem.db')
const env = { ...process.env, PYTHONPATH: join(root, 'src'), PYTHONIOENCODING: 'utf-8' }

const run = (...args) => {
  const proc = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, ...args], { env, encoding: 'utf8' })
  assert.equal(proc.status, 0, proc.stderr || proc.stdout)
  return proc.stdout
}

run('init')
run('add', '我最喜欢的颜色是深蓝色，因为夜里看海最安静', '--group', '我/偏好', '--grade', 'B')
run('surface', '--session', 's-rec', '--cue', '颜色', '--mode', 'turn')

const listeners = {}
const ctx = {
  systemPrompt: { context: () => () => {} },
  tools: { register: () => () => {} },
  commands: { register: () => () => {} },
  on(event, handler) {
    ;(listeners[event] ??= []).push(handler)
    return () => {}
  },
}

const plugin = apply(ctx, {
  db,
  src: join(root, 'src'),
  cwd: root,
  refreshMs: 60 * 60 * 1000,
})

const agent = { session: { id: 's-rec', header: { cwd: dir } } }
const fire = (event) => {
  for (const handler of listeners['session/event'] ?? []) handler(agent.session, event)
}

const receipts = () => JSON.parse(run('--json', 'surface', '--session', 's-rec', '--receipts')).data

try {
  fire({
    type: 'assistant/message',
    data: { message: { role: 'assistant', content: [{ type: 'text', text: '我记得你说过：深蓝色，因为夜里看海最安静。' }] } },
  })
  fire({ type: 'turn/end', data: { reason: { kind: 'completed' } } })

  let used = 0
  for (let attempt = 0; attempt < 40 && used === 0; attempt += 1) {
    await new Promise(r => setTimeout(r, 150))
    used = receipts().used
  }
  assert.equal(used, 1, `回执没有落库：${JSON.stringify(receipts())}`)

  const stats = JSON.parse(run('--json', 'receipt', '--session', 's-rec', '--stats')).data
  assert.equal(stats.samples, 1, `token 样本没记上：${JSON.stringify(stats)}`)
  assert.ok(stats.mean >= 0)

  console.log('dsh-pmem receipt ok')
} finally {
  try { listeners['agent/disposed']({ agent }) } catch { /* ignore */ }
  plugin.stop()
  rmSync(dir, { recursive: true, force: true })
}
