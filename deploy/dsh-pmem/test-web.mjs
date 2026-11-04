/**
 * M9.7 宿主 Web 面的集成测试：真起一个 HTTP server，打真实路由。
 *
 * 覆盖：状态 / 醒来卡 / 记忆检索 / 配置读写 / 星图 JSON + 页面 + 壳页 /
 *       跨站与 content-type 拦截 / SSE 新增记忆后 refresh。
 *
 * 不碰真实 data/pmem.db，也不改 ~/.dsh/pmem/runtime.json。
 */

import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply } from './index.js'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const py = process.env.PMEM_PYTHON || 'python'
const dir = mkdtempSync(join(tmpdir(), 'pmem-web-'))
const db = join(dir, 'pmem.db')
const env = { ...process.env, PYTHONPATH: join(root, 'src'), PYTHONIOENCODING: 'utf-8' }

const run = args => spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, ...args], { env, encoding: 'utf8' })

const init = run(['init'])
assert.equal(init.status, 0, init.stderr || init.stdout)
const added = run(['--json', 'add', '我们第一次一起改这个项目', '--group', '我们/经历', '--grade', 'A'])
assert.equal(added.status, 0, added.stderr)

mkdirSync(join(dir, 'data'), { recursive: true })
const star = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, 'star', '--no-png', '--out', join(dir, 'data', 'starmap')], {
  env,
  encoding: 'utf8',
})
assert.equal(star.status, 0, star.stderr || star.stdout)

const routes = []
const ctx = {
  systemPrompt: { context: () => () => {} },
  tools: { register: () => () => {} },
  commands: { register: () => () => {} },
  on: () => () => {},
  get: name => (name === 'connection' ? { requestRejection: () => undefined } : undefined),
  webServer: {
    register(item) {
      routes.push(item)
      return () => {
        const at = routes.indexOf(item)
        if (at >= 0) routes.splice(at, 1)
      }
    },
  },
}

let openedUrl = null
const plugin = apply(ctx, {
  db,
  cwd: dir,
  openBrowser: async url => { openedUrl = url; return true },
  runtimeFile: join(dir, 'runtime.json'),
  refreshMs: 60 * 60 * 1000,
  budget: 300,
  handoff: false,
})

// 和 DSH 的 webServer 同一套匹配规则：先 exact，再 prefix。
const server = createServer((req, res) => {
  const path = new URL(req.url, 'http://127.0.0.1').pathname
  const exact = routes.find(item => item.kind === 'exact' && item.path === path)
  // 和 dsh-host-webserver 一模一样：等于，或者以 "前缀/" 开头。
  // 之前这里写成裸 startsWith，才让「前缀带尾斜杠」的 bug 溜过去了。
  const prefix = routes
    .filter(item => item.kind === 'prefix' && (path === item.path || path.startsWith(item.path + '/')))
    .sort((a, b) => b.path.length - a.path.length)[0]
  const hit = exact ?? prefix
  if (!hit) {
    res.statusCode = 404
    res.end('not found')
    return
  }
  hit.handler(req, res)
})

await new Promise(done => server.listen(0, '127.0.0.1', done))
const port = server.address().port
const base = `http://127.0.0.1:${port}`

async function get(path) {
  const res = await fetch(base + path, { headers: { accept: 'application/json' } })
  const text = await res.text()
  let body = null
  try { body = JSON.parse(text) } catch { body = text }
  return { status: res.status, body }
}

