/**
 * 自动 handoff 的集成测试：临时库 → 假会话 → 写回 → 醒来卡出现「上次」。
 * 不碰真实 data/pmem.db。
 */

import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from './index.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const py = process.env.PMEM_PYTHON || 'python'
const dir = mkdtempSync(join(tmpdir(), 'pmem-handoff-'))
const db = join(dir, 'pmem.db')
// 自我层是全局的（跟用户走，不跟库走）。这个测试验的是**旧检索卡**那条路，
// 所以把自我层指到一个空目录：否则一打开就注入真实宪法，永远看不到「上次：」。
const soulDir = join(dir, 'soul')
process.env.PMEM_SOUL_DIR = soulDir
// 上下文水位也落盘在 ~/.dsh/pmem/context.json：不隔离的话，每跑一次这个测试
// 就往真实状态里塞一条假会话（实测真出现过一条 5% 的 s1）。
const contextState = join(dir, 'context.json')
process.env.PMEM_CONTEXT_STATE = contextState

const env = {
  ...process.env,
  PYTHONPATH: join(root, 'src'),
  PYTHONIOENCODING: 'utf-8',
  PMEM_SOUL_DIR: soulDir,
  PMEM_CONTEXT_STATE: contextState,
}
const init = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, 'init'], { env, encoding: 'utf8' })
assert.equal(init.status, 0, init.stderr || init.stdout)

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

/** 一个事件可能挂多个监听器：按注册顺序跑 next() 链，模拟真实 ctx。 */
const preStep = payload => {
  const handlers = listeners['agent/pre-step'] ?? []
  let index = 0
  const next = async () =>
    index >= handlers.length ? { messages: payload.messages } : handlers[index++](payload, next)
  return next()
}

const plugin = apply(ctx, {
  db,
  handoffTurns: 2,
  handoffMinMs: 1000,
  refreshMs: 60 * 60 * 1000,
  budget: 300,
})

const agent = { session: { id: 's1', header: { cwd: dir } } }
const messages = [{ role: 'user', content: [{ type: 'text', text: '我们正在做自动 handoff 的集成测试' }] }]

try {
  await preStep({ agent, messages })
  // 先给一句「这一轮说过的话」：四段里的死路/下一步要从它里面抽。
  for (const handler of listeners['session/event'] ?? []) {
    handler(agent.session, {
      type: 'assistant/message',
      data: { message: { content: [{ type: 'text', text: '试过压缩，会失真。下一步：写四段交接。' }] } },
    })
  }
  for (const handler of listeners['session/event'] ?? []) {
    handler(agent.session, { type: 'turn/end', data: {} })
  }
  for (const handler of listeners['session/event'] ?? []) {
    handler(agent.session, { type: 'turn/end', data: {} })
  }

  await new Promise(resolvePromise => setTimeout(resolvePromise, 1800))
  await plugin.refresh()
  // handoff 现在双写：记忆库一行 + 自我层叙事「停在哪」。没有自我层时是旧检索卡
  // （「上次：」），第一次写回会顺手建出自我层（「停在哪：」）——两条路都算通过。
  const stopped = /上次：|停在哪：/
  assert.ok(stopped.test(plugin.card), `卡里没有上次/停在哪：${plugin.card}`)
  assert.ok(!plugin.card.includes('上次：上次停在哪'), `前缀重复了：${plugin.card}`)
  assert.ok(plugin.card.includes('自动 handoff 的集成测试'), '卡里没有会话内容')

  const wake = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, '--json', 'wake'], { env, encoding: 'utf8' })
  assert.equal(wake.status, 0, wake.stderr)
  const data = JSON.parse(wake.stdout)
  assert.ok(stopped.test(data.data.text))

  // 四段交接到底写没写进去？上一轮真机「谎报已交接」就是没写却说写了——
  // 所以这里**直接查库**，不信插件嘴上的话。
  const probeScript = [
    'import json, sqlite3, sys',
    'c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row',
    'row = c.execute("select text_raw from memory where text_raw like ? order by seq desc limit 1", ("%上次交接%",)).fetchone()',
    'print(json.dumps({"text": row["text_raw"] if row else ""}, ensure_ascii=False))',
  ].join(String.fromCharCode(10))
  const probe = spawnSync(py, ['-c', probeScript, db], { env, encoding: 'utf8' })
  assert.equal(probe.status, 0, probe.stderr)
  const saved = JSON.parse(probe.stdout).text
  for (const part of ['上次交接（四段）', '状态：我们正在做自动 handoff 的集成测试', '试过压缩，会失真', '写四段交接']) {
    assert.ok(saved.includes(part), `写进库的交接缺一块：${part}｜${saved}`)
  }
  assert.ok(!saved.includes('（没说下一步）'), `下一步没抽出来：${saved}`)
  // 自我层叙事只放摘要（状态 + 下一步），不把整篇四段塞进卡里
  const narrative = readFileSync(join(dir, 'soul', 'narrative.md'), 'utf8')

  assert.ok(narrative.includes('状态：'), '叙事里没有状态')
  assert.ok(narrative.includes('写四段交接'), `叙事里没有下一步｜${narrative}`)
  assert.ok(!narrative.includes('死路：'), '叙事不该装整篇四段')

  console.log('dsh-pmem handoff ok')
} finally {
  try { listeners['agent/disposed']({ agent }) } catch { /* ignore */ }
  plugin.stop()
  rmSync(dir, { recursive: true, force: true })
}
