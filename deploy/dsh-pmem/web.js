/**
 * M9.7：宿主侧 Web 面。
 *
 * 三件事，都只在本机回环上：
 *   · `/dsh-pmem/api/*`      设置页要的数据（状态 / 卡 / 记忆 / 配置 / MCP）
 *   · `/dsh-pmem/starmap`    星图的独立页面（不占设置标签页，WebGL2 实时渲染）
 *   · `/dsh-pmem/starmap/*`  星图的静态资源（三份文件，白名单）
 *   · `/dsh-pmem/api/starmap/events`  SSE：新记忆写进库就通知页面重绘
 *
 * 安全边界（和 surfcore 同一条线，不自己发明）：
 *   1. 只接受回环地址；
 *   2. 写操作必须同源 + `application/json`，body ≤ 256 KB；
 *   3. 再过一次 DSH 的 `connection.requestRejection`；
 *   4. 只读白名单里的文件，不接受任何路径参数。
 *
 * 这里不制造第二份配置：运行时的开关写 `~/.dsh/pmem/runtime.json`，
 * 它覆盖 profile 里的默认值，但**不能**打开 profile 明确关掉的能力。
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createStandalone, openInBrowser } from './standalone.js'
import { dirname } from 'node:path'

const require = createRequire(import.meta.url)

const API = '/dsh-pmem/api'
const MAX_BODY = 256 * 1024

export const RUNTIME_DEFAULTS = Object.freeze({
  surface: true,
  starmap: true,
  starmapLive: true,
  budget: 300,
  surfaceCooldownMs: 90000,
  surfaceTimeoutMs: 1500,
  handoffTurns: 6,
  handoffMinMs: 600000,
  contextAuto: true,
})

/** 每个键的类型与范围。范围是守卫，不是建议值。 */
export const RUNTIME_SPEC = Object.freeze({
  surface: { type: 'boolean', label: '每轮浮现' },
  starmap: { type: 'boolean', label: '星图' },
  starmapLive: { type: 'boolean', label: '星图实时' },
  budget: { type: 'int', min: 120, max: 1200, step: 20, label: '醒来卡预算', unit: 'token' },
  surfaceCooldownMs: { type: 'int', min: 10000, max: 600000, step: 1000, label: '浮现冷却', unit: 'ms' },
  surfaceTimeoutMs: { type: 'int', min: 200, max: 5000, step: 100, label: '浮现超时', unit: 'ms' },
  handoffTurns: { type: 'int', min: 2, max: 100, step: 1, label: '落盘间隔', unit: '轮' },
  handoffMinMs: { type: 'int', min: 60000, max: 3600000, step: 60000, label: '落盘最小间隔', unit: 'ms' },
  contextAuto: { type: 'boolean', label: '上下文自动交接' },
})

function coerce(key, value) {
  const spec = RUNTIME_SPEC[key]
  if (!spec) return undefined
  if (spec.type === 'boolean') return typeof value === 'boolean' ? value : undefined
  const number = Number(value)
  if (!Number.isFinite(number)) return undefined
  return Math.min(spec.max, Math.max(spec.min, Math.trunc(number)))
}

/**
 * 读运行时文件。
 *
 * 返回 `{values, explicit}`：`values` 是合并默认值后的完整快照，`explicit` 是
 * **用户真的写过**的键。只有 explicit 的键才会覆盖宿主配置——否则第一次写
 * `runtime.json` 就会把 profile 里配好的 budget / handoffTurns 全部顶掉。
 */
export function readRuntime(file) {
  const values = { ...RUNTIME_DEFAULTS }
  const explicit = new Set()
  try {
    if (!existsSync(file)) return { values, explicit }
    const parsed = JSON.parse(readFileSync(file, 'utf8'))
    for (const key of Object.keys(RUNTIME_SPEC)) {
      const next = coerce(key, parsed?.[key])
      if (next !== undefined) {
        values[key] = next
        explicit.add(key)
      }
    }
  } catch {
    // 文件坏了就当没写过
  }
  return { values, explicit }
}

export function writeRuntime(file, patch) {
  // 只写显式给的键：文件本身就是"用户改过什么"的记录。
  let raw = {}
  try {
    if (existsSync(file)) raw = JSON.parse(readFileSync(file, 'utf8')) ?? {}
  } catch {
    raw = {}
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) raw = {}
  for (const key of Object.keys(RUNTIME_SPEC)) {
    const value = coerce(key, patch?.[key])
    if (value !== undefined) raw[key] = value
  }
  try {
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, `${JSON.stringify(raw, null, 2)}\n`, 'utf8')
  } catch {
    // 写不进去也要返回内存里的结果：设置页只是提示失败，不能崩
  }
  return readRuntime(file)
}

