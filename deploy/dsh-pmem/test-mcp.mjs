/**
 * MCP 接入契约测试：临时库 → initialize → tools/list（30）→ tools/call。
 * 不启动 DSH，不碰真实 data/pmem.db；DSH_HOME 指向临时目录，也不碰真实 config.toml。
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
// 配置隔离：MCP server / CLI 都按 DSH_HOME 找 ~/.dsh/pmem/config.toml，
// 这里指到临时目录 → 读到的全是默认值（gate.render.cards=false 等），不依赖本机配置。
process.env.DSH_HOME = join(dir, 'dsh-home')
const config = { python: py, cwd: root, src: join(root, 'src'), db }

const env = { ...process.env, PYTHONPATH: join(root, 'src'), PYTHONIOENCODING: 'utf-8' }
const init = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, 'init'], { env, encoding: 'utf8' })
assert.equal(init.status, 0, init.stderr || init.stdout)

try {
  const probe = await probeMcp(config)
  assert.equal(probe.ok, true, probe.error || probe.stderr)
  assert.equal(probe.count, 30, `工具数不是 30：${probe.count}`)
  assert.ok(probe.tools.includes('memory_search'))
  assert.ok(probe.tools.includes('memory_stats'))
  assert.ok(probe.tools.includes('memory_whale_prep'))
  assert.ok(probe.tools.includes('memory_whale_finish'))
  assert.ok(probe.tools.includes('memory_selfcheck'))
  assert.ok(probe.tools.includes('memory_answer_plan'))
  assert.ok(probe.tools.includes('memory_verify_answer'))
  assert.ok(probe.tools.includes('memory_render'), '缺少统一渲染入口 memory_render')

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

    // 统一渲染器（P0-2）：默认配置全关时，memory_render 输出与 0.38.0 的 render_context
    // 逐字一致（cards/contract 都不传，relative_dates 关），宿主/MCP/CLI 共用同一条入口。
    const rendered = await client.callTool('memory_render', { query: 'MCP 集成测试记忆', k: 5 })
    assert.equal(rendered.isError, false, JSON.stringify(rendered.value))
    assert.equal(rendered.value?.ok, true, JSON.stringify(rendered.value))
    const renderData = rendered.value?.data ?? {}
    assert.equal(renderData.cards, null, '默认 gate.render.cards 关：cards 应为 null')
    assert.equal(renderData.contract, null, '默认 answer.contract 关：contract 应为 null')
    assert.equal(renderData.relative_dates, false, '默认相对日期关')
    assert.ok(String(renderData.text).includes('PMEM_MEMORY_DATA'), '渲染文本不是统一证据块')
    assert.ok(String(renderData.text).includes('MCP 集成测试记忆'), '渲染文本里没有刚写的那条记忆')
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
