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
const memoryId = JSON.parse(added.stdout).data.id
assert.ok(memoryId, 'CLI add 没回 id')
// 控制台详情页的 evidence 用 trace（精确子串）找原话：先留一行同样的话。
const traced = run(['--json', 'journal', 'append', '--text', '我们第一次一起改这个项目',
  '--role', 'user', '--session', 'console-trace'])
assert.equal(traced.status, 0, traced.stderr || traced.stdout)
// 实体/关系（P1）：`add` 会顺手抽实体和关系，给控制台实体页造真实数据。
const entityAdded = run(['--json', 'add', '小王在字节工作', '--grade', 'B', '--group', '工作/团队'])
assert.equal(entityAdded.status, 0, entityAdded.stderr || entityAdded.stdout)
const entitySeedId = JSON.parse(entityAdded.stdout).data.id
// 批量操作（P1）用的两条一次性记忆。
const batchAddedA = run(['--json', 'add', '批量测试一', '--grade', 'C'])
assert.equal(batchAddedA.status, 0, batchAddedA.stderr || batchAddedA.stdout)
const batchIdA = JSON.parse(batchAddedA.stdout).data.id
const batchAddedB = run(['--json', 'add', '批量测试二', '--grade', 'C'])
assert.equal(batchAddedB.status, 0, batchAddedB.stderr || batchAddedB.stdout)
const batchIdB = JSON.parse(batchAddedB.stdout).data.id
// 第 8 轮：单条/批量改元数据（kind/group/status）用。
const updateAddedA = run(['--json', 'add', '更新测试甲', '--grade', 'C'])
assert.equal(updateAddedA.status, 0, updateAddedA.stderr || updateAddedA.stdout)
const updateIdA = JSON.parse(updateAddedA.stdout).data.id
const updateAddedB = run(['--json', 'add', '更新测试乙', '--grade', 'C'])
assert.equal(updateAddedB.status, 0, updateAddedB.stderr || updateAddedB.stdout)
const updateIdB = JSON.parse(updateAddedB.stdout).data.id
// 大睡（宿主模式）要一天流水才会算出计划：给 2026-05-03 写一行原话。
const appended = run(['--json', 'journal', 'append', '--text', '今晚要加班到十点', '--role', 'user',
  '--ts', '2026-05-03T10:00:00Z', '--session', 's1'])
assert.equal(appended.status, 0, appended.stderr || appended.stdout)

mkdirSync(join(dir, 'data'), { recursive: true })
const star = spawnSync(py, ['-m', 'pmem.gate.cli', '--db', db, 'star', '--no-png', '--out', join(dir, 'data', 'starmap')], {
  env,
  encoding: 'utf8',
})
assert.equal(star.status, 0, star.stderr || star.stdout)