/**
 * 把运行时开关铺到宿主配置上。
 *
 * 这些值都是**每次用到时才读**的（refresh / pre-step / turn/end），所以改完立即生效，
 * 不需要重启。`injectCard` / `tools` / `commands` 是装配期的，不在这里。
 * 只应用 `explicit` 里的键，没写过的保持 profile 的值。
 */
export function applyRuntime(config, runtime) {
  const values = runtime?.values ?? runtime ?? {}
  const explicit = runtime?.explicit ?? new Set(Object.keys(values))
  if (explicit.has('surface')) config.surface = config.surfaceAllowed ? values.surface !== false : false
  for (const key of ['budget', 'surfaceCooldownMs', 'surfaceTimeoutMs', 'handoffTurns', 'handoffMinMs']) {
    if (explicit.has(key)) config[key] = values[key]
  }
  return config
}

function json(res, status, body) {
  const text = JSON.stringify(body)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(text)
}

function isLoopback(req) {
  const address = req.socket?.remoteAddress ?? ''
  return (
    address === '127.0.0.1' ||
    address === '::1' ||
    address === '::ffff:127.0.0.1' ||
    address === ''
  )
}

function headerOf(req, name) {
  return String(req.headers?.[name] ?? '')
}

/**
 * 同源判定：浏览器跨站请求一定带 Sec-Fetch-Site 或 Origin，本机 CLI 两个都不带。
 * 只看 Host 会漏（Host 可伪造），所以两者都要过。
 */
function sameOrigin(req) {
  const site = headerOf(req, 'sec-fetch-site').toLowerCase()
  if (site && site !== 'same-origin' && site !== 'none') return false
  const origin = headerOf(req, 'origin')
  if (!origin) return true
  try {
    const host = headerOf(req, 'host').toLowerCase()
    return new URL(origin).host.toLowerCase() === host
  } catch {
    return false
  }
}

