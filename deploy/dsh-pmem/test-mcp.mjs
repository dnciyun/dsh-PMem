/**
 * MCP 接入契约测试：临时库 → initialize → tools/list（27）→ tools/call。
 * 不启动 DSH，不碰真实 data/pmem.db。
 */

import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createMcpClient, probeMcp } from './mcp.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const py = process.env.PMEM_PYTHON || 'python'
const dir = mkdtempSync(join(tmpdir(), 'pmem-mcp-'))
const db = join(dir, 'pmem.db')
const config = { python: py, cwd: root, src: join(root, 'src'), db }

const env = { ...process.env, PYTHONPATH: join(root, 'src'), PYTHONIOENCODING: 'utf-8' }
const init = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, 'init'], { env, encoding: 'utf8' })
assert.equal(init.status, 0, init.stderr || init.stdout)

try {
  const probe = await probeMcp(config)
  assert.equal(probe.ok, true, probe.error || probe.stderr)
  assert.equal(probe.count, 27, `工具数不是 27：${probe.count}`)
  assert.ok(probe.tools.includes('memory_search'))
  assert.ok(probe.tools.includes('memory_stats'))
  assert.ok(probe.tools.includes('memory_whale_prep'))
  assert.ok(probe.tools.includes('memory_whale_finish'))
assert.ok(probe.tools.includes('memory_selfcheck'))

  const client = createMcpClient(config)
  try {
    await client.initialize()
    const added = await client.callTool('memory_add', { text: 'MCP 集成测试记忆' })
    assert.equal(added.isError, false, JSON.stringify(added.value))
    assert.equal(added.value?.ok, true)

    const stats = await client.callTool('memory_stats')
    assert.equal(stats.isError, false, JSON.stringify(stats.value))
    assert.equal(stats.value?.ok, true)
    assert.equal(stats.value?.data?.memory_total, 1)
  } finally {
    client.close()
  }

  console.log(`dsh-pmem mcp ok（${probe.count} tools, ${probe.latency_ms} ms）`)
} finally {
  // Windows 上 Python 子进程退出可能比 kill() 慢一拍；给文件句柄一点时间。
  await new Promise(resolve => setTimeout(resolve, 300))
  try {
    rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 })
  } catch (error) {
    console.warn(`临时目录清理失败（不影响测试结果）：${error.message}`)
  }
}