async function post(path, body, headers = {}) {
  const res = await fetch(base + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
  const text = await res.text()
  let parsed = null
  try { parsed = JSON.parse(text) } catch { parsed = text }
  return { status: res.status, body: parsed }
}
let stream = null


try {
  const status = await get('/dsh-pmem/api/status')
  assert.equal(status.status, 200)
  assert.equal(status.body.ok, true)
  assert.ok(status.body.db?.memory_total >= 1, `库不对：${JSON.stringify(status.body.db)}`)
  assert.equal(status.body.runtime.surface, true)
  assert.equal(status.body.starmap.ready, true)
  assert.ok(status.body.starmap.nodes > 0, '星图节点为空')

  const wake = await get('/dsh-pmem/api/wake')
  assert.equal(wake.status, 200)
  assert.ok((wake.body.text.includes('PMEM_WAKE') || wake.body.text.includes('PMEM_SOUL')), '没有醒来卡')

  const memories = await get('/dsh-pmem/api/memories?q=' + encodeURIComponent('项目'))
  assert.equal(memories.status, 200)
  assert.ok(Array.isArray(memories.body.items), 'items 不是数组')

  const config = await get('/dsh-pmem/api/config')
  assert.equal(config.body.runtime.surface, true)
  const off = await post('/dsh-pmem/api/config', { surface: false })
  assert.equal(off.status, 200)
  assert.equal(off.body.runtime.surface, false, '关不掉 surface')
  const on = await post('/dsh-pmem/api/config', { surface: true })
  assert.ok(on.body && on.body.runtime, `on 响应不对：${JSON.stringify(on)}`)
  assert.equal(on.body.runtime.surface, true, '打不开 surface')

  // 数值参数：写进去要生效，越界要夹住，而且要落进 config（下一轮就用新值）
  const budget = await post('/dsh-pmem/api/config', { budget: 500 })
  assert.equal(budget.body.runtime.budget, 500)
  const clamped = await post('/dsh-pmem/api/config', { budget: 99999 })
  assert.equal(clamped.body.runtime.budget, 1200, '越界的预算没被夹住')
  const debug = await get('/dsh-pmem/api/debug')
  assert.equal(debug.status, 200)
  assert.equal(debug.body.config.budget, 1200, '运行时预算没有铺到 config')
  assert.ok(Array.isArray(debug.body.requests) && debug.body.requests.length > 0, '自检没有请求日志')
  assert.ok(debug.body.requests.some(item => item.path.includes('/api/config')), '请求日志里没有 config')

  const crossSite = await post('/dsh-pmem/api/config', { surface: false }, { origin: 'https://evil.example' })
  assert.equal(crossSite.status, 403, '跨站写请求没被拦')

  const plain = await fetch(base + '/dsh-pmem/api/config', {
    method: 'POST',
    headers: { 'content-type': 'text/plain' },
    body: '{}',
  })
  assert.equal(plain.status, 415, '非 JSON 写请求没被拦')

  const map = await get('/dsh-pmem/api/starmap')
  assert.equal(map.status, 200)
  assert.ok(map.body.nodes.length > 0, '星图 JSON 为空')

  const view = await fetch(base + '/dsh-pmem/api/starmap/view')
  assert.equal(view.status, 200)
  assert.ok((await view.text()).includes('<html'), '星图页不是 HTML')

  const shell = await fetch(base + '/dsh-pmem/starmap')
  assert.equal(shell.status, 200)
  const shellText = await shell.text()
  assert.ok(shellText.includes('海马体星图'), '星图壳页不对')
  assert.ok(shellText.includes('./starmap/space.js'), '壳页没有引用渲染器')
  assert.ok(shellText.includes('./starmap/ui.js'), '壳页没有引用界面层')
  assert.ok(shellText.includes('./starmap/space.css'), '壳页没有引用样式')

  // 星图的静态资源：三个文件名白名单，其它一律 404。
  const assets = {}
  for (const [name, type] of [
    ['space.js', 'text/javascript'],
    ['ui.js', 'text/javascript'],
    ['space.css', 'text/css'],
  ]) {
    const asset = await fetch(base + '/dsh-pmem/starmap/' + name)
    assert.equal(asset.status, 200, name + ' 取不到')
    assert.ok(String(asset.headers.get('content-type')).includes(type), name + ' 类型不对')
    const text = await asset.text()
    assert.ok(text.length > 1000, name + ' 内容太短')
    assets[name] = text
  }
  // 来源字段要一路从库走到 JSON，界面层才可能给日记派生的记忆单独上色；
  // 渲染器和图例得同时存在，缺一边就是一种无法解释的颜色。
  for (const node of map.body.nodes.filter(item => item.type === 'memory')) {
    assert.equal(typeof node.source_kind, 'string', node.id + ' 缺 source_kind')
  }
  assert.ok(assets['space.js'].includes('JOURNAL_COLOR'), '渲染器没有日记派生颜色')
  assert.ok(assets['ui.js'].includes('日记派生'), '图例里没有日记派生')
  const ghost = await fetch(base + '/dsh-pmem/starmap/web.js')
  assert.equal(ghost.status, 404, '白名单外的文件不能给')
  // 前缀必须按 "/前缀/" 匹配：多了同名前缀的路径不能被吞掉。
  const neighbour = await fetch(base + '/dsh-pmem/starmapx')
  assert.equal(neighbour.status, 404, '前缀匹配把同名前缀的路径也吃掉了')
  const escape = await fetch(base + '/dsh-pmem/starmap/%2e%2e/web.js')
  assert.equal(escape.status, 404, '路径穿越没被挡住')

  // 数据契约：界面层靠这些字段活着，少一个就是白屏。
  for (const node of map.body.nodes) {
    for (const key of ['x3', 'y3', 'z3', 't', 'size', 'glow', 'spike']) {
      assert.equal(typeof node[key], 'number', node.id + ' 缺 ' + key)
    }
  }
  assert.ok(map.body.dust.length > 0, '没有星尘：日志没有被画进星图')
  assert.ok(map.body.dust.every(d => typeof d.x3 === 'number'), '星尘缺坐标')
  assert.ok(Array.isArray(map.body.history) && map.body.history.length > 0, '没有时间轴历史')
  assert.equal(map.body.shape.kind, 'organic')
  assert.equal(map.body.shape.layout, 'spiral-disk')
  assert.ok(map.body.space.sectors >= 1, '没有分组扇区')

  // ---- 夜间流水：设置面板「夜间」标签的数据源 ----
  // 独立页面已拆掉（新窗口/整页导航过不了桌面壳和登录闸），这条路由不能悄悄回来。
  const nightPage = await get('/dsh-pmem/night')
  assert.equal(nightPage.status, 404, '夜间独立页面应该已经拆掉')

  const sleepLog = await get('/dsh-pmem/api/sleep')
  assert.equal(sleepLog.status, 200, 'sleep log 取不到')
  assert.equal(sleepLog.body.ok, true)
  assert.ok(Array.isArray(sleepLog.body.data?.runs), `sleep log 不是 runs 数组：${JSON.stringify(sleepLog.body)}`)
  for (const run of sleepLog.body.data.runs) {
    assert.equal(typeof run.day, 'string', 'run 缺 day')
    assert.equal(typeof run.status, 'string', 'run 缺 status')
    assert.ok(Array.isArray(run.stages), 'run.stages 不是数组')
    for (const stage of run.stages) {
      assert.equal(typeof (stage.name ?? stage.stage), 'string', '阶段缺名字')
      assert.equal(typeof stage.skipped, 'boolean', '阶段缺 skipped')
    }
  }

  // 主动入睡：POST 才给，GET 405；跑完要回写一条 ok 的 run（空库里也一样）。
  const sleepNowGet = await get('/dsh-pmem/api/sleep/now')
  assert.equal(sleepNowGet.status, 405, '主动入睡只接受 POST')
  const sleepNow = await post('/dsh-pmem/api/sleep/now', {})
  assert.equal(sleepNow.status, 200, '主动入睡失败')
  assert.equal(sleepNow.body.ok, true, JSON.stringify(sleepNow.body))
  assert.equal(typeof sleepNow.body.data?.status, 'string', '主动入睡没回 status')
  const afterSleep = await get('/dsh-pmem/api/sleep')
  assert.ok(afterSleep.body.data.runs.length >= 1, '跑完一轮睡眠后历史里没有记录')

  // ---- 浏览器通道：独立服务 + 拉起默认浏览器 ----
  const notPost = await fetch(base + '/dsh-pmem/api/starmap/open')
  assert.equal(notPost.status, 405, '打开星图只接受 POST')

  openedUrl = null
  const opened = await fetch(base + '/dsh-pmem/api/starmap/open', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  })
  assert.equal(opened.status, 200, '打开星图失败')
  const openBody = await opened.json()
  assert.equal(openBody.ok, true, JSON.stringify(openBody))
  assert.equal(openBody.opened, true, '没有真正调用拉起浏览器')
  assert.equal(openBody.url, openedUrl, '返回的地址和真正拉起的地址不一致')
  assert.match(openBody.url, /^http:\/\/127\.0\.0\.1:\d+\/$/, `地址不对：${openBody.url}`)

  // 独立服务只认固定路径，而且真的能出图
  const standaloneUrl = openBody.url
  const solo = await fetch(standaloneUrl)
  assert.equal(solo.status, 200, '独立页打不开')
  const soloHtml = await solo.text()
  assert.ok(soloHtml.includes('__PMEM_EMBED__'), '独立页没有注入独立模式配置')
  assert.ok(soloHtml.includes('./starmap/space.js'), '独立页没有引用渲染器')
  const soloData = await fetch(standaloneUrl + 'data.json')
  assert.equal(soloData.status, 200, '独立页取不到数据')
  const soloJson = await soloData.json()
  assert.ok(Array.isArray(soloJson.nodes) && soloJson.nodes.length > 0, '独立页数据为空')
  for (const name of ['starmap/space.js', 'starmap/ui.js', 'starmap/space.css']) {
    const asset = await fetch(standaloneUrl + name)
    assert.equal(asset.status, 200, `独立页缺 ${name}`)
  }
  const soloGhost = await fetch(standaloneUrl + 'dsh-pmem/api/debug')
  assert.equal(soloGhost.status, 404, '独立服务不能代理别的路由')
  const soloWrite = await fetch(standaloneUrl + 'data.json', { method: 'POST' })
  assert.equal(soloWrite.status, 405, '独立服务必须只读')

  // 再问一次地址：应该复用同一个端口，不重复起服务
  const again = await fetch(base + '/dsh-pmem/api/starmap/url').then(r => r.json())
  assert.equal(again.ok, true)
  assert.equal(again.url, standaloneUrl, '第二次取地址时又起了一个服务')

  // ---- SSE：新记忆写进库之后，页面应该收到一次 refresh ----
  const events = []
  stream = await fetch(base + '/dsh-pmem/api/starmap/events', {
    headers: { accept: 'text/event-stream' },
  })
  assert.equal(stream.status, 200)
  assert.ok(String(stream.headers.get('content-type')).includes('text/event-stream'))
  const decoder = new TextDecoder()
  const reading = (async () => {
    for await (const chunk of stream.body) {
      events.push(decoder.decode(chunk, { stream: true }))
      if (events.join('').includes('event: refresh')) break
    }
  })()

  await new Promise(done => setTimeout(done, 600))
  const late = run(['--json', 'add', 'SSE 测试写入的一条新记忆', '--group', '工作/PMem', '--grade', 'B'])
  assert.equal(late.status, 0, late.stderr)

  await Promise.race([reading, new Promise((_, fail) => setTimeout(() => fail(new Error('SSE 没有 refresh')), 15000))])
  const transcript = events.join('')
  assert.ok(transcript.includes('event: hello'), 'SSE 没有 hello')
  assert.ok(transcript.includes('event: refresh'), 'SSE 没有 refresh')

  // M9.7c：实时刷新必须走增量，否则每 1.5 秒重画一份 370 KB 的离线单文件，
// 而且旧星位置会被重算（画面会跳）。
const webSrc = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'web.js'), 'utf8')
assert.ok(webSrc.includes("'--incremental'"), '实时刷新没用增量模式')
assert.ok(!webSrc.includes("['star', '--no-png']"), '实时刷新还在做全量重建')

console.log('dsh-pmem web ok')
} finally {
  try { plugin.stop() } catch { /* ignore */ }
  try { await stream?.body?.cancel?.() } catch { /* 已经读完 */ }
  await new Promise(done => server.close(done))
  // Windows 上 sqlite / SSE 的句柄释放比 close 慢一拍：等一会儿再删，还要能重试。
  await new Promise(done => setTimeout(done, 300))
  try {
    rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 })
  } catch { /* 临时目录删不掉不影响测试结论 */ }
}
