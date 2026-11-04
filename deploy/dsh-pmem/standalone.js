/**
 * 星图的「独立浏览器」通道（M9.7c）。
 *
 * 为什么需要它：DSH 的所有插件路由都过登录闸，用系统浏览器打开必然 401；
 * 而桌面壳的 `setWindowOpenHandler` 只放行 https，页面里也没法直接拉起浏览器。
 * 所以这里自己起一个**只绑 127.0.0.1**、只认几个固定路径的小服务，
 * 由插件（Node 侧）去拉系统浏览器。窗口就完全是浏览器自己的，不占 DSH。
 *
 * 安全边界（比主服务更窄，因为它不带 DSH 的登录闸）：
 *   1. 只绑回环，端口随机；
 *   2. 只认 GET/HEAD，只认 5 个固定路径，不接受任何路径参数；
 *   3. 不提供写接口，不返回配置，不返回记忆正文以外的任何东西；
 *   4. `/data.json` 每次现读磁盘，不缓存——DSH 那句「重绘」立刻能看到。
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { createServer } from 'node:http'

const HTML_TYPE = 'text/html; charset=utf-8'
const JS_TYPE = 'text/javascript; charset=utf-8'
const CSS_TYPE = 'text/css; charset=utf-8'
const JSON_TYPE = 'application/json; charset=utf-8'

const ASSETS = new Map([
  ['/starmap/space.js', ['space.js', JS_TYPE]],
  ['/starmap/ui.js', ['ui.js', JS_TYPE]],
  ['/starmap/space.css', ['space.css', CSS_TYPE]],
])

function send(res, status, type, body) {
  res.statusCode = status
  res.setHeader('content-type', type)
  res.setHeader('cache-control', 'no-store')
  res.setHeader('x-content-type-options', 'nosniff')
  res.end(body)
}

/** 把独立模式的配置注入页面。同一份 starmap.html，两处用，不复制逻辑。 */
export function injectStandaloneConfig(html, { version, dataUrl }) {
  const payload = JSON.stringify({
    standalone: true,
    dataUrl,
    eventsUrl: null,
    version: version ?? null,
  })
    .replace(/</g, '\\u003c')
    .replace(/-->/g, '--\\u003e')
  const tag = `<script>window.__PMEM_EMBED__=${payload}</script>`
  if (html.includes('</head>')) return html.replace('</head>', `${tag}\n</head>`)
  return tag + html
}

export function createStandalone({ pluginDir, starmapBase, version }) {
  let server = null
  let url = null
  let starting = null
  let closed = false

  function page() {
    const file = `${pluginDir}/starmap.html`
    if (!existsSync(file)) return null
    return injectStandaloneConfig(readFileSync(file, 'utf8'), {
      version,
      dataUrl: '/data.json',
    })
  }

  function handle(req, res) {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      return send(res, 405, 'text/plain; charset=utf-8', '只读')
    }
    const pathname = new URL(req.url ?? '/', 'http://127.0.0.1').pathname

    if (pathname === '/' || pathname === '/index.html') {
      const html = page()
      if (html === null) return send(res, 500, 'text/plain; charset=utf-8', '插件缺少 starmap.html')
      return send(res, 200, HTML_TYPE, html)
    }

    const asset = ASSETS.get(pathname)
    if (asset !== undefined) {
      const [name, type] = asset
      const file = `${pluginDir}/starmap/${name}`
      if (!existsSync(file)) return send(res, 404, 'text/plain; charset=utf-8', `缺少 ${name}`)
      return send(res, 200, type, readFileSync(file))
    }

    if (pathname === '/data.json') {
      const file = `${starmapBase}.json`
      if (!existsSync(file)) {
        return send(res, 404, JSON_TYPE, JSON.stringify({ ok: false, error: '还没有星图：先在 DSH 里打开一次，或跑 pmem star' }))
      }
      try {
        const graph = JSON.parse(readFileSync(file, 'utf8'))
        // 和 /dsh-pmem/api/starmap 保持同一个信封：界面层只认一种形状。
        return send(res, 200, JSON_TYPE, JSON.stringify({ ok: true, ...graph }))
      } catch (error) {
        return send(res, 500, JSON_TYPE, JSON.stringify({ ok: false, error: String(error?.message ?? error) }))
      }
    }

    return send(res, 404, 'text/plain; charset=utf-8', 'not found')
  }

  /** 起服务；已经在跑就直接返回同一个地址。 */
  function start() {
    if (closed) return Promise.reject(new Error('standalone 已关闭'))
    if (url) return Promise.resolve(url)
    if (starting) return starting
    starting = new Promise((resolve, reject) => {
      const instance = createServer(handle)
      instance.once('error', error => {
        starting = null
        reject(error)
      })
      instance.listen(0, '127.0.0.1', () => {
        const address = instance.address()
        if (address === null || typeof address === 'string') {
          instance.close()
          starting = null
          reject(new Error('拿不到监听端口'))
          return
        }
        server = instance
        url = `http://127.0.0.1:${address.port}/`
        // 不拖住进程：插件卸载后 DSH 该退就能退。
        instance.unref?.()
        resolve(url)
      })
    })
    return starting
  }

  function close() {
    closed = true
    starting = null
    url = null
    const instance = server
    server = null
    if (instance) {
      try { instance.close() } catch { /* 已经关了 */ }
    }
  }

  return {
    start,
    close,
    get url() { return url },
    handle,
  }
}

/** 用系统默认浏览器打开一个地址。只接受我们自己造出来的 http://127.0.0.1 地址。 */
export function openInBrowser(target) {
  return new Promise((resolve, reject) => {
    let text
    try {
      const parsed = new URL(target)
      if (parsed.protocol !== 'http:') throw new Error('只允许 http 地址')
      if (parsed.hostname !== '127.0.0.1' && parsed.hostname !== 'localhost') throw new Error('只允许本机地址')
      text = parsed.href
    } catch (error) {
      reject(error)
      return
    }

    const platform = process.platform
    const command = platform === 'win32' ? 'cmd' : platform === 'darwin' ? 'open' : 'xdg-open'
    // windows 上 `start` 会把第一个带引号的参数当成窗口标题，所以先给一个空标题。
    const args = platform === 'win32' ? ['/c', 'start', '', text] : [text]
    let settled = false
    let child
    try {
      child = spawn(command, args, { detached: true, stdio: 'ignore', windowsHide: true })
    } catch (error) {
      reject(error)
      return
    }
    child.once('error', error => {
      if (settled) return
      settled = true
      reject(error)
    })
    child.once('spawn', () => {
      if (settled) return
      settled = true
      try { child.unref() } catch { /* 已经退出 */ }
      resolve(true)
    })
  })
}
