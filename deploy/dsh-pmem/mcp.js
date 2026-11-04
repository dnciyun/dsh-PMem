/**
 * 极简 MCP 探针/客户端：只做 initialize / tools/list / tools/call。
 *
 * 正式接入走 DSH 官方的 `@deepseek-ai/dsh-mcp-client`（见 cordis.patch.yml），
 * 这个文件只用于：
 *   - M9.6 的 test-mcp.mjs；
 *   - 设置页/诊断里的 probeMcp() 状态检查。
 *
 * 不注册工具、不常驻；用完就 kill。
 */

import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'

const PROTOCOL_VERSION = '2024-11-05'

function parseToolText(result) {
  const text = result?.content?.find?.(item => item?.type === 'text')?.text
  if (typeof text !== 'string') return null
  try {
    return JSON.parse(text)
  } catch {
    return { raw: text }
  }
}

export function createMcpClient(config, { timeoutMs = 15000 } = {}) {
  const child = spawn(String(config.python || 'python'), [
    '-m', 'pmem.gate.mcp_server',
    '--db', String(config.db),
  ], {
    cwd: config.cwd,
    env: {
      ...process.env,
      PYTHONPATH: config.src,
      PYTHONIOENCODING: 'utf-8',
    },
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  })

  const pending = new Map()
  const stderr = []
  let nextId = 1
  let closed = false

  const failAll = error => {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer)
      entry.reject(error)
    }
    pending.clear()
  }

  child.on('error', error => failAll(error))
  child.on('exit', code => {
    closed = true
    failAll(new Error(`MCP server exited (${code})${stderr.length ? `: ${stderr.slice(-3).join(' ')}` : ''}`))
  })
  child.stderr.on('data', chunk => {
    stderr.push(String(chunk).trim())
    if (stderr.length > 20) stderr.shift()
  })

  const lines = createInterface({ input: child.stdout })
  lines.on('line', line => {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      return
    }
    if (message.id === undefined || message.id === null) return
    const entry = pending.get(message.id)
    if (!entry) return
    pending.delete(message.id)
    clearTimeout(entry.timer)
    if (message.error) entry.reject(new Error(message.error.message || 'MCP error'))
    else entry.resolve(message.result)
  })

  const request = (method, params = {}) => new Promise((resolve, reject) => {
    if (closed) {
      reject(new Error('MCP server 已退出'))
      return
    }
    const id = nextId++
    const timer = setTimeout(() => {
      pending.delete(id)
      reject(new Error(`MCP ${method} 超时（${timeoutMs}ms）`))
    }, timeoutMs)
    pending.set(id, { resolve, reject, timer })
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
  })

  const notify = (method, params = {}) => {
    if (!closed) child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n')
  }

  return {
    async initialize() {
      const result = await request('initialize', {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: 'dsh-pmem-probe', version: '0.1.0' },
      })
      notify('notifications/initialized')
      return result
    },
    listTools() {
      return request('tools/list', {})
    },
    async callTool(name, args = {}) {
      const result = await request('tools/call', { name, arguments: args })
      return { isError: Boolean(result?.isError), value: parseToolText(result) }
    },
    close() {
      if (closed) return
      closed = true
      try { child.kill() } catch { /* already gone */ }
      failAll(new Error('MCP client closed'))
    },
    get stderr() { return stderr.join('\n') },
  }
}

export async function probeMcp(config, options = {}) {
  const started = Date.now()
  const client = createMcpClient(config, options)
  try {
    const info = await client.initialize()
    const listed = await client.listTools()
    const tools = (listed?.tools ?? []).map(tool => tool.name)
    return {
      ok: true,
      server: info?.serverInfo ?? null,
      count: tools.length,
      tools,
      latency_ms: Date.now() - started,
    }
  } catch (error) {
    return {
      ok: false,
      count: 0,
      tools: [],
      latency_ms: Date.now() - started,
      error: String(error?.message ?? error),
      stderr: client.stderr,
    }
  } finally {
    client.close()
  }
}