function readBody(req, limit = MAX_BODY) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', chunk => {
      size += chunk.length
      if (size > limit) {
        reject(new Error('body 超过 256 KB'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

/** 只读一列 seq，用来判断"库里有没有新记忆"。没有 node:sqlite 就退化成 null。 */
function openSeqReader(dbPath) {
  try {
    // 延迟 require：老 Node 没有 node:sqlite，这里必须能降级。
    const { DatabaseSync } = require('node:sqlite')
    const db = new DatabaseSync(dbPath, { readOnly: true })
    const stmt = db.prepare('SELECT MAX(seq) AS seq FROM event_log')
    return {
      read() {
        try {
          const row = stmt.get()
          return Number(row?.seq) || 0
        } catch {
          return null
        }
      },
      close() {
        try { db.close() } catch { /* 已关 */ }
      },
    }
  } catch {
    return null
  }
}

export function createWeb({ config, callPmem, version, runtimeFile, starmapBase, pluginDir, openBrowser }) {
  const loaded = readRuntime(runtimeFile)
  let runtime = loaded.values
  let explicit = loaded.explicit
  let regenerating = null
  let lastRegen = 0

  // profile 里明确关掉的能力：运行时只能关，不能开。
  config.surfaceAllowed = config.surface !== false
  applyRuntime(config, { values: runtime, explicit })

  const disabled = {
    surface: config.surfaceAllowed !== true,
  }

  // 星图的浏览器通道：只绑回环、端口随机、只认固定路径。
  const standalone = createStandalone({ pluginDir, starmapBase, version })
  const logFile = `${dirname(runtimeFile)}/web.log`
  const requests = []
  const note = entry => {
    requests.push(entry)
    if (requests.length > 200) requests.shift()
    try {
      appendFileSync(logFile, `${JSON.stringify(entry)}\n`, 'utf8')
    } catch {
      // 日志写不进去不影响请求
    }
  }

  const publicRuntime = () => ({
    ...runtime,
    spec: RUNTIME_SPEC,
    // profile 明确关掉的能力，运行时不能再打开
    locked: { surface: disabled.surface },
  })

  const stats = async () => {
    const payload = await callPmem(config, ['stats'], { json: true, timeout: 15000 })
    return payload?.data ?? null
  }

  const wake = async (budget) => {
    const argv = ['wake']
    if (budget) argv.push('--budget', String(budget))
    const payload = await callPmem(config, argv, { json: true, timeout: 20000 })
    return payload?.data?.text ?? ''
  }

  const surfaceReceipts = async () => {
    const payload = await callPmem(config, ['surface', '--session', 'settings', '--receipts'], {
      json: true,
      timeout: 15000,
    })
    return payload?.data ?? null
  }

  const tokenStats = async () => {
    const payload = await callPmem(config, ['receipt', '--stats'], { json: true, timeout: 15000 })
    return payload?.data ?? null
  }

  const regenerate = async () => {
    if (regenerating) return regenerating
    if (Date.now() - lastRegen < 1000) return null
    lastRegen = Date.now()
    regenerating = (async () => {
      try {
        // M9.7c：增量模式。旧星位置不动（页面不会跳），只写 JSON——
        // HTML/SVG 是离线产物，每 1.5 秒重画一份 370 KB 的单文件没有意义。
        await callPmem(config, ['star', '--incremental'], { timeout: 90000 })
        return true
      } catch {
        return false
      } finally {
        regenerating = null
      }
    })()
    return regenerating
  }

  const readStarmap = () => {
    try {
      const file = `${starmapBase}.json`
      if (!existsSync(file)) return null
      return JSON.parse(readFileSync(file, 'utf8'))
    } catch {
      return null
    }
  }

  const starmapInfo = () => {
    try {
      const file = `${starmapBase}.json`
      if (!existsSync(file)) return { ready: false }
      const info = statSync(file)
      const data = JSON.parse(readFileSync(file, 'utf8'))
      return {
        ready: true,
        generated_at: data?.meta?.generated_at ?? null,
        nodes: data?.nodes?.length ?? 0,
        links: data?.links?.length ?? 0,
        shown: data?.meta?.shown ?? null,
        total: data?.meta?.total ?? null,
        bytes: info.size,
      }
    } catch {
      return { ready: false }
    }
  }

  const routes = []

  function route(kind, path, handler) {
    routes.push({ kind, path, handler })
  }

  async function guarded(req, res, handler) {
    const started = Date.now()
    // 每条请求都记一笔：星图打不开时，这份日志是唯一能分辨
    // "路由没注册(404)" / "被登录闸拦了(401)" / "处理时炸了(500)" 的东西。
    res.on('finish', () => {
      note({
        ts: new Date().toISOString(),
        method: req.method,
        path: String(req.url ?? '').slice(0, 200),
        status: res.statusCode,
        ms: Date.now() - started,
      })
    })
    if (!isLoopback(req)) return json(res, 403, { ok: false, error: '只接受本机回环访问' })
    if (req.method !== 'GET') {
      if (!sameOrigin(req)) return json(res, 403, { ok: false, error: '跨站请求被拒绝' })
      if (!headerOf(req, 'content-type').toLowerCase().includes('application/json')) {
        return json(res, 415, { ok: false, error: '写操作必须是 application/json' })
      }
    }
    try {
      const conn = typeof handler.ctx?.get === 'function' ? handler.ctx.get('connection') : null
      if (conn && typeof conn.requestRejection === 'function') {
        const code = conn.requestRejection(req)
        if (code !== undefined && code !== null && code !== false) {
          res.statusCode = typeof code === 'number' ? code : 403
          res.end()
          return
        }
      }
      await handler.run(req, res)
    } catch (error) {
      try { json(res, 500, { ok: false, error: String(error?.message ?? error) }) } catch { /* 已断 */ }
    }
  }

  // ---- 自检：星图打不开时先看这里 -------------------------------------------
  route('exact', `${API}/debug`, async (req, res) => {
    return json(res, 200, {
      ok: true,
      version,
      runtime: publicRuntime(),
      config: {
        surfaceAllowed: config.surfaceAllowed,
        surface: config.surface,
        budget: config.budget,
        surfaceCooldownMs: config.surfaceCooldownMs,
        surfaceTimeoutMs: config.surfaceTimeoutMs,
        handoffTurns: config.handoffTurns,
        handoffMinMs: config.handoffMinMs,
        db: config.db,
        starmapBase,
      },
      starmap: starmapInfo(),
      requests: requests.slice(-40),
      logFile,
    })
  })

  // ---- 状态 ----------------------------------------------------------------
  route('exact', `${API}/status`, async (req, res) => {
    const [dbStats, card, receipts, tokens] = await Promise.all([
      stats().catch(() => null),
      wake(config.budget).catch(() => ''),
      surfaceReceipts().catch(() => null),
      tokenStats().catch(() => null),
    ])
    return json(res, 200, {
      ok: true,
      version,
      runtime: publicRuntime(),
      db: dbStats,
      wake: { tokens: Math.ceil(card.length / 3), chars: card.length, head: card.split('\n')[1] ?? '' },
      surface: receipts,
      tokens,
      starmap: starmapInfo(),
      handoff: {
        enabled: config.handoff !== false,
        turns: config.handoffTurns,
        minMs: config.handoffMinMs,
      },
      mcp: { serverName: 'pmem', tools: 20 },
    })
  })

  route('exact', `${API}/wake`, async (req, res) => {
    const url = new URL(req.url, 'http://localhost')
    const budget = Number(url.searchParams.get('budget')) || undefined
    const text = await wake(budget)
    return json(res, 200, { ok: true, text })
  })

  route('exact', `${API}/memories`, async (req, res) => {
    const url = new URL(req.url, 'http://localhost')
    const query = String(url.searchParams.get('q') ?? '').slice(0, 200)
    const k = Math.min(50, Math.max(1, Number(url.searchParams.get('k')) || 20))
    if (!query.trim()) {
      const payload = await callPmem(config, ['search', '记忆', '--k', String(k)], {
        json: true,
        timeout: 15000,
      }).catch(() => null)
      return json(res, 200, { ok: true, query, items: payload?.data?.items ?? [] })
    }
    const payload = await callPmem(config, ['search', query, '--k', String(k)], {
      json: true,
      timeout: 15000,
    })
    return json(res, 200, { ok: Boolean(payload?.ok), query, items: payload?.data?.items ?? [] })
  })

  route('exact', `${API}/config`, async (req, res) => {
    if (req.method === 'GET') {
      return json(res, 200, { ok: true, runtime: publicRuntime() })
    }
    const raw = await readBody(req)
    let patch = {}
    try {
      patch = JSON.parse(raw || '{}')
    } catch {
      return json(res, 400, { ok: false, error: 'body 不是 JSON' })
    }
    if (patch?.surface === true && disabled.surface) {
      return json(res, 409, {
        ok: false,
        error: 'profile 里已经关掉了 surface，运行时不能打开（改 cordis.patch.yml）',
      })
    }
    const next = writeRuntime(runtimeFile, patch)
    runtime = next.values
    explicit = next.explicit
    applyRuntime(config, next)
    return json(res, 200, { ok: true, runtime: publicRuntime() })
  })

  route('exact', `${API}/mcp`, async (req, res) => {
    return json(res, 200, { ok: true, serverName: 'pmem', tools: 20, note: 'MCP 由官方 client 管理；这里是静态摘要' })
  })

  // ---- 星图 ----------------------------------------------------------------
  route('exact', '/dsh-pmem/starmap', async (req, res) => {
    const file = `${pluginDir}/starmap.html`
    if (!existsSync(file)) {
      res.statusCode = 500
      res.setHeader('content-type', 'text/plain; charset=utf-8')
      return res.end('插件缺少 starmap.html')
    }
    res.statusCode = 200
    res.setHeader('content-type', 'text/html; charset=utf-8')
    res.setHeader('cache-control', 'no-store')
    res.end(readFileSync(file))
  })

  // 星图的静态资源：白名单三个文件，不接受任何路径参数。
  const MAP_ASSETS = new Map([
    ['space.js', 'text/javascript; charset=utf-8'],
    ['ui.js', 'text/javascript; charset=utf-8'],
    ['space.css', 'text/css; charset=utf-8'],
  ])

  // 注意：DSH 的前缀匹配器规则是
  //   pathname === prefix || pathname.startsWith(prefix + '/')
  // 所以 path **不能**带尾斜杠，否则它去比 "/dsh-pmem/starmap//"，永远不中。
  route('prefix', '/dsh-pmem/starmap', async (req, res) => {
    const url = new URL(req.url, 'http://localhost')
    const name = url.pathname.slice('/dsh-pmem/starmap/'.length)
    // 前缀之外的精确路由（同一个前缀下还有 /api/...）不归这里管。
    if (name.includes('/')) {
      res.statusCode = 404
      res.setHeader('content-type', 'text/plain; charset=utf-8')
      return res.end('没有这个资源')
    }
    const type = MAP_ASSETS.get(name)
    if (!type) {
      res.statusCode = 404
      res.setHeader('content-type', 'text/plain; charset=utf-8')
      return res.end('没有这个资源')
    }
    const file = `${pluginDir}/starmap/${name}`
    if (!existsSync(file)) {
      res.statusCode = 404
      res.setHeader('content-type', 'text/plain; charset=utf-8')
      return res.end(`插件缺少 starmap/${name}`)
    }
    res.statusCode = 200
    res.setHeader('content-type', type)
    res.setHeader('cache-control', 'no-store')
    res.end(readFileSync(file))
  })

  route('exact', `${API}/starmap`, async (req, res) => {
    const data = readStarmap()
    if (!data) return json(res, 404, { ok: false, error: '还没有生成星图，先跑 pmem star' })
    return json(res, 200, { ok: true, ...data })
  })

  // ---- 在系统浏览器里打开（不占 DSH，也不走登录闸） ------------------------
  route('exact', `${API}/starmap/url`, async (req, res) => {
    return json(res, 200, { ok: true, running: standalone.url !== null, url: standalone.url })
  })

  route('exact', `${API}/starmap/open`, async (req, res) => {
    if (req.method !== 'POST') return json(res, 405, { ok: false, error: '只接受 POST' })
    if (runtime.starmap === false) {
      return json(res, 409, { ok: false, error: '星图已关：先在概览里把开关打开' })
    }
    await regenerate().catch(() => null)
    let target
    try {
      target = await standalone.start()
    } catch (error) {
      return json(res, 500, { ok: false, error: `独立服务起不来：${String(error?.message ?? error)}` })
    }
    const opener = typeof openBrowser === 'function' ? openBrowser : openInBrowser
    try {
      await opener(target)
    } catch (error) {
      // 拉不起来也不要紧：地址已经能用，让设置页把它显示出来自己点。
      return json(res, 200, {
        ok: true,
        opened: false,
        url: target,
        warning: `浏览器没拉起来：${String(error?.message ?? error)}`,
      })
    }
    return json(res, 200, { ok: true, opened: true, url: target })
  })

  route('exact', `${API}/starmap/regen`, async (req, res) => {
    if (req.method !== 'POST') return json(res, 405, { ok: false, error: '只接受 POST' })
    const done = await regenerate()
    return json(res, 200, { ok: Boolean(done), starmap: starmapInfo() })
  })

  route('exact', `${API}/starmap/view`, async (req, res) => {    const file = `${starmapBase}.html`
    if (!existsSync(file)) {
      res.statusCode = 404
      res.setHeader('content-type', 'text/plain; charset=utf-8')
      return res.end('还没有星图：运行 pmem star 生成一次')
    }
    res.statusCode = 200
    res.setHeader('content-type', 'text/html; charset=utf-8')
    res.setHeader('cache-control', 'no-store')
    res.end(readFileSync(file))
  })

  route('exact', `${API}/starmap/events`, async (req, res) => {
    res.writeHead(200, {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-store',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    })
    res.write('retry: 3000\n\n')
    const send = (event, data) => {
      try {
        res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
      } catch { /* 客户端走了 */ }
    }
    send('hello', { ok: true, live: runtime.starmapLive, info: starmapInfo() })

    let closed = false
    let pending = null
    const reader = openSeqReader(config.db)
    let last = reader?.read() ?? 0

    const cleanup = () => {
      if (closed) return
      closed = true
      clearInterval(timer)
      if (pending) clearTimeout(pending)
      try { reader?.close() } catch { /* 已关 */ }
    }

    const timer = setInterval(async () => {
      if (closed) return
      if (!runtime.starmap || !runtime.starmapLive) return
      const seq = reader?.read()
      if (seq === null || seq === undefined) return
      if (seq <= last) return
      last = seq
      if (pending) return
      // 去抖：一次写入常常连着好几条事件，等 1.5s 再重绘一次就够。
      pending = setTimeout(async () => {
        pending = null
        if (closed) return
        const done = await regenerate()
        if (done) send('refresh', { seq, info: starmapInfo() })
      }, 1500)
    }, 2000)
    timer.unref?.()

    req.on('close', cleanup)
    req.on('error', cleanup)
  })

  return {
    routes,
    runtimeFile,
    get runtime() {
      return publicRuntime()
    },
    get live() {
      return runtime
    },
    /** 挂到 ctx.webServer 上；返回 disposer。 */
    register(ctx) {
      const disposers = []
      for (const item of routes) {
        disposers.push(
          ctx.webServer.register({
            kind: item.kind,
            path: item.path,
            handler: (req, res) => guarded(req, res, { ctx, run: item.handler }),
          }),
        )
      }
      return () => {
        for (const dispose of disposers.reverse()) {
          try { dispose?.() } catch { /* 卸载路径不能抛 */ }
        }
        try { standalone.close() } catch { /* 已经关了 */ }
      }
    },
    starmapInfo,
    regenerate,
    standalone,
  }
}