const routes = []
// 大睡（宿主模式）会通过这些 mock 验证：确实新建了会话、确实把鲸眠提示词投递进去。
const hostSessions = []
const hostPrompts = []
const sessionController = {
  async create(payload) {
    hostSessions.push(payload)
    return { sessionId: payload.sessionId }
  },
  async prompt(payload) {
    hostPrompts.push(payload)
  },
}
const ctx = {
  systemPrompt: { context: () => () => {} },
  tools: { register: () => () => {} },
  commands: { register: () => () => {} },
  on: () => () => {},
  get: name => {
    if (name === 'connection') return { requestRejection: () => undefined }
    if (name === 'sessionController') return sessionController
    return undefined
  },
  sessionController,
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

  // 大睡一场：只接受 POST；模板模式 + 演练要回 ok 和汇总字段（空库时 days=0 也算成功）。
  const bigGet = await get('/dsh-pmem/api/sleep/big')
  assert.equal(bigGet.status, 405, '大睡一场只接受 POST')
  const bigDry = await post('/dsh-pmem/api/sleep/big', { template: true, dry_run: true, limit: 1 })
  assert.equal(bigDry.status, 200, '大睡演练失败')
  assert.equal(bigDry.body.ok, true, JSON.stringify(bigDry.body))
  assert.equal(typeof bigDry.body.data?.days, 'number', '大睡没回 days')
  assert.equal(bigDry.body.data.dry_run, true, '演练没有标记 dry_run')
  const bigReal = await post('/dsh-pmem/api/sleep/big', { template: true, limit: 1 })
  assert.equal(bigReal.status, 200, '大睡失败')
  assert.equal(bigReal.body.ok, true, JSON.stringify(bigReal.body))
  assert.equal(typeof bigReal.body.data?.elapsed_s, 'number', '大睡没回 elapsed_s')
  assert.equal(typeof bigReal.body.data?.mode, 'string', '大睡没回 mode')

  // 大睡（默认）：由 DSH 来——算好日子、新建会话、把鲸眠提示词投进去。
  const hostGet = await get('/dsh-pmem/api/sleep/host-big')
  assert.equal(hostGet.status, 405, '大睡（DSH）只接受 POST')
  const hostBig = await post('/dsh-pmem/api/sleep/host-big', {})
  assert.equal(hostBig.status, 200, '大睡（DSH）失败')
  assert.equal(hostBig.body.ok, true, JSON.stringify(hostBig.body))
  assert.ok(hostBig.body.count >= 1, `没算出要睡的日子：${JSON.stringify(hostBig.body)}`)
  assert.ok(hostBig.body.days.includes('2026-05-03'), '计划里没有 2026-05-03')
  assert.ok(hostSessions.length >= 1, '没有新建会话')
  assert.equal(hostSessions.length, hostBig.body.sessions.length, '会话数与返回不一致')
  assert.match(String(hostPrompts[0]?.content?.[0]?.text || ''), /鲸眠/, '投递的提示词不像鲸眠')
  assert.match(String(hostPrompts[0]?.content?.[0]?.text || ''), /2026-05-03/, '提示词里没有日期')

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

  // ---- M22 控制台 API：只读聚合 + 三个管理动作（没有 chats / SSE） ----
  const consoleBootstrap = await get('/dsh-pmem/api/console/bootstrap')
  assert.equal(consoleBootstrap.status, 200, JSON.stringify(consoleBootstrap.body))
  assert.equal(consoleBootstrap.body.ok, true)
  assert.equal(typeof consoleBootstrap.body.version, 'string')
  assert.ok(consoleBootstrap.body.status?.db, 'bootstrap 缺 status.db')
  assert.match(consoleBootstrap.body.wake.text, /PMEM_WAKE|PMEM_SOUL/, 'bootstrap 醒来卡为空')
  assert.ok(Array.isArray(consoleBootstrap.body.profile.traits), 'profile.traits 不是数组')
  assert.ok(Array.isArray(consoleBootstrap.body.profile.open_goals), 'profile.open_goals 不是数组')
  assert.ok(Array.isArray(consoleBootstrap.body.recent), 'recent 不是数组')
  assert.ok(consoleBootstrap.body.recent.length >= 1, 'recent 为空')
  for (const key of ['id', 'text', 'score', 'day', 'kind', 'grade', 'status',
    'provenance', 'confidence', 'importance', 'groups', 'why', 'truncated']) {
    assert.ok(key in consoleBootstrap.body.recent[0], `recent 记忆缺 ${key}`)
  }
  assert.equal(typeof consoleBootstrap.body.counts.memory_total, 'number', 'counts.memory_total 不是数字')
  assert.equal(typeof consoleBootstrap.body.counts.today_recalls, 'number', 'counts.today_recalls 不是数字')
  assert.equal(typeof consoleBootstrap.body.counts.week_new, 'number', 'counts.week_new 不是数字')
  assert.ok(consoleBootstrap.body.counts.week_new >= 1, '今天刚写的记忆没算进 week_new')
  assert.equal(typeof consoleBootstrap.body.counts.pinned, 'number', 'counts.pinned 不是数字')
  assert.equal(consoleBootstrap.body.starmap.ready, true, 'bootstrap 星图未就绪')
  assert.equal(typeof consoleBootstrap.body.sleep.runs, 'number', 'bootstrap sleep.runs 不是数字')

  const consoleSearch = await get('/dsh-pmem/api/console/memories?q=' + encodeURIComponent('项目'))
  assert.equal(consoleSearch.status, 200, JSON.stringify(consoleSearch.body))
  assert.equal(consoleSearch.body.ok, true)
  assert.equal(consoleSearch.body.mode, 'auto')
  assert.ok(Array.isArray(consoleSearch.body.items), 'console memories items 不是数组')
  assert.ok(consoleSearch.body.items.some(item => item.id === memoryId), '控制台搜索没找到刚写的记忆')
  for (const key of ['id', 'text', 'score', 'day', 'kind', 'grade', 'status',
    'provenance', 'confidence', 'importance', 'groups', 'source_kind', 'why', 'truncated']) {
    assert.ok(key in consoleSearch.body.items[0], `搜索记忆缺 ${key}`)
  }

  const consoleClamped = await get('/dsh-pmem/api/console/memories?q=' + encodeURIComponent('项目') + '&k=999')
  assert.equal(consoleClamped.body.k, 50, 'k 没夹到 50')
  assert.ok(consoleClamped.body.items.length <= 50)

  const consoleRecent = await get('/dsh-pmem/api/console/memories?mode=recent&k=5')
  assert.equal(consoleRecent.status, 200)
  assert.equal(consoleRecent.body.effective, 'recent')
  assert.ok(consoleRecent.body.items.length >= 1, 'recent 模式为空')

  const consoleEmptyQuery = await get('/dsh-pmem/api/console/memories')
  assert.equal(consoleEmptyQuery.status, 200)
  assert.equal(consoleEmptyQuery.body.effective, 'recent')
  assert.ok(consoleEmptyQuery.body.items.length >= 1, '空查询没有回退到最近')

  const consoleBadMode = await get('/dsh-pmem/api/console/memories?q=x&mode=semantic')
  assert.equal(consoleBadMode.status, 400, '非法 mode 没被拦')

  // ---- 真分页 / 排序 / 日期过滤 ----
  const pageOne = await get('/dsh-pmem/api/console/memories?limit=2&offset=0')
  assert.equal(pageOne.status, 200, JSON.stringify(pageOne.body))
  assert.equal(pageOne.body.offset, 0)
  assert.equal(pageOne.body.limit, 2)
  assert.equal(pageOne.body.items.length, 2)
  assert.equal(typeof pageOne.body.total, 'number', '分页缺 total')
  assert.ok(pageOne.body.total >= 2)
  assert.equal(pageOne.body.hasMore, pageOne.body.total > 2)
  const pageTwo = await get('/dsh-pmem/api/console/memories?limit=2&offset=2')
  assert.equal(pageTwo.status, 200, JSON.stringify(pageTwo.body))
  assert.equal(pageTwo.body.offset, 2)
  assert.equal(pageTwo.body.items.length, 2)
  const pageOneIds = new Set(pageOne.body.items.map(item => item.id))
  assert.ok(pageTwo.body.items.every(item => !pageOneIds.has(item.id)), '第二页出现了第一页的记忆')

  const byImportance = await get('/dsh-pmem/api/console/memories?limit=5&sort=importance')
  assert.equal(byImportance.status, 200, JSON.stringify(byImportance.body))
  assert.equal(byImportance.body.sort, 'importance')
  const importanceValues = byImportance.body.items.map(item => Number(item.importance ?? 0))
  for (let index = 1; index < importanceValues.length; index += 1) {
    assert.ok(importanceValues[index] <= importanceValues[index - 1], 'importance 排序没生效')
  }
  const byUses = await get('/dsh-pmem/api/console/memories?limit=5&sort=uses')
  assert.equal(byUses.status, 200)
  const useValues = byUses.body.items.map(item => Number(item.use_count ?? 0))
  for (let index = 1; index < useValues.length; index += 1) {
    assert.ok(useValues[index] <= useValues[index - 1], 'uses 排序没生效')
  }

  // 日期过滤：同一天 since=until 只剩那天的记忆
  const sampleDay = pageOne.body.items[0].day
  assert.match(String(sampleDay), /^\d{4}-\d{2}-\d{2}$/)
  const datedPage = await get('/dsh-pmem/api/console/memories?limit=20&since='
    + sampleDay + '&until=' + sampleDay)
  assert.equal(datedPage.status, 200, JSON.stringify(datedPage.body))
  assert.ok(datedPage.body.items.length >= 1)
  assert.ok(datedPage.body.items.every(item => item.day === sampleDay), '日期过滤没生效')
  const futurePage = await get('/dsh-pmem/api/console/memories?since=2099-01-01')
  assert.equal(futurePage.status, 200)
  assert.deepEqual(futurePage.body.items, [])
  assert.equal(futurePage.body.total, 0)

  // 越界参数夹取 / 非法参数 400
  const clampedPage = await get('/dsh-pmem/api/console/memories?offset=99999&limit=999')
  assert.equal(clampedPage.status, 200)
  assert.equal(clampedPage.body.offset, 1000)
  assert.equal(clampedPage.body.limit, 50)
  const badSort = await get('/dsh-pmem/api/console/memories?sort=recent')
  assert.equal(badSort.status, 400, '非法 sort 没被拦')
  const badSince = await get('/dsh-pmem/api/console/memories?since=2026-13-01')
  assert.equal(badSince.status, 400, '非法 since 没被拦')
  const invertedRange = await get('/dsh-pmem/api/console/memories?since=2026-11-30&until=2026-11-01')
  assert.equal(invertedRange.status, 400, 'since>until 没被拦')

  // 有 q 时：第二页不与第一页重复；日期过滤同样生效
  const qPageOne = await get('/dsh-pmem/api/console/memories?q=' + encodeURIComponent('测试')
    + '&limit=2&offset=0')
  assert.equal(qPageOne.status, 200, JSON.stringify(qPageOne.body))
  assert.equal(qPageOne.body.sort, 'relevance')
  assert.ok(qPageOne.body.items.length >= 1, '搜索分页第一页为空')
  const qPageTwo = await get('/dsh-pmem/api/console/memories?q=' + encodeURIComponent('测试')
    + '&limit=2&offset=2')
  assert.equal(qPageTwo.status, 200)
  const qFirstIds = new Set(qPageOne.body.items.map(item => item.id))
  assert.ok(qPageTwo.body.items.every(item => !qFirstIds.has(item.id)), '搜索第二页出现了第一页的记忆')
  const qFuture = await get('/dsh-pmem/api/console/memories?q=' + encodeURIComponent('测试')
    + '&since=2099-01-01')
  assert.equal(qFuture.status, 200)
  assert.deepEqual(qFuture.body.items, [])

  const consoleDetail = await get('/dsh-pmem/api/console/memory?id=' + encodeURIComponent(memoryId))
  assert.equal(consoleDetail.status, 200, JSON.stringify(consoleDetail.body))
  assert.equal(consoleDetail.body.item.id, memoryId)
  assert.equal(consoleDetail.body.item.text, '我们第一次一起改这个项目')
  assert.ok(consoleDetail.body.item.groups.includes('我们/经历'), '详情缺分组')
  assert.equal(consoleDetail.body.item.grade, 'A')
  assert.ok(Array.isArray(consoleDetail.body.history) && consoleDetail.body.history.length >= 1, '详情缺 history')
  assert.equal(typeof consoleDetail.body.history[0].at, 'string', 'history 缺 at')
  assert.equal(typeof consoleDetail.body.history[0].action, 'string', 'history 缺 action')
  assert.ok(Array.isArray(consoleDetail.body.evidence), '详情缺 evidence 数组')
  assert.ok(consoleDetail.body.evidence.length >= 1, 'trace 没找到原话 evidence')
  assert.equal(consoleDetail.body.evidence[0].session, 'console-trace')
  assert.ok(Array.isArray(consoleDetail.body.conflicts), '详情缺 conflicts 数组')

  const consoleNoId = await get('/dsh-pmem/api/console/memory')
  assert.equal(consoleNoId.status, 400, '缺 id 没被拦')
  const consoleMissing = await get('/dsh-pmem/api/console/memory?id=ZZZZNOTFOUND')
  assert.equal(consoleMissing.status, 404, '不存在的记忆没回 404')
  assert.equal(consoleMissing.body.code, 'E_NOT_FOUND')

  const consoleProfile = await get('/dsh-pmem/api/console/profile?budget=500')
  assert.equal(consoleProfile.status, 200, JSON.stringify(consoleProfile.body))
  assert.equal(consoleProfile.body.budget, 500)
  assert.ok(Array.isArray(consoleProfile.body.profile.traits), 'profile traits 不是数组')

  const consoleGroups = await get('/dsh-pmem/api/console/groups')
  assert.equal(consoleGroups.status, 200, JSON.stringify(consoleGroups.body))
  assert.ok(Array.isArray(consoleGroups.body.tree) && consoleGroups.body.tree.length >= 1, '分组树为空')
  assert.equal(typeof consoleGroups.body.stats.groups, 'number', '分组统计缺 groups')
  const consoleTargets = await get('/dsh-pmem/api/console/groups?path=' + encodeURIComponent('我们/经历'))
  assert.equal(consoleTargets.status, 200)
  assert.equal(consoleTargets.body.path, '我们/经历')
  assert.ok(consoleTargets.body.items.some(item => item.id === memoryId), '分组 targets 里没有这条记忆')

  const consoleConflicts = await get('/dsh-pmem/api/console/conflicts')
  assert.equal(consoleConflicts.status, 200)
  assert.ok(Array.isArray(consoleConflicts.body.items), 'conflicts items 不是数组')

  const consoleStats = await get('/dsh-pmem/api/console/stats')
  assert.equal(consoleStats.status, 200, JSON.stringify(consoleStats.body))
  assert.ok(consoleStats.body.stats.memory_total >= 1, 'stats.memory_total 不对')
  assert.ok(consoleStats.body.groups && typeof consoleStats.body.groups.groups === 'number', 'stats 缺分组统计')
  assert.equal(typeof consoleStats.body.conflicts.open, 'number', 'stats 缺冲突计数')

  const consoleDoctor = await get('/dsh-pmem/api/console/doctor')
  assert.equal(consoleDoctor.status, 200, JSON.stringify(consoleDoctor.body))
  assert.equal(consoleDoctor.body.doctor.integrity, true, 'doctor 完整性失败')
  assert.ok(consoleDoctor.body.stats.db_size_bytes > 0, 'doctor 缺库体积')

  // 写路由：只接受 POST；同源 + JSON 守卫沿用既有那套
  const feedbackGet = await get('/dsh-pmem/api/console/memory/feedback')
  assert.equal(feedbackGet.status, 405, 'feedback 只接受 POST')
  const crossConsole = await post(
    '/dsh-pmem/api/console/memory/feedback',
    { id: memoryId, signal: 'useful' },
    { origin: 'https://evil.example' },
  )
  assert.equal(crossConsole.status, 403, '控制台跨站写没被拦')
  const plainConsole = await fetch(base + '/dsh-pmem/api/console/memory/feedback', {
    method: 'POST',
    headers: { 'content-type': 'text/plain' },
    body: '{}',
  })
  assert.equal(plainConsole.status, 415, '控制台非 JSON 写没被拦')
  const feedback = await post('/dsh-pmem/api/console/memory/feedback',
    { id: memoryId, signal: 'useful', note: '验收一下' })
  assert.equal(feedback.status, 200, JSON.stringify(feedback.body))
  assert.equal(feedback.body.ok, true)
  assert.equal(feedback.body.signal, 'useful')
  const badSignal = await post('/dsh-pmem/api/console/memory/feedback',
    { id: memoryId, signal: 'like' })
  assert.equal(badSignal.status, 400, '非法 signal 没被拦')

  const update = await post('/dsh-pmem/api/console/memory/update',
    { id: memoryId, importance: 0.8, pinned: true, grade: 'A' })
  assert.equal(update.status, 200, JSON.stringify(update.body))
  assert.equal(update.body.ok, true)
  assert.ok(update.body.item, 'update 没回 item')
  assert.equal(update.body.item.pinned, true, 'update 没写进 pinned')
  assert.equal(update.body.item.importance, 0.8, 'update 没写进 importance')
  assert.equal(update.body.updated, true)
  assert.deepEqual(update.body.failed, [])

  // ---- 第 8 轮：单条 update 支持 kind / group / status ----
  const updateMeta = await post('/dsh-pmem/api/console/memory/update', {
    id: updateIdA,
    kind: 'episodic',
    status: 'dormant',
    group: '工作/验收组',
    grade: 'B',
    sensitivity: 'personal',
    importance: 0.6,
    reason: '验收改元数据',
  })
  assert.equal(updateMeta.status, 200, JSON.stringify(updateMeta.body))
  assert.equal(updateMeta.body.ok, true)
  assert.equal(updateMeta.body.applied.kind, 'episodic')
  assert.equal(updateMeta.body.applied.status, 'dormant')
  assert.equal(updateMeta.body.applied.group, '工作/验收组')
  assert.equal(updateMeta.body.applied.grade, 'B')
  assert.deepEqual(updateMeta.body.failed, [])
  const updateMetaDetail = await get('/dsh-pmem/api/console/memory?id=' + encodeURIComponent(updateIdA))
  assert.equal(updateMetaDetail.status, 200, JSON.stringify(updateMetaDetail.body))
  assert.equal(updateMetaDetail.body.item.kind, 'episodic', 'kind 没落库')
  assert.equal(updateMetaDetail.body.item.status, 'dormant', 'status 没落库')
  assert.ok(updateMetaDetail.body.item.groups.includes('工作/验收组'), 'group 没落库')
  assert.equal(updateMetaDetail.body.item.grade, 'B', 'grade 没落库')
  assert.equal(updateMetaDetail.body.item.sensitivity, 'personal')
  assert.equal(updateMetaDetail.body.item.importance, 0.6)

  const badKind = await post('/dsh-pmem/api/console/memory/update', { id: updateIdA, kind: 'book' })
  assert.equal(badKind.status, 400, '非法 kind 没被拦')
  assert.equal(badKind.body.field, 'kind')
  const badStatus = await post('/dsh-pmem/api/console/memory/update', { id: updateIdA, status: 'deleted' })
  assert.equal(badStatus.status, 400, '非法 status 没被拦')
  const missingUpdate = await post('/dsh-pmem/api/console/memory/update', { id: 'ZZZZNOTFOUND', kind: 'semantic' })
  assert.equal(missingUpdate.status, 404, '更新不存在的记忆没回 404')
  assert.deepEqual(missingUpdate.body.applied, {})
  assert.equal(missingUpdate.body.failed.length, 1)
  assert.equal(missingUpdate.body.failed[0].field, 'kind')

  // ---- 第 8 轮：batch update ----
  const batchUpdate = await post('/dsh-pmem/api/console/memory/batch', {
    ids: [updateIdA, updateIdB],
    op: 'update',
    patch: { kind: 'semantic', status: 'active', grade: 'A' },
  })
  assert.equal(batchUpdate.status, 200, JSON.stringify(batchUpdate.body))
  assert.equal(batchUpdate.body.ok, true)
  assert.equal(batchUpdate.body.okCount, 2)
  assert.equal(batchUpdate.body.failCount, 0)
  assert.deepEqual(batchUpdate.body.applied_fields, ['kind', 'status', 'grade'])
  assert.deepEqual(batchUpdate.body.results[0].applied, { kind: 'semantic', status: 'active', grade: 'A' })
  const batchUpdatedDetail = await get('/dsh-pmem/api/console/memory?id=' + encodeURIComponent(updateIdB))
  assert.equal(batchUpdatedDetail.status, 200)
  assert.equal(batchUpdatedDetail.body.item.kind, 'semantic', 'batch update 的 kind 没落库')
  assert.equal(batchUpdatedDetail.body.item.status, 'active', 'batch update 的 status 没落库')
  assert.equal(batchUpdatedDetail.body.item.grade, 'A', 'batch update 的 grade 没落库')
  const badBatchPatch = await post('/dsh-pmem/api/console/memory/batch', {
    ids: [updateIdA], op: 'update', patch: { pinned: true },
  })
  assert.equal(badBatchPatch.status, 400, 'batch update 白名单外的字段没被拦')
  const emptyBatchPatch = await post('/dsh-pmem/api/console/memory/batch', {
    ids: [updateIdA], op: 'update', patch: {},
  })
  assert.equal(emptyBatchPatch.status, 400, 'batch update 空 patch 没被拦')

  const removeSoft = await post('/dsh-pmem/api/console/memory/delete', { id: memoryId, hard: false })
  assert.equal(removeSoft.status, 200, JSON.stringify(removeSoft.body))
  assert.equal(removeSoft.body.ok, true)
  assert.equal(removeSoft.body.hard, false)
  const afterDelete = await get('/dsh-pmem/api/console/memory?id=' + encodeURIComponent(memoryId))
  assert.equal(afterDelete.status, 404, '软删后还能读到详情')
  const removeHard = await post('/dsh-pmem/api/console/memory/delete', { id: memoryId, hard: true })
  assert.equal(removeHard.status, 200, JSON.stringify(removeHard.body))
  assert.equal(removeHard.body.hard, true)
  assert.ok(removeHard.body.deleted >= 1, '硬删没删到行')

  // ---- P1：实体 / 关系（真实 CLI + 真实抽取） ----
  const entities = await get('/dsh-pmem/api/console/entities')
  assert.equal(entities.status, 200, JSON.stringify(entities.body))
  assert.ok(Array.isArray(entities.body.items) && entities.body.items.length >= 1, '实体列表为空')
  assert.equal(typeof entities.body.total, 'number', '实体列表缺 total')
  for (const key of ['id', 'name', 'type', 'mention_count', 'mentions', 'grade', 'groups']) {
    assert.ok(key in entities.body.items[0], `实体缺字段 ${key}`)
  }
  const xiaoWang = entities.body.items.find(item => item.name === '小王')
  const ziJie = entities.body.items.find(item => item.name === '字节')
  assert.ok(xiaoWang, `实体列表里没有小王：${JSON.stringify(entities.body.items.map(item => item.name))}`)
  assert.ok(ziJie, '实体列表里没有字节')

  const entityDetail = await get('/dsh-pmem/api/console/entity?id=' + encodeURIComponent(xiaoWang.id))
  assert.equal(entityDetail.status, 200, JSON.stringify(entityDetail.body))
  assert.equal(entityDetail.body.entity.id, xiaoWang.id)
  assert.equal(entityDetail.body.entity.name, '小王')
  assert.ok(Array.isArray(entityDetail.body.aliases), '实体详情缺 aliases')
  assert.ok(Array.isArray(entityDetail.body.mentions) && entityDetail.body.mentions.length >= 1, '实体详情缺提及记忆')
  assert.ok(entityDetail.body.mentions.some(item => item.id === entitySeedId), '提及记忆里没有小王那条')
  assert.ok(Array.isArray(entityDetail.body.relations) && entityDetail.body.relations.length >= 1, '实体详情缺 1 跳关系')
  assert.ok(entityDetail.body.relations.some(item => item.predicate === 'works_at'), '关系里没有 works_at')
  assert.equal(typeof entityDetail.body.counts.mentions, 'number', '实体详情缺 counts')

  const entityByName = await get('/dsh-pmem/api/console/entity?name=' + encodeURIComponent('小王'))
  assert.equal(entityByName.status, 200, JSON.stringify(entityByName.body))
  assert.equal(entityByName.body.entity.id, xiaoWang.id)

  const entityMissing = await get('/dsh-pmem/api/console/entity?name=' + encodeURIComponent('没有这个实体XYZ'))
  assert.equal(entityMissing.status, 404, '不存在的实体没回 404')
  const entityBadId = await get('/dsh-pmem/api/console/entity?id=' + encodeURIComponent('-rf'))
  assert.equal(entityBadId.status, 400, '非法实体 id 没被拦')

  const netQuery = await post('/dsh-pmem/api/console/net/query', { src: xiaoWang.id, depth: 1 })
  assert.equal(netQuery.status, 200, JSON.stringify(netQuery.body))
  assert.ok(netQuery.body.items.length >= 1, '1 跳查询为空')
  assert.equal(netQuery.body.items[0].predicate, 'works_at')
  const netQueryDst = await post('/dsh-pmem/api/console/net/query', { dst: ziJie.id, depth: 1 })
  assert.equal(netQueryDst.status, 200, JSON.stringify(netQueryDst.body))
  assert.ok(netQueryDst.body.items.length >= 1, '按 dst 反查为空')
  const netQueryBad = await post('/dsh-pmem/api/console/net/query', {})
  assert.equal(netQueryBad.status, 400, 'net query 缺 src/dst 没被拦')

  const mergeSame = await post('/dsh-pmem/api/console/net/merge',
    { left: xiaoWang.id, right: xiaoWang.id, dry_run: true })
  assert.equal(mergeSame.status, 400, '合并到自身没被拦')
  const mergePreview = await post('/dsh-pmem/api/console/net/merge',
    { left: xiaoWang.id, right: ziJie.id, dry_run: true })
  assert.equal(mergePreview.status, 200, JSON.stringify(mergePreview.body))
  assert.equal(mergePreview.body.dry_run, true)
  assert.equal(mergePreview.body.keep.id, xiaoWang.id)
  assert.equal(mergePreview.body.drop.id, ziJie.id)
  assert.ok(mergePreview.body.drop_counts && mergePreview.body.drop_counts.mentions >= 1, '合并预览缺计数')
  const merged = await post('/dsh-pmem/api/console/net/merge', { left: xiaoWang.id, right: ziJie.id })
  assert.equal(merged.status, 200, JSON.stringify(merged.body))
  assert.equal(merged.body.dry_run, false)
  assert.equal(merged.body.keep, xiaoWang.id)
  assert.equal(merged.body.merged, ziJie.id)
  assert.equal(typeof merged.body.mentions, 'number', '合并结果缺 mentions')
  const mergedGone = await get('/dsh-pmem/api/console/entity?id=' + encodeURIComponent(ziJie.id))
  assert.equal(mergedGone.status, 404, '被合并的实体还能按 id 查到')

  // ---- P1：备份 / 下载 ----
  const backupsBefore = await get('/dsh-pmem/api/console/backup')
  assert.equal(backupsBefore.status, 200, JSON.stringify(backupsBefore.body))
  assert.ok(Array.isArray(backupsBefore.body.items), '备份列表不是数组')
  // 前面跑过的 sleep 也会在 db 目录/backups 里留快照文件，所以只比较增量。
  const backupCountBefore = backupsBefore.body.items.length
  assert.equal(typeof backupsBefore.body.counts.total_bytes, 'number', '备份列表缺总大小')

  const backupCreated = await post('/dsh-pmem/api/console/backup', { note: '验收备份' })
  assert.equal(backupCreated.status, 200, JSON.stringify(backupCreated.body))
  assert.ok(backupCreated.body.file && backupCreated.body.file.name, '生成备份没回文件名')
  assert.match(backupCreated.body.file.name, /^pmem-backup-.*\.db$/, '备份文件名不对')
  assert.ok(backupCreated.body.file.size_bytes > 0, '备份文件大小为 0')
  assert.equal(backupCreated.body.file.verified, true, '备份没有校验通过')
  assert.equal(backupCreated.body.note, '验收备份')

  const backupsAfter = await get('/dsh-pmem/api/console/backup')
  assert.equal(backupsAfter.status, 200, JSON.stringify(backupsAfter.body))
  assert.equal(backupsAfter.body.items.length, backupCountBefore + 1, '备份列表增量不对')
  assert.ok(backupsAfter.body.items.some(item => item.name === backupCreated.body.file.name), '备份列表缺新文件')
  assert.equal(typeof backupsAfter.body.counts.total_bytes, 'number', '备份列表缺总大小')

  const download = await fetch(base + '/dsh-pmem/api/console/backup/file?name='
    + encodeURIComponent(backupCreated.body.file.name))
  assert.equal(download.status, 200, '备份下载失败')
  assert.equal(download.headers.get('content-type'), 'application/octet-stream')
  assert.match(String(download.headers.get('content-disposition')), /^attachment; filename="/)
  const downloaded = Buffer.from(await download.arrayBuffer())
  assert.equal(downloaded.length, backupCreated.body.file.size_bytes, '下载大小和返回的 size_bytes 不一致')
  assert.ok(downloaded.length > 0)
  assert.equal(downloaded.subarray(0, 15).toString('utf8'), 'SQLite format 3')

  const traversal = await get('/dsh-pmem/api/console/backup/file?name='
    + encodeURIComponent('../pmem.db'))
  assert.equal(traversal.status, 400, '备份下载路径穿越没被拦')
  const outside = await get('/dsh-pmem/api/console/backup/file?name=pmem.db')
  assert.equal(outside.status, 404, '备份目录外的同名文件不该给')

  // ---- P1：向量重建（dry_run 只看状态） ----
  const vectorDry = await post('/dsh-pmem/api/console/vector/rebuild', { dry_run: true })
  assert.equal(vectorDry.status, 200, JSON.stringify(vectorDry.body))
  assert.equal(vectorDry.body.dry_run, true)
  assert.equal(typeof vectorDry.body.status.vectors, 'number', 'vector status 缺 vectors')

  // ---- P1：批量操作（真实逐条执行） ----
  const batchFeedback = await post('/dsh-pmem/api/console/memory/batch',
    { ids: [batchIdA, batchIdB], op: 'feedback', signal: 'useful', note: '验收批量' })
  assert.equal(batchFeedback.status, 200, JSON.stringify(batchFeedback.body))
  assert.equal(batchFeedback.body.ok, true)
  assert.equal(batchFeedback.body.okCount, 2)
  assert.equal(batchFeedback.body.failCount, 0)
  const batchRetract = await post('/dsh-pmem/api/console/memory/batch',
    { ids: [batchIdA], op: 'retract', note: '验收撤回' })
  assert.equal(batchRetract.status, 200, JSON.stringify(batchRetract.body))
  assert.equal(batchRetract.body.okCount, 1)
  const retracted = await get('/dsh-pmem/api/console/memory?id=' + encodeURIComponent(batchIdA))
  assert.equal(retracted.status, 200, '撤回后仍应能读到（archived）')
  assert.equal(retracted.body.item.status, 'archived', 'retract 没把状态降到 archived')
  const batchDelete = await post('/dsh-pmem/api/console/memory/batch',
    { ids: [batchIdB], op: 'delete' })
  assert.equal(batchDelete.status, 200, JSON.stringify(batchDelete.body))
  assert.equal(batchDelete.body.hard, false, '批量删除默认应该软删')
  const batchDeleted = await get('/dsh-pmem/api/console/memory?id=' + encodeURIComponent(batchIdB))
  assert.equal(batchDeleted.status, 404, '批量软删后还能读到')
  const batchEmpty = await post('/dsh-pmem/api/console/memory/batch', { ids: [], op: 'delete' })
  assert.equal(batchEmpty.status, 400, '空 ids 没被拦')

  // ---- P1：运维白名单 ----
  const actionDoctor = await post('/dsh-pmem/api/console/action', { name: 'doctor' })
  assert.equal(actionDoctor.status, 200, JSON.stringify(actionDoctor.body))
  assert.equal(actionDoctor.body.result.integrity, true)
  const actionVacuumDry = await post('/dsh-pmem/api/console/action', { name: 'vacuum', dry_run: true })
  assert.equal(actionVacuumDry.status, 200, JSON.stringify(actionVacuumDry.body))
  assert.equal(actionVacuumDry.body.dry_run, true)
  assert.ok(actionVacuumDry.body.planned.includes('vacuum'))
  const actionBad = await post('/dsh-pmem/api/console/action', { name: 'rm -rf /' })
  assert.equal(actionBad.status, 400, '运维白名单外的 name 没被拦')
  const actionGet = await get('/dsh-pmem/api/console/action')
  assert.equal(actionGet.status, 405, 'action 只接受 POST')

  // 新写路由同样受同源 / JSON 守卫
  const batchCross = await post('/dsh-pmem/api/console/memory/batch',
    { ids: [batchIdA], op: 'feedback', signal: 'useful' }, { origin: 'https://evil.example' })
  assert.equal(batchCross.status, 403, '新写路由跨站没被拦')
  const actionPlain = await fetch(base + '/dsh-pmem/api/console/action', {
    method: 'POST',
    headers: { 'content-type': 'text/plain' },
    body: '{}',
  })
  assert.equal(actionPlain.status, 415, '新写路由非 JSON 没被拦')

  // 需求纠偏后不该再有聊天/SSE 路由
  const chats = await get('/dsh-pmem/api/console/chats')
  assert.equal(chats.status, 404, '控制台不该再有 chats 路由')

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
