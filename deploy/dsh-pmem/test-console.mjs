/**
 * M22 控制台后端单测：不启 HTTP、不碰真库、不 spawn Python。
 *
 * 覆盖：
 *   · 路由表（21 条 exact，全部在 /dsh-pmem/api/console 下，没有 chats/SSE 残留）
 *   · bootstrap 聚合（status / wake / profile / recent / counts / starmap / sleep）
 *   · memories 搜索与过滤、k 夹取、recent 模式与降级
 *   · memory 详情（item / history / evidence / conflicts）+ 404
 *   · feedback / update / delete 的 POST-only、参数校验、CLI 参数
 *   · profile / groups / conflicts / stats / doctor
 *   · P1：entities / entity / net query / net merge / backup / backup file /
 *     vector rebuild / memory batch / action 白名单
 *   · body 上限、坏 JSON、CLI 非 0 退出的错误映射、子调用失败的降级
 *
 * 跑：node deploy/dsh-pmem/test-console.mjs
 */

import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import {
  ACTION_NAMES,
  BATCH_MAX_IDS,
  BATCH_UPDATE_FIELDS,
  CONSOLE_API,
  FEEDBACK_SIGNALS,
  MAX_BODY,
  MEMORY_KINDS,
  MEMORY_SORTS,
  MEMORY_STATUSES,
  createConsole,
  intIn,
  localDayKey,
  normalizeEntity,
  normalizeMemory,
  openMemoryReader,
  parseList,
  resolveBackupDir,
  safeBackupName,
  safeValue,
} from './console.js'

// ---------------------------------------------------------------------------
// 假 HTTP：req/res 只要够 readJsonBody 和 json() 用
// ---------------------------------------------------------------------------

function createRes() {
  return {
    statusCode: 0,
    headers: {},
    body: null,
    bytes: null,
    chunks: [],
    finished: false,
    setHeader(name, value) {
      this.headers[String(name).toLowerCase()] = value
    },
    end(chunk) {
      this.finished = true
      if (chunk === undefined || chunk === null) return
      this.chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)))
      this.finishBody()
    },
    write(chunk) {
      this.chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)))
      this.finishBody()
    },
    finishBody() {
      this.bytes = Buffer.concat(this.chunks)
      this.body = this.bytes.toString('utf8')
    },
    destroy() {
      this.destroyed = true
    },
  }
}

function createReq({ method = 'GET', url = '/', headers = {}, body = null } = {}) {
  const listeners = new Map()
  const req = {
    method,
    url,
    headers,
    destroyed: false,
    on(event, handler) {
      const list = listeners.get(event) ?? []
      list.push(handler)
      listeners.set(event, list)
      return req
    },
    destroy() {
      req.destroyed = true
    },
  }
  if (body !== null && body !== undefined) {
    queueMicrotask(() => {
      const chunk = Buffer.isBuffer(body) ? body : Buffer.from(String(body), 'utf8')
      for (const handler of listeners.get('data') ?? []) handler(chunk)
      if (!req.destroyed) for (const handler of listeners.get('end') ?? []) handler()
    })
  }
  return req
}

async function callRoute(routes, path, options = {}) {
  // /backup 同时有 GET（列目录）和 POST（生成）两个 handler；按方法挑。
  const candidates = routes.filter(item => item.path === path)
  assert.ok(candidates.length > 0, `没有注册路由 ${path}`)
  const isPost = (options.method ?? 'GET').toUpperCase() === 'POST'
  const route = candidates.length > 1 && isPost ? candidates[candidates.length - 1] : candidates[0]
  const res = createRes()
  await route.handler(
    createReq({
      method: options.method ?? 'GET',
      url: options.url ?? path,
      headers: options.headers ?? {},
      body: options.body ?? null,
    }),
    res,
  )
  let body = null
  try {
    body = res.body === null ? null : JSON.parse(res.body)
  } catch {
    body = res.body
  }
  return { status: res.statusCode, headers: res.headers, body, bytes: res.bytes }
}

const PATHS = {
  bootstrap: `${CONSOLE_API}/bootstrap`,
  memories: `${CONSOLE_API}/memories`,
  memory: `${CONSOLE_API}/memory`,
  feedback: `${CONSOLE_API}/memory/feedback`,
  update: `${CONSOLE_API}/memory/update`,
  remove: `${CONSOLE_API}/memory/delete`,
  profile: `${CONSOLE_API}/profile`,
  groups: `${CONSOLE_API}/groups`,
  conflicts: `${CONSOLE_API}/conflicts`,
  stats: `${CONSOLE_API}/stats`,
  doctor: `${CONSOLE_API}/doctor`,
  entities: `${CONSOLE_API}/entities`,
  entity: `${CONSOLE_API}/entity`,
  netQuery: `${CONSOLE_API}/net/query`,
  netMerge: `${CONSOLE_API}/net/merge`,
  backup: `${CONSOLE_API}/backup`,
  backupFile: `${CONSOLE_API}/backup/file`,
  vectorRebuild: `${CONSOLE_API}/vector/rebuild`,
  batch: `${CONSOLE_API}/memory/batch`,
  action: `${CONSOLE_API}/action`,
}

// ---------------------------------------------------------------------------
// 假 callPmem：按 args 前缀匹配，记录调用
// ---------------------------------------------------------------------------

function makePmem(rules = []) {
  const calls = []
  const callPmem = async (config, args, opts = {}) => {
    calls.push({ config, args: [...args], opts })
    for (const rule of rules) {
      const prefix = rule.args ?? []
      if (args.length < prefix.length) continue
      if (!prefix.every((value, index) => args[index] === value)) continue
      const reply = typeof rule.reply === 'function' ? rule.reply(args, opts) : rule.reply
      if (reply instanceof Error) throw reply
      return reply
    }
    return { ok: true, data: {} }
  }
  callPmem.calls = calls
  callPmem.argsOf = name => calls.filter(item => item.args[0] === name).map(item => item.args)
  return callPmem
}

function cliError(code, message) {
  return { ok: false, error: { code, message } }
}

function makeConsole(overrides = {}) {
  const callPmem = overrides.callPmem ?? makePmem(overrides.rules ?? [])
  const reader = overrides.reader === undefined
    ? {
      recent: () => [],
      counts: () => ({ total: 7, today_new: 2, uses: 9 }),
    }
    : overrides.reader
  const snapshot = overrides.snapshot === undefined
    ? async ({ card } = {}) => ({
      ok: true,
      version: 'test',
      runtime: { surface: true },
      db: { memory_total: 7, event_count: 42, recent: [] },
      wake: { chars: String(card ?? '').length },
      starmap: { ready: true, nodes: 3 },
    })
    : overrides.snapshot
  const wake = overrides.wake ?? (async () => '<<<PMEM_WAKE v1>>>\n我是谁：小鲸鲸')
  const stats = overrides.stats ?? (async () => ({ memory_total: 7, event_count: 42, recent: [] }))
  const starmap = overrides.starmap ?? (() => ({ ready: true, nodes: 3 }))
  const api = createConsole({
    config: {
      db: overrides.db ?? join(tmpdir(), 'pmem-console-unit', 'pmem.db'),
      budget: overrides.budget ?? 300,
      ...(overrides.config ?? {}),
    },
    callPmem,
    version: 'test',
    wake,
    stats,
    starmap,
    snapshot,
    reader,
    logger: { warn() {} },
  })
  return { api, callPmem, routes: api.routes }
}

// ---------------------------------------------------------------------------
// 0. 纯函数
// ---------------------------------------------------------------------------

{
  assert.equal(intIn('999', 20, 1, 50), 50)
  assert.equal(intIn('0', 20, 1, 50), 1)
  assert.equal(intIn('', 20, 1, 50), 20)
  assert.equal(intIn(null, 20, 1, 50), 20)
  assert.equal(intIn('abc', 20, 1, 50), 20)
  assert.equal(intIn('-5', 20, 1, 50), 1)

  assert.deepEqual(parseList('a,b, a'), ['a', 'b'])
  assert.deepEqual(parseList(['x, y', 'y']), ['x', 'y'])
  assert.deepEqual(parseList(null), [])

  // P1 参数守卫：不得为空、不得以 - 开头（argparse 会当选项）、不得有控制字符
  assert.equal(safeValue('小王'), '小王')
  assert.equal(safeValue('-rf'), null)
  assert.equal(safeValue('  '), null)
  assert.equal(safeValue('a' + String.fromCharCode(0) + 'b'), null)
  assert.equal(safeValue('x'.repeat(300), 200), null)
  assert.equal(safeValue('x'.repeat(300), 400).length, 300)

  // 备份文件名只认 basename + .db
  assert.equal(safeBackupName('pmem-backup-20261105.db'), 'pmem-backup-20261105.db')
  assert.equal(safeBackupName('../pmem.db'), null)
  assert.equal(safeBackupName('sub/pmem.db'), null)
  assert.equal(safeBackupName('pmem.db-wal'), null)
  assert.equal(safeBackupName('.hidden.db'), null)
  assert.equal(safeBackupName(''), null)
  assert.equal(safeBackupName('..'), null)

  // 备份目录：默认 = 数据库目录/backups；可覆盖；:memory: 没有目录
  assert.equal(resolveBackupDir({ db: join(tmpdir(), 'pmem-data', 'pmem.db') }), resolve(join(tmpdir(), 'pmem-data', 'backups')))
  assert.equal(resolveBackupDir({ db: ':memory:' }), null)
  assert.equal(resolveBackupDir({ db: '', backupDir: join(tmpdir(), 'custom-backups') }), resolve(join(tmpdir(), 'custom-backups')))

  const entity = normalizeEntity({
    id: 'e1',
    name: '小王',
    type: 'person',
    confidence: '0.5',
    mention_count: 3,
    extra_json: JSON.stringify({ disambig: ['works_at:字节'] }),
    grade: 'A',
    group_paths: '我们/朋友||工作',
  })
  assert.equal(entity.id, 'e1')
  assert.equal(entity.confidence, 0.5)
  assert.equal(entity.mentions, 3)
  assert.deepEqual(entity.groups, ['我们/朋友', '工作'])
  assert.deepEqual(entity.disambig, ['works_at:字节'])

  // 第 8 轮：kind/status 枚举与批量 patch 白名单
  assert.deepEqual([...MEMORY_KINDS], ['episodic', 'semantic', 'person', 'relation', 'experience', 'goal', 'meta'])
  assert.deepEqual([...MEMORY_STATUSES], ['active', 'contested', 'dormant', 'archived'])
  assert.deepEqual([...BATCH_UPDATE_FIELDS], ['kind', 'grade', 'group', 'sensitivity', 'importance', 'status'])
  assert.deepEqual([...MEMORY_SORTS], ['relevance', 'time', 'importance', 'uses'])

  assert.match(localDayKey(), /^\d{4}-\d{2}-\d{2}$/)

  const normalized = normalizeMemory({
    id: 'm1',
    text: '薄荷',
    score: '0.5',
    day: '2026-11-05',
    kind: 'semantic',
    grade: 'S',
    status: 'active',
    provenance: 'user_explicit',
    confidence: '0.9',
    importance: 0.5,
    groups: ['我们/经历'],
    channels: ['tok', 'vec'],
    pinned: 1,
  })
  assert.equal(normalized.id, 'm1')
  assert.equal(normalized.score, 0.5)
  assert.equal(normalized.confidence, 0.9)
  assert.equal(normalized.why, '词法命中 + 语义相近')
  assert.deepEqual(normalized.groups, ['我们/经历'])
  assert.equal(normalized.pinned, true)
  assert.equal(normalized.truncated, false)
  // 缺字段时不能抛，前端拿到的是 null/[] 而不是 undefined
  const sparse = normalizeMemory({ id: 'm2' })
  assert.equal(sparse.score, null)
  assert.deepEqual(sparse.groups, [])
  assert.equal(sparse.why, null)
  assert.equal(sparse.source_kind, null)

  // 没有 node:sqlite 或库不存在时必须返回 null，而不是炸掉
  const missing = openMemoryReader(join(tmpdir(), `pmem-missing-${Date.now()}.db`))
  assert.ok(missing === null || typeof missing.recent === 'function')
  if (missing) assert.equal(missing.recent(5), null)
  assert.equal(openMemoryReader(''), null)
  assert.equal(openMemoryReader(null), null)
}

// ---------------------------------------------------------------------------
// 1. 路由表：11 条 exact，没有 chats/SSE 残留
// ---------------------------------------------------------------------------

{
  const { routes } = makeConsole()
  assert.equal(routes.length, 20, `路由数不对：${routes.map(item => item.path).join(',')}`)
  const paths = new Set(routes.map(item => item.path))
  for (const [name, path] of Object.entries(PATHS)) {
    assert.ok(paths.has(path), `缺路由 ${name} (${path})`)
  }
  for (const route of routes) {
    assert.equal(route.kind, 'exact', `${route.path} 应该是 exact`)
    assert.ok(route.path.startsWith(CONSOLE_API), `${route.path} 不在控制台前缀下`)
    assert.equal(typeof route.handler, 'function')
  }
  // 需求纠偏：聊天/SSE/模型整段作废，不能偷偷回来
  const joined = routes.map(item => item.path).join(' ')
  assert.ok(!/chats|messages|stream/i.test(joined), `控制台不该再有聊天路由：${joined}`)
  assert.ok(FEEDBACK_SIGNALS.includes('useful'))
}

// ---------------------------------------------------------------------------
// 2. bootstrap
// ---------------------------------------------------------------------------

{
  const rules = [
    {
      args: ['profile'],
      reply: {
        ok: true,
        data: { traits: [{ name: '安静' }], goals: [{ id: 'g1' }], rendered: '画像', tokens: 5, budget: 300, truncated: false },
      },
    },
    {
      args: ['sleep', 'log'],
      reply: { ok: true, data: { runs: [{ day: '2026-11-04', status: 'ok', stages: [] }] } },
    },
    { args: ['conflicts', 'list'], reply: { ok: true, data: { items: [{ id: 'c1' }] } } },
  ]
  const { routes, callPmem } = makeConsole({
    rules,
    reader: {
      recent: () => [{ id: 'm1', text_raw: '最近的记忆', day_key: '2026-11-05', kind: 'semantic', status: 'active' }],
      counts: () => ({ total: 7, today_new: 2, uses: 9, today_recalls: 5, week_new: 6, pinned: 4 }),
    },
  })
  const out = await callRoute(routes, PATHS.bootstrap)
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.ok, true)
  assert.equal(out.body.version, 'test')
  assert.equal(out.body.status.db.memory_total, 7)
  assert.match(out.body.wake.text, /PMEM_WAKE/)
  assert.ok(out.body.wake.tokens > 0)
  assert.equal(out.body.profile.traits.length, 1)
  assert.equal(out.body.profile.open_goals.length, 1)
  assert.equal(out.body.recent[0].id, 'm1')
  assert.equal(out.body.recent[0].text, '最近的记忆')
  assert.equal(out.body.counts.today_new, 2)
  assert.equal(out.body.counts.uses, 9)
  assert.equal(out.body.counts.today_recalls, 5)
  assert.equal(out.body.counts.week_new, 6)
  assert.equal(out.body.counts.pinned, 4)
  assert.equal(out.body.counts.open_conflicts, 1)
  assert.equal(out.body.counts.memory_total, 7)
  assert.equal(out.body.sleep.runs, 1)
  assert.equal(out.body.sleep.latest.day, '2026-11-04')
  assert.equal(out.body.starmap.ready, true)
  assert.deepEqual(out.body.degraded, [])
  // 用的还是既有 CLI：profile / sleep log / conflicts list
  assert.ok(callPmem.argsOf('profile').length === 1)
  assert.ok(callPmem.argsOf('sleep').some(args => args[1] === 'log'))
  assert.ok(callPmem.argsOf('conflicts').some(args => args[1] === 'list'))

  // 子调用失败 → 仍 200，字段给空、degraded 如实说
  const degradedConsole = makeConsole({
    rules: [
      { args: ['profile'], reply: cliError('E_SPAWN', '没有 python') },
      { args: ['sleep', 'log'], reply: new Error('炸了') },
    ],
  })
  const degraded = await callRoute(degradedConsole.routes, PATHS.bootstrap)
  assert.equal(degraded.status, 200)
  assert.equal(degraded.body.ok, true)
  assert.deepEqual(degraded.body.profile.traits, [])
  assert.equal(degraded.body.sleep.runs, 0)
  assert.ok(degraded.body.degraded.includes('profile'))
  assert.ok(degraded.body.degraded.includes('sleep'))
  assert.ok(degraded.body.degraded.includes('conflicts') === false)
}

// bootstrap 的 status 兜底：没有 snapshot 时用 stats() 拼一个
{
  const { routes } = makeConsole({
    snapshot: null,
    rules: [{ args: ['stats'], reply: { ok: true, data: { memory_total: 3, event_count: 4, recent: [{ id: 'r9', text: '旧 recent' }] } } }],
    stats: async () => ({ memory_total: 3, event_count: 4, recent: [{ id: 'r9', text: '旧 recent' }] }),
    reader: null,
  })
  const out = await callRoute(routes, PATHS.bootstrap)
  assert.equal(out.status, 200)
  assert.equal(out.body.status.db.memory_total, 3)
  assert.equal(out.body.recent[0].id, 'r9')
}

// ---------------------------------------------------------------------------
// 3. memories：搜索、过滤、k 夹取、recent 与降级
// ---------------------------------------------------------------------------

const SEARCH_ITEMS = [
  {
    id: 'm1', text: '薄荷长得很旺', score: 0.91, kind: 'semantic', grade: 'S', status: 'active',
    provenance: 'user_explicit', confidence: 0.9, importance: 0.6, groups: [], channels: ['tok', 'vec'],
  },
  {
    id: 'm2', text: '旧薄荷', score: 0.4, kind: 'semantic', grade: 'B', status: 'archived',
    provenance: 'inferred', confidence: 0.5, importance: 0.2, groups: ['我们/经历'],
  },
  {
    id: 'm3', text: '薄荷日记', score: 0.3, kind: 'episodic', grade: 'A', status: 'active',
    provenance: 'observed', confidence: 0.7, importance: 0.4, groups: [],
  },
]

{
  const { routes, callPmem } = makeConsole({
    rules: [{ args: ['search'], reply: { ok: true, data: { items: SEARCH_ITEMS } } }],
  })
  const out = await callRoute(
    routes,
    PATHS.memories,
    { url: `${PATHS.memories}?q=${encodeURIComponent('薄荷')}&k=999&mode=keyword&grade=S,A&status=active` },
  )
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.ok, true)
  assert.equal(out.body.mode, 'keyword')
  assert.equal(out.body.k, 50)
  assert.equal(out.body.total, 2)
  assert.deepEqual(out.body.items.map(item => item.id), ['m1', 'm3'])
  assert.equal(out.body.items[0].score, 0.91)
  assert.equal(out.body.items[0].why, '词法命中 + 语义相近')
  const searchArgs = callPmem.argsOf('search')[0]
  assert.deepEqual(searchArgs.slice(0, 5), ['search', '薄荷', '--k', '50', '--mode'])
  assert.ok(searchArgs.includes('--grade'))
  assert.equal(searchArgs.filter(value => value === '--grade').length, 2)

  // 非法参数
  const badMode = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=x&mode=semantic`,
  })
  assert.equal(badMode.status, 400)
  const badGrade = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=x&grade=Z`,
  })
  assert.equal(badGrade.status, 400)
  // 读路由不收 POST
  const postRead = await callRoute(routes, PATHS.memories, {
    method: 'POST',
    body: '{}',
    headers: { 'content-type': 'application/json' },
  })
  assert.equal(postRead.status, 405)
}

// recent 模式：走只读读取器，带 kind/grade/status 过滤
{
  const seen = []
  const { routes } = makeConsole({
    reader: {
      recent(limit, filters) {
        seen.push({ limit, filters })
        return [{ id: 'r1', text_raw: '最近一条', day_key: '2026-11-05', kind: 'semantic', status: 'active', grade: 'A', group_paths: '我们/经历||工作' }]
      },
      counts: () => ({ total: 1, today_new: 1, uses: 0 }),
    },
  })
  const out = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?mode=recent&k=10&kind=semantic&grade=A&status=active`,
  })
  assert.equal(out.status, 200)
  assert.equal(out.body.effective, 'recent')
  assert.equal(out.body.mode, 'recent')
  assert.equal(out.body.items[0].id, 'r1')
  assert.equal(out.body.items[0].day, '2026-11-05')
  assert.deepEqual(out.body.items[0].groups, ['我们/经历', '工作'])
  assert.equal(seen[0].limit, 10)
  assert.deepEqual(seen[0].filters.kind, ['semantic'])
  assert.deepEqual(seen[0].filters.grade, ['A'])
  assert.deepEqual(seen[0].filters.status, ['active'])

  // 空查询 = 看最近
  const empty = await callRoute(routes, PATHS.memories, { url: PATHS.memories })
  assert.equal(empty.status, 200)
  assert.equal(empty.body.effective, 'recent')
  assert.equal(empty.body.items[0].id, 'r1')
}

// recent 降级：读取器拿不到时退回 stats.recent
{
  const { routes } = makeConsole({
    reader: null,
    rules: [{ args: ['stats'], reply: { ok: true, data: { recent: [{ id: 's1', text: 'stats recent', day: '2026-11-05', kind: 'semantic', status: 'active' }] } } }],
  })
  const out = await callRoute(routes, PATHS.memories, { url: PATHS.memories })
  assert.equal(out.status, 200)
  assert.equal(out.body.effective, 'recent')
  assert.equal(out.body.items[0].id, 's1')
}

// 真分页（无 q）：reader.page 拿 total/items，参数夹取
{
  const seen = []
  const reader = {
    recent: () => [],
    counts: () => null,
    page(args) {
      seen.push(args)
      return {
        total: 30,
        items: [
          { id: 'p1', text_raw: '第一页一', day_key: '2026-11-05', kind: 'semantic', status: 'active', importance: 0.3 },
          { id: 'p2', text_raw: '第一页二', day_key: '2026-11-04', kind: 'semantic', status: 'active', importance: 0.2 },
        ],
      }
    },
  }
  const { routes } = makeConsole({ reader })
  const out = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?offset=20&limit=10&sort=importance&since=2026-11-01&until=2026-11-30&kind=semantic&pinned=1`,
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.offset, 20)
  assert.equal(out.body.limit, 10)
  assert.equal(out.body.k, 10)
  assert.equal(out.body.sort, 'importance')
  assert.equal(out.body.total, 30)
  assert.equal(out.body.items.length, 2)
  assert.equal(out.body.hasMore, true)
  assert.equal(seen[0].limit, 10)
  assert.equal(seen[0].offset, 20)
  assert.equal(seen[0].sort, 'importance')
  assert.equal(seen[0].filters.since, '2026-11-01')
  assert.equal(seen[0].filters.until, '2026-11-30')
  assert.equal(seen[0].filters.pinned, true)
  assert.deepEqual(seen[0].filters.kind, ['semantic'])

  // 越界参数：offset 夹到 1000、limit 夹到 50
  const clamped = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?offset=99999&limit=999&sort=uses`,
  })
  assert.equal(clamped.status, 200)
  assert.equal(clamped.body.offset, 1000)
  assert.equal(clamped.body.limit, 50)
  assert.equal(seen.at(-1).offset, 1000)
  assert.equal(seen.at(-1).limit, 50)
  assert.equal(seen.at(-1).sort, 'uses')

  // 非法 sort / 日期
  const badSort = await callRoute(routes, PATHS.memories, { url: `${PATHS.memories}?sort=recent` })
  assert.equal(badSort.status, 400)
  const badSince = await callRoute(routes, PATHS.memories, { url: `${PATHS.memories}?since=2026-13-01` })
  assert.equal(badSince.status, 400)
  const badUntil = await callRoute(routes, PATHS.memories, { url: `${PATHS.memories}?until=${encodeURIComponent('昨天')}` })
  assert.equal(badUntil.status, 400)
  const inverted = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?since=2026-11-30&until=2026-11-01`,
  })
  assert.equal(inverted.status, 400)
}

// 有 q：先取 min(200, offset+limit) 的池子，再后端排序/切片/日期过滤
{
  const searchItems = [
    { id: 'a', text: '测试一', score: 0.9, day: '2026-11-05', importance: 0.2, use_count: 1, kind: 'semantic', grade: 'A', status: 'active', provenance: 'user_explicit', confidence: 0.5 },
    { id: 'b', text: '测试二', score: 0.8, day: '2026-11-04', importance: 0.9, use_count: 5, kind: 'semantic', grade: 'A', status: 'active', provenance: 'user_explicit', confidence: 0.5 },
    { id: 'c', text: '测试三', score: 0.7, day: '2026-11-03', importance: 0.5, use_count: 9, kind: 'semantic', grade: 'B', status: 'active', provenance: 'user_explicit', confidence: 0.5 },
    { id: 'd', text: '测试四', score: 0.6, day: '2026-10-01', importance: 0.4, use_count: 0, kind: 'semantic', grade: 'B', status: 'active', provenance: 'user_explicit', confidence: 0.5 },
    { id: 'e', text: '测试五', score: 0.5, day: '2026-11-05', importance: 0.4, use_count: 2, kind: 'semantic', grade: 'C', status: 'active', provenance: 'user_explicit', confidence: 0.5 },
  ]
  const { routes, callPmem } = makeConsole({
    rules: [{ args: ['search'], reply: { ok: true, data: { items: searchItems } } }],
  })
  const page1 = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&limit=2&offset=0&mode=keyword`,
  })
  assert.equal(page1.status, 200, JSON.stringify(page1.body))
  assert.equal(page1.body.sort, 'relevance')
  assert.equal(page1.body.total, 5)
  assert.equal(page1.body.hasMore, true)
  assert.deepEqual(page1.body.items.map(item => item.id), ['a', 'b'])
  assert.equal(callPmem.argsOf('search')[0][3], '2', '第一页 --k 应该是 offset+limit=2')
  assert.equal(callPmem.argsOf('search')[0][5], 'keyword')

  const page2 = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&limit=2&offset=2`,
  })
  assert.equal(page2.status, 200)
  assert.deepEqual(page2.body.items.map(item => item.id), ['c', 'd'])
  assert.equal(page2.body.total, 5)
  assert.equal(page2.body.hasMore, true)
  // 第二页不与第一页重复
  const firstIds = new Set(page1.body.items.map(item => item.id))
  assert.ok(page2.body.items.every(item => !firstIds.has(item.id)), '第二页出现了第一页的记忆')
  assert.equal(callPmem.argsOf('search').at(-1)[3], '4', '第二页 --k 应该是 offset+limit=4')

  // 排序
  const byImportance = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&limit=2&sort=importance`,
  })
  assert.deepEqual(byImportance.body.items.map(item => item.id), ['b', 'c'])
  const byUses = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&limit=2&sort=uses`,
  })
  assert.deepEqual(byUses.body.items.map(item => item.id), ['c', 'b'])
  const byTime = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&limit=2&sort=time`,
  })
  assert.deepEqual(byTime.body.items.map(item => item.id), ['a', 'e'])

  // 日期过滤：只留 2026-11-04
  const dated = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&since=2026-11-04&until=2026-11-04`,
  })
  assert.equal(dated.status, 200)
  assert.deepEqual(dated.body.items.map(item => item.id), ['b'])
  assert.equal(dated.body.total, 1)
  const future = await callRoute(routes, PATHS.memories, {
    url: `${PATHS.memories}?q=${encodeURIComponent('测试')}&since=2099-01-01`,
  })
  assert.equal(future.status, 200)
  assert.deepEqual(future.body.items, [])
}

// 搜索失败 → 把 CLI 的错误码映射成 HTTP
{
  const { routes } = makeConsole({
    rules: [{ args: ['search'], reply: new Error('python 不见了') }],
  })
  const out = await callRoute(routes, PATHS.memories, { url: `${PATHS.memories}?q=x` })
  assert.equal(out.status, 500)
  assert.equal(out.body.ok, false)
}

// ---------------------------------------------------------------------------
// 4. memory 详情
// ---------------------------------------------------------------------------

const GET_ITEM = {
  id: 'm1',
  text: '小米枣喜欢薄荷',
  kind: 'semantic',
  status: 'active',
  importance: 0.6,
  confidence: 0.9,
  provenance: 'user_explicit',
  sensitivity: 'personal',
  pinned: true,
  day: '2026-11-05',
  created_at: '2026-11-05T10:00:00Z',
  use_count: 3,
  evidence_count: 1,
  groups: ['我们/经历'],
  grade: 'S',
  source: { kind: 'cli' },
  extra: { evidence_sessions: 1 },
  retracted: false,
}

{
  const { routes, callPmem } = makeConsole({
    rules: [
      { args: ['get', 'm1'], reply: { ok: true, data: GET_ITEM } },
      {
        args: ['history', 'm1'],
        reply: {
          ok: true,
          data: {
            items: [
              { event: 'e1', ts: '2026-11-05T10:00:00Z', actor: 'system', op: 'memory.add', payload: { kind: 'semantic' } },
              { event: 'e2', ts: '2026-11-05T11:00:00Z', actor: 'ai', op: 'memory.update', payload: { rationale: '手动调重要度' } },
            ],
          },
        },
      },
      {
        args: ['trace'],
        reply: {
          ok: true,
          data: {
            items: [{ session_id: 's1', line_no: 2, role: 'user', ts: '2026-11-05T09:00:00Z', text: '小米枣喜欢薄荷', id: 'l1', retracted: false }],
          },
        },
      },
      {
        args: ['conflicts', 'list'],
        reply: {
          ok: true,
          data: {
            items: [
              { id: 'c1', kind: 'polarity', slot: 'like', severity: 0.8, status: 'open', left: 'm1', right: 'm9', created_at: '2026-11-05T12:00:00Z' },
              { id: 'c2', kind: 'polarity', slot: 'like', severity: 0.5, status: 'open', left: 'm7', right: 'm8', created_at: '2026-11-05T12:00:00Z' },
            ],
          },
        },
      },
    ],
  })
  const out = await callRoute(routes, PATHS.memory, { url: `${PATHS.memory}?id=m1` })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.ok, true)
  assert.equal(out.body.item.id, 'm1')
  assert.equal(out.body.item.text, '小米枣喜欢薄荷')
  assert.equal(out.body.item.pinned, true)
  assert.equal(out.body.item.sensitivity, 'personal')
  assert.equal(out.body.item.use_count, 3)
  assert.equal(out.body.history.length, 2)
  assert.equal(out.body.history[1].action, 'memory.update')
  assert.equal(out.body.history[1].note, '手动调重要度')
  assert.equal(out.body.evidence.length, 1)
  assert.equal(out.body.evidence[0].session, 's1')
  assert.equal(out.body.evidence[0].line, 2)
  assert.equal(out.body.conflicts.length, 1)
  assert.equal(out.body.conflicts[0].other, 'm9')
  // trace 用的是正文前 60 字
  const traceArgs = callPmem.argsOf('trace')[0]
  assert.equal(traceArgs[1], '小米枣喜欢薄荷')

  // id 校验
  const missing = await callRoute(routes, PATHS.memory)
  assert.equal(missing.status, 400)
  const evil = await callRoute(routes, PATHS.memory, { url: `${PATHS.memory}?id=${encodeURIComponent('-rf /tmp/x')}` })
  assert.equal(evil.status, 400)
  const postRead = await callRoute(routes, PATHS.memory, {
    method: 'POST', body: '{}', headers: { 'content-type': 'application/json' },
  })
  assert.equal(postRead.status, 405)
}

// 404：CLI 返回 ok:false（走 callPmem 的抛错路径也认）
{
  const notFound = makeConsole({
    rules: [{ args: ['get'], reply: cliError('E_NOT_FOUND', '找不到记忆 m9') }],
  })
  const out = await callRoute(notFound.routes, PATHS.memory, { url: `${PATHS.memory}?id=m9` })
  assert.equal(out.status, 404)
  assert.equal(out.body.code, 'E_NOT_FOUND')

  const thrown = new Error('Command failed')
  thrown.stdout = JSON.stringify(cliError('E_NOT_FOUND', '记忆已被删除'))
  const viaThrow = makeConsole({
    rules: [{ args: ['get'], reply: thrown }],
  })
  const out2 = await callRoute(viaThrow.routes, PATHS.memory, { url: `${PATHS.memory}?id=m9` })
  assert.equal(out2.status, 404)
  assert.equal(out2.body.code, 'E_NOT_FOUND')
}

// evidence / conflicts 失败只降级，不影响详情
{
  const { routes } = makeConsole({
    rules: [
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
      { args: ['history'], reply: cliError('E_SPAWN', '没有 python') },
      { args: ['conflicts', 'list'], reply: new Error('炸了') },
    ],
  })
  const out = await callRoute(routes, PATHS.memory, { url: `${PATHS.memory}?id=m1` })
  assert.equal(out.status, 200)
  assert.deepEqual(out.body.history, [])
  assert.deepEqual(out.body.evidence, [])
  assert.deepEqual(out.body.conflicts, [])
  assert.ok(out.body.degraded.includes('history'))
  assert.ok(out.body.degraded.includes('conflicts'))
}

// ---------------------------------------------------------------------------
// 5. 写路由：feedback / update / delete
// ---------------------------------------------------------------------------

{
  const { routes, callPmem } = makeConsole({
    rules: [
      { args: ['feedback'], reply: { ok: true, data: { id: 'm1', signal: 'useful', importance: 0.7 } } },
      { args: ['update'], reply: { ok: true, data: { id: 'm1' } } },
      { args: ['group', 'assign'], reply: { ok: true, data: { ok: true } } },
      { args: ['delete'], reply: { ok: true, data: { deleted: 1, hard: false, cascaded: {} } } },
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
    ],
  })

  const feedback = await callRoute(routes, PATHS.feedback, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', signal: 'useful', note: '这条有用' }),
    headers: { 'content-type': 'application/json' },
  })
  assert.equal(feedback.status, 200, JSON.stringify(feedback.body))
  assert.equal(feedback.body.ok, true)
  assert.equal(feedback.body.signal, 'useful')
  assert.deepEqual(callPmem.argsOf('feedback')[0], ['feedback', 'm1', 'useful', '--note', '这条有用'])

  const badSignal = await callRoute(routes, PATHS.feedback, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', signal: 'like' }),
  })
  assert.equal(badSignal.status, 400)
  const badId = await callRoute(routes, PATHS.feedback, {
    method: 'POST',
    body: JSON.stringify({ id: 'a b', signal: 'useful' }),
  })
  assert.equal(badId.status, 400)
  const getFeedback = await callRoute(routes, PATHS.feedback)
  assert.equal(getFeedback.status, 405)
  const badJson = await callRoute(routes, PATHS.feedback, { method: 'POST', body: '{' })
  assert.equal(badJson.status, 400)
  const arrayBody = await callRoute(routes, PATHS.feedback, { method: 'POST', body: '[]' })
  assert.equal(arrayBody.status, 400)
  const huge = await callRoute(routes, PATHS.feedback, {
    method: 'POST',
    body: Buffer.alloc(MAX_BODY + 1024, 0x61),
  })
  assert.equal(huge.status, 413)
  assert.equal(huge.body.code, 'E_TOO_LARGE')

  const update = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', importance: 0.85, pinned: true, status: 'archived', grade: 'A', reason: '手动' }),
  })
  assert.equal(update.status, 200, JSON.stringify(update.body))
  assert.equal(update.body.ok, true)
  assert.equal(update.body.item.id, 'm1')
  const updateArgs = callPmem.argsOf('update')[0]
  assert.deepEqual(updateArgs.slice(0, 2), ['update', 'm1'])
  assert.ok(updateArgs.includes('--importance'))
  assert.ok(updateArgs.includes('0.85'))
  assert.ok(updateArgs.includes('--pin'))
  assert.ok(updateArgs.includes('--status'))
  assert.ok(updateArgs.includes('archived'))
  assert.ok(updateArgs.includes('--reason'))
  const assignArgs = callPmem.argsOf('group')[0]
  assert.deepEqual(assignArgs.slice(0, 5), ['group', 'assign', 'memory', 'm1', '--grade'])
  assert.equal(assignArgs[5], 'A')
  assert.equal(update.body.updated, true)
  assert.equal(update.body.failed.length, 0)
  assert.equal(update.body.applied.importance, 0.85)
  assert.equal(update.body.applied.status, 'archived')
  assert.equal(update.body.applied.grade, 'A')

  const unpin = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', pinned: false }),
  })
  assert.equal(unpin.status, 200)
  assert.ok(callPmem.argsOf('update').at(-1).includes('--unpin'))

  const empty = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1' }),
  })
  assert.equal(empty.status, 400)
  const badImportance = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', importance: '高' }),
  })
  assert.equal(badImportance.status, 400)
  const badGrade = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', grade: 'Z' }),
  })
  assert.equal(badGrade.status, 400)

  const removeHard = await callRoute(routes, PATHS.remove, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', hard: true }),
  })
  assert.equal(removeHard.status, 200)
  assert.equal(removeHard.body.hard, true)
  assert.equal(removeHard.body.deleted, 1)
  assert.deepEqual(callPmem.argsOf('delete').at(-1), ['delete', 'm1', '--hard'])
  const removeSoft = await callRoute(routes, PATHS.remove, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', hard: false }),
  })
  assert.equal(removeSoft.status, 200)
  assert.equal(removeSoft.body.hard, false)
  assert.deepEqual(callPmem.argsOf('delete').at(-1), ['delete', 'm1'])
  const badHard = await callRoute(routes, PATHS.remove, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', hard: 'yes' }),
  })
  assert.equal(badHard.status, 400)
  const method = await callRoute(routes, PATHS.remove)
  assert.equal(method.status, 405)
}

// 第 8 轮：kind / group / status 支持，部分失败如实报
{
  const { routes, callPmem } = makeConsole({
    rules: [
      { args: ['update'], reply: { ok: true, data: {} } },
      { args: ['group', 'assign'], reply: { ok: true, data: { groups: ['工作/验收'], grade: 'B' } } },
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
    ],
  })
  const out = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({
      id: 'm1',
      kind: 'episodic',
      status: 'dormant',
      sensitivity: 'secret',
      importance: '0.5',
      pinned: true,
      group: '工作/验收',
      grade: 'B',
      reason: '批量改',
    }),
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.ok, true)
  assert.deepEqual(out.body.applied, {
    kind: 'episodic',
    status: 'dormant',
    sensitivity: 'secret',
    importance: 0.5,
    pinned: true,
    group: '工作/验收',
    grade: 'B',
  })
  assert.deepEqual(out.body.failed, [])
  assert.deepEqual(callPmem.argsOf('update')[0], [
    'update', 'm1', '--kind', 'episodic', '--status', 'dormant',
    '--sensitivity', 'secret', '--importance', '0.5', '--pin', '--reason', '批量改',
  ])
  assert.deepEqual(callPmem.argsOf('group')[0], [
    'group', 'assign', 'memory', 'm1', '--path', '工作/验收', '--grade', 'B', '--rationale', '批量改',
  ])

  // 非法枚举/路径：400，且带 field
  const invalidCases = [
    [{ id: 'm1', kind: 'book' }, 'kind'],
    [{ id: 'm1', status: 'deleted' }, 'status'],
    [{ id: 'm1', group: '-rf' }, 'group'],
    [{ id: 'm1', sensitivity: 'top-secret' }, 'sensitivity'],
  ]
  for (const [payload, field] of invalidCases) {
    const bad = await callRoute(routes, PATHS.update, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    assert.equal(bad.status, 400, `${JSON.stringify(payload)} 返回 ${bad.status}`)
    assert.equal(bad.body.field, field)
  }
  // 只有 reason 不算更新
  const reasonOnly = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', reason: '只写备注' }),
  })
  assert.equal(reasonOnly.status, 400)

  // 部分失败：kind 成功、group 失败 → 200 + ok:false + applied/failed
  const partial = makeConsole({
    rules: [
      { args: ['update'], reply: { ok: true, data: {} } },
      { args: ['group', 'assign'], reply: cliError('E_SPAWN', 'python 不见了') },
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
    ],
  })
  const half = await callRoute(partial.routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', kind: 'semantic', group: '工作/验收' }),
  })
  assert.equal(half.status, 200, JSON.stringify(half.body))
  assert.equal(half.body.ok, false)
  assert.deepEqual(half.body.applied, { kind: 'semantic' })
  assert.equal(half.body.failed.length, 1)
  assert.equal(half.body.failed[0].field, 'group')
  assert.equal(half.body.failed[0].error.code, 'E_SPAWN')

  // 全部失败（找不到记忆）→ 404，applied 空、failed 带字段
  const broken = makeConsole({
    rules: [{ args: ['update'], reply: cliError('E_NOT_FOUND', '找不到记忆') }],
  })
  const allFail = await callRoute(broken.routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm9', kind: 'semantic' }),
  })
  assert.equal(allFail.status, 404)
  assert.equal(allFail.body.ok, false)
  assert.deepEqual(allFail.body.applied, {})
  assert.equal(allFail.body.failed[0].field, 'kind')
}

// 等级写失败：元数据已改，但要如实报错（不能假装全成功）
{
  const { routes } = makeConsole({
    rules: [
      { args: ['update'], reply: { ok: true, data: {} } },
      { args: ['group', 'assign'], reply: cliError('E_SPAWN', 'python 不见了') },
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
    ],
  })
  const out = await callRoute(routes, PATHS.update, {
    method: 'POST',
    body: JSON.stringify({ id: 'm1', grade: 'S' }),
  })
  assert.equal(out.status, 500)
  assert.equal(out.body.ok, false)
  assert.deepEqual(out.body.applied, {})
  assert.equal(out.body.failed.length, 1)
  assert.equal(out.body.failed[0].field, 'grade')
  assert.equal(out.body.failed[0].error.code, 'E_SPAWN')
}

// delete 的 CLI 404
{
  const { routes } = makeConsole({
    rules: [{ args: ['delete'], reply: cliError('E_NOT_FOUND', '找不到记忆') }],
  })
  const out = await callRoute(routes, PATHS.remove, {
    method: 'POST',
    body: JSON.stringify({ id: 'm9' }),
  })
  assert.equal(out.status, 404)
}

// ---------------------------------------------------------------------------
// 6. profile / groups / conflicts / stats / doctor
// ---------------------------------------------------------------------------

{
  const { routes, callPmem } = makeConsole({
    rules: [
      {
        args: ['profile'],
        reply: { ok: true, data: { traits: [{ name: '安静' }], goals: [], rendered: '画像', tokens: 3, budget: 300, truncated: false } },
      },
      { args: ['group', 'tree'], reply: { ok: true, data: { items: [{ path: '我们', members: 1 }] } } },
      { args: ['group', 'stats'], reply: { ok: true, data: { groups: 2, memberships: 3 } } },
      { args: ['group', 'targets'], reply: { ok: true, data: { items: [{ id: 'm1', text: '薄荷' }] } } },
      { args: ['conflicts', 'list'], reply: { ok: true, data: { items: [{ id: 'c1' }] } } },
      { args: ['stats'], reply: { ok: true, data: { memory_total: 7, by_kind: { semantic: 7 }, db_size_bytes: 1024 } } },
      { args: ['receipt'], reply: { ok: true, data: { samples: 3, days: 30 } } },
      { args: ['net', 'stats'], reply: { ok: true, data: { entities: 2, relations: 1 } } },
      {
        args: ['doctor'],
        reply: { ok: true, data: { integrity: true, event_chain: true, memory_total: 7, invariants: { ok: true } } },
      },
    ],
    reader: { recent: () => [], counts: () => ({ total: 7, today_new: 1, uses: 4 }) },
  })

  const profile = await callRoute(routes, PATHS.profile, { url: `${PATHS.profile}?budget=9999` })
  assert.equal(profile.status, 200)
  assert.equal(profile.body.budget, 1200)
  assert.equal(profile.body.profile.traits[0].name, '安静')
  assert.deepEqual(callPmem.argsOf('profile').at(-1), ['profile', '--budget', '1200'])

  const groups = await callRoute(routes, PATHS.groups)
  assert.equal(groups.status, 200)
  assert.equal(groups.body.tree.length, 1)
  assert.equal(groups.body.stats.groups, 2)

  const targets = await callRoute(routes, PATHS.groups, {
    url: `${PATHS.groups}?path=${encodeURIComponent('我们/经历')}&tbl=memory&limit=5`,
  })
  assert.equal(targets.status, 200)
  assert.equal(targets.body.path, '我们/经历')
  assert.equal(targets.body.items[0].id, 'm1')
  assert.deepEqual(callPmem.argsOf('group').at(-1), ['group', 'targets', '我们/经历', '--tbl', 'memory', '--limit', '5'])
  const badTbl = await callRoute(routes, PATHS.groups, {
    url: `${PATHS.groups}?path=x&tbl=memorys`,
  })
  assert.equal(badTbl.status, 400)
  const badPath = await callRoute(routes, PATHS.groups, {
    url: `${PATHS.groups}?path=${encodeURIComponent('-rf')}`,
  })
  assert.equal(badPath.status, 400)

  const conflicts = await callRoute(routes, PATHS.conflicts, {
    url: `${PATHS.conflicts}?status=resolved&limit=999`,
  })
  assert.equal(conflicts.status, 200)
  assert.equal(conflicts.body.limit, 200)
  assert.equal(conflicts.body.items[0].id, 'c1')
  assert.deepEqual(callPmem.argsOf('conflicts').at(-1), ['conflicts', 'list', '--status', 'resolved', '--limit', '200'])
  const badStatus = await callRoute(routes, PATHS.conflicts, {
    url: `${PATHS.conflicts}?status=Bad!`,
  })
  assert.equal(badStatus.status, 400)

  const stats = await callRoute(routes, PATHS.stats)
  assert.equal(stats.status, 200, JSON.stringify(stats.body))
  assert.equal(stats.body.stats.memory_total, 7)
  assert.equal(stats.body.groups.groups, 2)
  assert.equal(stats.body.receipt.samples, 3)
  assert.equal(stats.body.net.entities, 2)
  assert.equal(stats.body.conflicts.open, 1)
  assert.equal(stats.body.counts.today_new, 1)
  assert.deepEqual(stats.body.degraded, [])

  const doctor = await callRoute(routes, PATHS.doctor)
  assert.equal(doctor.status, 200)
  assert.equal(doctor.body.doctor.integrity, true)
  assert.equal(doctor.body.stats.memory_total, 7)
  assert.equal(doctor.body.stats.integrity_ok, false)
}

// stats：只读子项失败要降级，主 stats 失败才是 500
{
  const { routes } = makeConsole({
    rules: [
      { args: ['stats'], reply: { ok: true, data: { memory_total: 7 } } },
      { args: ['group', 'stats'], reply: cliError('E_SPAWN', '没有 python') },
      { args: ['receipt'], reply: new Error('炸了') },
      { args: ['net', 'stats'], reply: { ok: true, data: {} } },
      { args: ['conflicts', 'list'], reply: { ok: true, data: { items: [] } } },
    ],
  })
  const out = await callRoute(routes, PATHS.stats)
  assert.equal(out.status, 200)
  assert.equal(out.body.groups, null)
  assert.equal(out.body.receipt, null)
  assert.ok(out.body.degraded.includes('groups'))
  assert.ok(out.body.degraded.includes('receipt'))

  const broken = makeConsole({ rules: [{ args: ['stats'], reply: cliError('E_SPAWN', 'db 打不开') }] })
  const failed = await callRoute(broken.routes, PATHS.stats)
  assert.equal(failed.status, 500)
  assert.equal(failed.body.ok, false)
}

// doctor / profile 的失败映射
{
  const { routes } = makeConsole({
    rules: [
      { args: ['doctor'], reply: cliError('E_SPAWN', '没有 python') },
      { args: ['stats'], reply: { ok: true, data: {} } },
    ],
  })
  const out = await callRoute(routes, PATHS.doctor)
  assert.equal(out.status, 500)
  assert.equal(out.body.ok, false)
}

{
  const { routes } = makeConsole({
    rules: [{ args: ['profile'], reply: cliError('E_SPAWN', '没有 python') }],
  })
  const out = await callRoute(routes, PATHS.profile)
  assert.equal(out.status, 500)
}

// ---------------------------------------------------------------------------
// 7. P1：实体列表 / 实体详情
// ---------------------------------------------------------------------------

function entityRow(overrides = {}) {
  return normalizeEntity({
    id: 'e1',
    name: '小王',
    type: 'person',
    name_norm: '小王',
    confidence: 0.55,
    mention_count: 2,
    first_seen: '2026-11-01T00:00:00Z',
    last_seen: '2026-11-05T00:00:00Z',
    extra_json: JSON.stringify({ disambig: ['works_at:字节'] }),
    grade: 'A',
    group_paths: '我们/朋友',
    ...overrides,
  })
}

function edgeOf(src, dst, predicate = 'works_at', id = null) {
  return {
    relation_id: id ?? `rel-${src.id}-${dst.id}`,
    src: { id: src.id, name: src.name, type: src.type },
    dst: { id: dst.id, name: dst.name, type: dst.type },
    predicate,
    hops: 1,
    weight: 0.75,
    confidence: 0.7,
    derivation: 'extracted:T0',
    evidence: [],
    path: [src.name, dst.name],
    predicates: [predicate],
  }
}

// 实体列表：走只读 reader（带 grade/groups/offset/total）
{
  let seenArgs = null
  const reader = {
    recent: () => [],
    counts: () => null,
    entities(args) {
      seenArgs = args
      return {
        total: 3,
        items: [entityRow({ id: 'e1' }), entityRow({ id: 'e2', name: '字节', type: 'org', mention_count: 1 })],
      }
    },
  }
  const { routes } = makeConsole({ reader })
  const out = await callRoute(routes, PATHS.entities, {
    url: `${PATHS.entities}?q=${encodeURIComponent('小')}&type=person&limit=5&offset=1`,
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.ok, true)
  assert.equal(out.body.total, 3)
  assert.equal(out.body.items.length, 2)
  assert.equal(out.body.hasMore, false)
  assert.equal(out.body.items[0].id, 'e1')
  assert.equal(out.body.items[0].mentions, 2)
  assert.equal(out.body.items[0].grade, 'A')
  assert.deepEqual(out.body.items[0].groups, ['我们/朋友'])
  assert.deepEqual(seenArgs, { query: '小', type: 'person', limit: 5, offset: 1 })

  const badType = await callRoute(routes, PATHS.entities, { url: `${PATHS.entities}?type=Bad!` })
  assert.equal(badType.status, 400, '非法 type 没被拦')
  const postRead = await callRoute(routes, PATHS.entities, {
    method: 'POST', body: '{}', headers: { 'content-type': 'application/json' },
  })
  assert.equal(postRead.status, 405)
}

// 实体列表降级：没有 reader 时用 CLI，offset 在 JS 里切
{
  const { routes, callPmem } = makeConsole({
    reader: null,
    rules: [{
      args: ['net', 'entities'],
      reply: {
        ok: true,
        data: {
          items: [
            { id: 'e1', name: '小王', type: 'person', mention_count: 2, confidence: 0.5 },
            { id: 'e2', name: '字节', type: 'org', mention_count: 1, confidence: 0.9 },
            { id: 'e3', name: '杭州', type: 'place', mention_count: 1, confidence: 0.4 },
          ],
        },
      },
    }],
  })
  const out = await callRoute(routes, PATHS.entities, {
    url: `${PATHS.entities}?q=${encodeURIComponent('小')}&limit=1&offset=1`,
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.items.length, 1)
  assert.equal(out.body.items[0].id, 'e2')
  assert.ok(out.body.degraded.includes('grade'))
  const args = callPmem.argsOf('net')[0]
  assert.deepEqual(args, ['net', 'entities', '--query', '小', '--limit', '2'])
}

// 实体详情：reader + 出边/入边两个 net query
{
  const outEdge = edgeOf({ id: 'e1', name: '小王', type: 'person' }, { id: 'e2', name: '字节', type: 'org' })
  const inEdge = edgeOf({ id: 'e3', name: '老板', type: 'person' }, { id: 'e1', name: '小王', type: 'person' }, 'manages', 'rel-in')
  const reader = {
    recent: () => [],
    counts: () => null,
    entityByIdOrName(ref) {
      assert.equal(ref, 'e1')
      return {
        entity: entityRow(),
        aliases: [{ alias: '鲸鲸', lang: 'zh', source: 'manual', confidence: 0.9, created_at: '2026-11-01T00:00:00Z' }],
        mentions: [{ id: 'm1', text: '小王在字节工作', day: '2026-11-01', kind: 'semantic', status: 'active', surface: '小王', confidence: 0.6, extractor: 'person_act' }],
        counts: { aliases: 1, mentions: 1, relations_out: 1, relations_in: 1 },
      }
    },
  }
  const { routes } = makeConsole({
    reader,
    rules: [{
      args: ['net', 'query'],
      reply: args => (args.includes('--dst')
        ? { ok: true, data: { items: [inEdge] } }
        : { ok: true, data: { items: [outEdge] } }),
    }],
  })
  const out = await callRoute(routes, PATHS.entity, { url: `${PATHS.entity}?id=e1` })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.entity.id, 'e1')
  assert.equal(out.body.entity.name, '小王')
  assert.equal(out.body.aliases[0].alias, '鲸鲸')
  assert.equal(out.body.mentions[0].id, 'm1')
  assert.equal(out.body.counts.mentions, 1)
  assert.equal(out.body.relations.length, 2)
  assert.deepEqual(out.body.relations.map(item => item.direction).sort(), ['in', 'out'])

  // 同名多身份：409 + candidates，不猜
  const ambiguous = makeConsole({
    reader: { ...reader, entityByIdOrName: () => ({ conflict: [entityRow({ id: 'e1' }), entityRow({ id: 'e9', name: '小王(2)' })] }) },
  })
  const conflict = await callRoute(ambiguous.routes, PATHS.entity, { url: `${PATHS.entity}?name=${encodeURIComponent('小王')}` })
  assert.equal(conflict.status, 409)
  assert.equal(conflict.body.code, 'E_CONFLICT')
  assert.equal(conflict.body.details.candidates.length, 2)

  // 参数校验 / 方法
  const noRef = await callRoute(routes, PATHS.entity)
  assert.equal(noRef.status, 400)
  const badId = await callRoute(routes, PATHS.entity, { url: `${PATHS.entity}?id=${encodeURIComponent('-rf')}` })
  assert.equal(badId.status, 400)
  const postRead = await callRoute(routes, PATHS.entity, {
    method: 'POST', body: '{}', headers: { 'content-type': 'application/json' },
  })
  assert.equal(postRead.status, 405)
}

// 实体详情：不存在 → 404；reader 缺席 → CLI 降级
{
  const missing = makeConsole({
    reader: { recent: () => [], counts: () => null, entityByIdOrName: () => null },
    rules: [{
      args: ['net', 'query'],
      reply: { ok: false, error: { code: 'E_NOT_FOUND', message: '找不到实体' } },
    }],
  })
  const notFound = await callRoute(missing.routes, PATHS.entity, { url: `${PATHS.entity}?id=ZZZZ` })
  assert.equal(notFound.status, 404)
  assert.equal(notFound.body.code, 'E_NOT_FOUND')

  const fallback = makeConsole({
    reader: null,
    rules: [{
      args: ['net', 'query'],
      reply: args => (args.includes('--dst')
        ? { ok: true, data: { items: [] } }
        : { ok: true, data: { src: { id: 'e1', name: '小王', type: 'person' }, items: [] } }),
    }],
  })
  const viaCli = await callRoute(fallback.routes, PATHS.entity, { url: `${PATHS.entity}?name=${encodeURIComponent('小王')}` })
  assert.equal(viaCli.status, 200, JSON.stringify(viaCli.body))
  assert.equal(viaCli.body.fallback, true)
  assert.equal(viaCli.body.entity.id, 'e1')
  assert.deepEqual(viaCli.body.aliases, [])
  assert.ok(viaCli.body.degraded.includes('aliases'))
}

// ---------------------------------------------------------------------------
// 8. P1：关系查询 / 合并
// ---------------------------------------------------------------------------

{
  const edge = edgeOf({ id: 'e1', name: '小王', type: 'person' }, { id: 'e2', name: '字节', type: 'org' })
  const { routes, callPmem } = makeConsole({
    rules: [{
      args: ['net', 'query'],
      reply: {
        ok: true,
        data: {
          src: { id: 'e1', name: '小王', type: 'person' },
          dst: { id: 'e2', name: '字节', type: 'org' },
          depth: 1,
          scanned: 1,
          request: { src: '小王', dst: '字节' },
          items: [edge],
        },
      },
    }],
  })
  const out = await callRoute(routes, PATHS.netQuery, {
    method: 'POST',
    body: JSON.stringify({
      src: '小王',
      dst: '字节',
      depth: 1,
      predicate: 'works_at',
      limit: 999,
      dormant: true,
      as_of: '2026-11-05',
    }),
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.items.length, 1)
  assert.equal(out.body.items[0].predicate, 'works_at')
  assert.equal(out.body.depth, 1)
  assert.deepEqual(callPmem.argsOf('net')[0], [
    'net', 'query', '小王', '--dst', '字节', '--predicate', 'works_at',
    '--depth', '1', '--limit', '100', '--dormant', '--as-of', '2026-11-05',
  ])

  const onlyDst = makeConsole({ rules: [{ args: ['net', 'query'], reply: { ok: true, data: { items: [] } } }] })
  const dstOut = await callRoute(onlyDst.routes, PATHS.netQuery, {
    method: 'POST',
    body: JSON.stringify({ dst: '字节' }),
  })
  assert.equal(dstOut.status, 200)
  assert.deepEqual(onlyDst.callPmem.argsOf('net')[0], ['net', 'query', '--dst', '字节', '--depth', '2', '--limit', '30'])

  const noRef = await callRoute(routes, PATHS.netQuery, { method: 'POST', body: '{}' })
  assert.equal(noRef.status, 400)
  const badDepth = await callRoute(routes, PATHS.netQuery, {
    method: 'POST', body: JSON.stringify({ src: '小王', depth: 3 }),
  })
  assert.equal(badDepth.status, 400)
  const badSrc = await callRoute(routes, PATHS.netQuery, {
    method: 'POST', body: JSON.stringify({ src: '-rf' }),
  })
  assert.equal(badSrc.status, 400)
  const badDormant = await callRoute(routes, PATHS.netQuery, {
    method: 'POST', body: JSON.stringify({ src: '小王', dormant: 'yes' }),
  })
  assert.equal(badDormant.status, 400)
  const badAsOf = await callRoute(routes, PATHS.netQuery, {
    method: 'POST', body: JSON.stringify({ src: '小王', as_of: '昨天' }),
  })
  assert.equal(badAsOf.status, 400)
  const getWrite = await callRoute(routes, PATHS.netQuery)
  assert.equal(getWrite.status, 405)
}

// net query 的 E_CONFLICT 要带上 candidates
{
  const { routes } = makeConsole({
    rules: [{
      args: ['net', 'query'],
      reply: {
        ok: false,
        error: {
          code: 'E_CONFLICT',
          message: '实体名 小王 对应 2 个身份',
          field: 'src',
          details: { candidates: [{ id: 'e1' }, { id: 'e2' }] },
        },
      },
    }],
  })
  const out = await callRoute(routes, PATHS.netQuery, {
    method: 'POST', body: JSON.stringify({ src: '小王' }),
  })
  assert.equal(out.status, 409)
  assert.equal(out.body.code, 'E_CONFLICT')
  assert.equal(out.body.details.candidates.length, 2)
}

// 合并：dry_run 只预览，不调 net merge
{
  const keep = entityRow({ id: 'e1', name: '小王' })
  const drop = entityRow({ id: 'e2', name: '小王(2)' })
  const between = edgeOf(
    { id: 'e1', name: '小王', type: 'person' },
    { id: 'e2', name: '小王(2)', type: 'person' },
    'alias_of',
    'rel-between',
  )
  const reader = {
    recent: () => [],
    counts: () => null,
    entityByIdOrName(ref) {
      if (ref === 'e1') return { entity: keep, counts: { aliases: 1, mentions: 2, relations_out: 1, relations_in: 0 } }
      if (ref === 'e2') return { entity: drop, counts: { aliases: 0, mentions: 1, relations_out: 0, relations_in: 1 } }
      return null
    },
  }
  const dry = makeConsole({
    reader,
    rules: [{ args: ['net', 'query'], reply: { ok: true, data: { items: [between] } } }],
  })
  const preview = await callRoute(dry.routes, PATHS.netMerge, {
    method: 'POST',
    body: JSON.stringify({ left: 'e1', right: 'e2', dry_run: true }),
  })
  assert.equal(preview.status, 200, JSON.stringify(preview.body))
  assert.equal(preview.body.dry_run, true)
  assert.equal(preview.body.keep.id, 'e1')
  assert.equal(preview.body.drop.id, 'e2')
  assert.equal(preview.body.keep_counts.mentions, 2)
  assert.equal(preview.body.relations_between.length, 1)
  assert.equal(dry.callPmem.argsOf('net').filter(args => args[1] === 'merge').length, 0)

  // 真合并
  const real = makeConsole({
    rules: [{
      args: ['net', 'merge'],
      reply: { ok: true, data: { keep: 'e1', merged: 'e2', aliases: 1, mentions: 3, relations: 2, actor: 'system:net' } },
    }],
  })
  const merged = await callRoute(real.routes, PATHS.netMerge, {
    method: 'POST',
    body: JSON.stringify({ left: 'e1', right: 'e2' }),
  })
  assert.equal(merged.status, 200, JSON.stringify(merged.body))
  assert.equal(merged.body.dry_run, false)
  assert.equal(merged.body.keep, 'e1')
  assert.equal(merged.body.mentions, 3)
  assert.deepEqual(real.callPmem.argsOf('net')[0], ['net', 'merge', 'e1', 'e2'])

  // 同名冲突：409 且不猜
  const ambiguous = makeConsole({
    reader: {
      recent: () => [],
      counts: () => null,
      entityByIdOrName: () => ({ conflict: [entityRow({ id: 'e1' }), entityRow({ id: 'e2' })] }),
    },
  })
  const conflict = await callRoute(ambiguous.routes, PATHS.netMerge, {
    method: 'POST',
    body: JSON.stringify({ left: '小王', right: '小王(2)', dry_run: true }),
  })
  assert.equal(conflict.status, 409)
  assert.equal(conflict.body.details.candidates.length, 2)

  // CLI 合并冲突（真实 merge 路径）
  const cliConflict = makeConsole({
    rules: [{
      args: ['net', 'merge'],
      reply: {
        ok: false,
        error: { code: 'E_CONFLICT', message: '同名多身份', details: { candidates: [{ id: 'e1' }] } },
      },
    }],
  })
  const cliOut = await callRoute(cliConflict.routes, PATHS.netMerge, {
    method: 'POST', body: JSON.stringify({ left: 'e1', right: 'e2' }),
  })
  assert.equal(cliOut.status, 409)
  assert.equal(cliOut.body.details.candidates.length, 1)

  const same = await callRoute(real.routes, PATHS.netMerge, {
    method: 'POST', body: JSON.stringify({ left: 'e1', right: 'e1' }),
  })
  assert.equal(same.status, 400)
  const badRef = await callRoute(real.routes, PATHS.netMerge, {
    method: 'POST', body: JSON.stringify({ left: '-rf', right: 'e2' }),
  })
  assert.equal(badRef.status, 400)
  const badDry = await callRoute(real.routes, PATHS.netMerge, {
    method: 'POST', body: JSON.stringify({ left: 'e1', right: 'e2', dry_run: 'yes' }),
  })
  assert.equal(badDry.status, 400)
  const getWrite = await callRoute(real.routes, PATHS.netMerge)
  assert.equal(getWrite.status, 405)
}

// ---------------------------------------------------------------------------
// 9. P1：备份列表 / 生成 / 下载
// ---------------------------------------------------------------------------

const backupDir = mkdtempSync(join(tmpdir(), 'pmem-console-backup-'))
const oldBackup = join(backupDir, 'pmem-backup-20260101-000000-000-OLD.db')
const newBackup = join(backupDir, 'pmem-backup-20260202-000000-000-NEW.db')
writeFileSync(oldBackup, 'SQLite format 3 old-content')
writeFileSync(newBackup, 'SQLite format 3 new-content')
utimesSync(oldBackup, new Date('2026-01-01T00:00:00Z'), new Date('2026-01-01T00:00:00Z'))
utimesSync(newBackup, new Date('2026-02-02T00:00:00Z'), new Date('2026-02-02T00:00:00Z'))

{
  const { routes } = makeConsole({
    config: { backupDir },
    rules: [{
      args: ['backup', 'list'],
      reply: {
        ok: true,
        data: {
          items: [{
            id: 'snap-2',
            path: newBackup,
            kind: 'manual',
            memory_count: 4,
            sha256: 'abc123',
            verified_at: '2026-02-02T00:00:01Z',
          }],
          counts: { manual: 1, total: 1 },
        },
      },
    }],
  })
  const out = await callRoute(routes, PATHS.backup)
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.dir, resolve(backupDir))
  assert.equal(out.body.exists, true)
  assert.equal(out.body.counts.total, 2)
  assert.equal(out.body.counts.total_bytes, Buffer.byteLength('SQLite format 3 old-content') + Buffer.byteLength('SQLite format 3 new-content'))
  assert.equal(out.body.items[0].name, 'pmem-backup-20260202-000000-000-NEW.db')
  assert.equal(out.body.items[0].kind, 'manual')
  assert.equal(out.body.items[0].memory_count, 4)
  assert.equal(out.body.items[1].name, 'pmem-backup-20260101-000000-000-OLD.db')
  assert.equal(out.body.items[1].kind, null)
  assert.equal(out.body.degraded, undefined)

  // 目录不存在：空列表，不报错
  const empty = makeConsole({ config: { backupDir: join(backupDir, 'missing') }, reader: null })
  const none = await callRoute(empty.routes, PATHS.backup)
  assert.equal(none.status, 200)
  assert.equal(none.body.exists, false)
  assert.deepEqual(none.body.items, [])

  // backup list 失败：目录照样列
  const partial = makeConsole({
    config: { backupDir },
    reader: null,
    rules: [{ args: ['backup', 'list'], reply: cliError('E_SPAWN', '没有 python') }],
  })
  const ls = await callRoute(partial.routes, PATHS.backup)
  assert.equal(ls.status, 200)
  assert.equal(ls.body.items.length, 2)
  assert.ok(ls.body.degraded.includes('backup-list'))
}

// 生成备份：POST /backup
{
  const createdName = 'pmem-backup-20261105-210000-000-NEWER.db'
  const { routes, callPmem } = makeConsole({
    config: { backupDir },
    rules: [{
      args: ['backup', 'run'],
      reply: {
        ok: true,
        data: {
          ok: true,
          path: join(backupDir, createdName),
          size_bytes: 4096,
          sha256: 'deadbeef',
          memory_count: 7,
          verified: true,
          kind: 'manual',
          snapshot_id: 'snap-3',
          duration_ms: 42,
          counts: { memory: 7 },
        },
      },
    }],
  })
  const out = await callRoute(routes, PATHS.backup, {
    method: 'POST',
    body: JSON.stringify({ note: '手动备份一下' }),
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.file.name, createdName)
  assert.equal(out.body.file.size_bytes, 4096)
  assert.equal(out.body.file.verified, true)
  assert.equal(out.body.note, '手动备份一下')
  assert.deepEqual(callPmem.argsOf('backup')[0], ['backup', 'run', '--kind', 'manual'])

  const daily = await callRoute(routes, PATHS.backup, {
    method: 'POST', body: JSON.stringify({ kind: 'daily' }),
  })
  assert.equal(daily.status, 200)
  assert.deepEqual(callPmem.argsOf('backup').at(-1), ['backup', 'run', '--kind', 'daily'])
  const badKind = await callRoute(routes, PATHS.backup, {
    method: 'POST', body: JSON.stringify({ kind: 'weekly' }),
  })
  assert.equal(badKind.status, 400)

  const denied = makeConsole({
    config: { backupDir },
    rules: [{ args: ['backup', 'run'], reply: cliError('E_DENIED', '维护窗口内不备份') }],
  })
  const deniedOut = await callRoute(denied.routes, PATHS.backup, { method: 'POST', body: '{}' })
  assert.equal(deniedOut.status, 403)
  const getWrite = await callRoute(routes, PATHS.backup)
  assert.equal(getWrite.status, 200, 'GET /backup 本身是读路由')
}

// 下载备份：只允许 basename
{
  const { routes } = makeConsole({ config: { backupDir }, reader: null })
  const out = await callRoute(routes, PATHS.backupFile, {
    url: `${PATHS.backupFile}?name=${encodeURIComponent('pmem-backup-20260202-000000-000-NEW.db')}`,
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.headers['content-type'], 'application/octet-stream')
  assert.match(out.headers['content-disposition'], /^attachment; filename="/)
  assert.match(out.headers['content-disposition'], /NEW\.db/)
  assert.equal(out.bytes.toString('utf8'), 'SQLite format 3 new-content')

  const badNames = [
    '../pmem.db',
    '..\\\\pmem.db',
    '/etc/passwd',
    'sub/pmem.db',
    'missing.db',
    'notes.txt',
    '',
  ]
  for (const name of badNames) {
    const bad = await callRoute(routes, PATHS.backupFile, {
      url: `${PATHS.backupFile}?name=${encodeURIComponent(name)}`,
    })
    assert.ok([400, 404].includes(bad.status), `非法文件名 ${JSON.stringify(name)} 返回 ${bad.status}`)
  }
  const noName = await callRoute(routes, PATHS.backupFile)
  assert.equal(noName.status, 400)
  const postRead = await callRoute(routes, PATHS.backupFile, {
    method: 'POST', body: '{}', headers: { 'content-type': 'application/json' },
  })
  assert.equal(postRead.status, 405)
}

// ---------------------------------------------------------------------------
// 10. P1：向量重建 / 批量操作 / 运维白名单
// ---------------------------------------------------------------------------

{
  const { routes, callPmem } = makeConsole({
    rules: [
      { args: ['vector', 'status'], reply: { ok: true, data: { enabled: true, ready: true, vectors: 3, model: 'bge' } } },
      { args: ['vector', 'rebuild'], reply: { ok: true, data: { vectors: 3, rebuilt: 3 } } },
    ],
  })
  const preview = await callRoute(routes, PATHS.vectorRebuild, {
    method: 'POST', body: JSON.stringify({ dry_run: true }),
  })
  assert.equal(preview.status, 200, JSON.stringify(preview.body))
  assert.equal(preview.body.dry_run, true)
  assert.equal(preview.body.status.vectors, 3)
  assert.equal(callPmem.argsOf('vector').some(args => args[1] === 'rebuild'), false, 'dry_run 不该真的重建')

  const real = await callRoute(routes, PATHS.vectorRebuild, {
    method: 'POST', body: '{}',
  })
  assert.equal(real.status, 200)
  assert.equal(real.body.dry_run, false)
  assert.equal(real.body.result.vectors, 3)
  assert.deepEqual(callPmem.argsOf('vector').at(-1), ['vector', 'rebuild'])

  const badDry = await callRoute(routes, PATHS.vectorRebuild, {
    method: 'POST', body: JSON.stringify({ dry_run: 'yes' }),
  })
  assert.equal(badDry.status, 400)
  const getWrite = await callRoute(routes, PATHS.vectorRebuild)
  assert.equal(getWrite.status, 405)
}

// 批量：feedback / retract / delete，逐个执行、部分失败照实报
{
  const { routes, callPmem } = makeConsole({
    rules: [
      { args: ['feedback'], reply: { ok: true, data: { id: 'm1' } } },
      { args: ['retract'], reply: { ok: true, data: { id: 'm1' } } },
      { args: ['delete'], reply: { ok: true, data: { deleted: 1 } } },
    ],
  })
  const feedback = await callRoute(routes, PATHS.batch, {
    method: 'POST',
    body: JSON.stringify({ ids: ['m1', 'm2'], op: 'feedback', signal: 'useful', note: '批量有用' }),
  })
  assert.equal(feedback.status, 200, JSON.stringify(feedback.body))
  assert.equal(feedback.body.ok, true)
  assert.equal(feedback.body.okCount, 2)
  assert.equal(feedback.body.failCount, 0)
  assert.equal(callPmem.argsOf('feedback').length, 2)
  assert.deepEqual(callPmem.argsOf('feedback')[0], ['feedback', 'm1', 'useful', '--note', '批量有用'])

  const retract = await callRoute(routes, PATHS.batch, {
    method: 'POST',
    body: JSON.stringify({ ids: ['m1'], op: 'retract', note: '撤回' }),
  })
  assert.equal(retract.status, 200)
  assert.deepEqual(callPmem.argsOf('retract')[0], ['retract', 'm1', '--reason', '撤回'])

  const soft = await callRoute(routes, PATHS.batch, {
    method: 'POST', body: JSON.stringify({ ids: ['m1'], op: 'delete' }),
  })
  assert.equal(soft.status, 200)
  assert.equal(soft.body.hard, false)
  assert.deepEqual(callPmem.argsOf('delete')[0], ['delete', 'm1'])

  const hard = await callRoute(routes, PATHS.batch, {
    method: 'POST', body: JSON.stringify({ ids: ['m2'], op: 'delete', hard: true }),
  })
  assert.equal(hard.status, 200)
  assert.equal(hard.body.hard, true)
  assert.deepEqual(callPmem.argsOf('delete').at(-1), ['delete', 'm2', '--hard'])

  // 部分失败
  const partial = makeConsole({
    rules: [{
      args: ['feedback'],
      reply: args => (args[1] === 'm2'
        ? cliError('E_NOT_FOUND', '没有这条记忆')
        : { ok: true, data: {} }),
    }],
  })
  const mixed = await callRoute(partial.routes, PATHS.batch, {
    method: 'POST',
    body: JSON.stringify({ ids: ['m1', 'm2'], op: 'feedback', signal: 'useful' }),
  })
  assert.equal(mixed.status, 200)
  assert.equal(mixed.body.ok, false)
  assert.equal(mixed.body.okCount, 1)
  assert.equal(mixed.body.failCount, 1)
  assert.equal(mixed.body.results[1].id, 'm2')
  assert.equal(mixed.body.results[1].error.code, 'E_NOT_FOUND')

  // 参数校验
  const cases = [
    [{ ids: [], op: 'feedback', signal: 'useful' }, 400],
    [{ ids: 'm1', op: 'feedback', signal: 'useful' }, 400],
    [{ ids: Array.from({ length: BATCH_MAX_IDS + 1 }, (_, index) => `m${index}`), op: 'delete' }, 400],
    [{ ids: ['m1'], op: 'update' }, 400],
    [{ ids: ['m1'], op: 'feedback' }, 400],
    [{ ids: ['m1'], op: 'feedback', signal: 'like' }, 400],
    [{ ids: ['m1'], op: 'delete', hard: 'yes' }, 400],
    [{ ids: ['bad id'], op: 'delete' }, 400],
  ]
  for (const [payload, expected] of cases) {
    const out = await callRoute(routes, PATHS.batch, { method: 'POST', body: JSON.stringify(payload) })
    assert.equal(out.status, expected, `batch 参数 ${JSON.stringify(payload)} 返回 ${out.status}`)
  }
  const getWrite = await callRoute(routes, PATHS.batch)
  assert.equal(getWrite.status, 405)
}

// 批量 update：patch 白名单 + 逐条执行 + 部分成功如实报
{
  const { routes, callPmem } = makeConsole({
    rules: [
      {
        args: ['update'],
        reply: args => (args[1] === 'm2'
          ? cliError('E_NOT_FOUND', '没有这条记忆')
          : { ok: true, data: {} }),
      },
      { args: ['group', 'assign'], reply: { ok: true, data: {} } },
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
    ],
  })
  const out = await callRoute(routes, PATHS.batch, {
    method: 'POST',
    body: JSON.stringify({ ids: ['m1', 'm2'], op: 'update', patch: { kind: 'semantic', grade: 'A' } }),
  })
  assert.equal(out.status, 200, JSON.stringify(out.body))
  assert.equal(out.body.op, 'update')
  assert.equal(out.body.ok, false)
  assert.equal(out.body.okCount, 1)
  assert.equal(out.body.failCount, 1)
  assert.deepEqual(out.body.applied_fields, ['kind', 'grade'])
  assert.deepEqual(out.body.results[0].applied, { kind: 'semantic', grade: 'A' })
  assert.equal(out.body.results[1].ok, false)
  // m2 的 kind 失败，但 grade 走 group assign 成功：部分成功要保留
  assert.deepEqual(out.body.results[1].applied, { grade: 'A' })
  assert.equal(out.body.results[1].failed[0].field, 'kind')

  // 全绿
  const allOk = makeConsole({
    rules: [
      { args: ['update'], reply: { ok: true, data: {} } },
      { args: ['group', 'assign'], reply: { ok: true, data: {} } },
      { args: ['get'], reply: { ok: true, data: GET_ITEM } },
    ],
  })
  const done = await callRoute(allOk.routes, PATHS.batch, {
    method: 'POST',
    body: JSON.stringify({ ids: ['m1', 'm3'], op: 'update', patch: { status: 'dormant', importance: 0.4 } }),
  })
  assert.equal(done.status, 200)
  assert.equal(done.body.ok, true)
  assert.equal(done.body.okCount, 2)
  assert.deepEqual(done.body.applied_fields, ['status', 'importance'])
  assert.deepEqual(done.body.results[0].applied, { status: 'dormant', importance: 0.4 })

  // patch 校验：空 / 非法字段 / 非法枚举
  const invalidCases = [
    { ids: ['m1'], op: 'update' },
    { ids: ['m1'], op: 'update', patch: {} },
    { ids: ['m1'], op: 'update', patch: { foo: 1 } },
    { ids: ['m1'], op: 'update', patch: { pinned: true } },
    { ids: ['m1'], op: 'update', patch: { kind: 'book' } },
    { ids: ['m1'], op: 'update', patch: { status: 'deleted' } },
    { ids: ['m1'], op: 'update', patch: { grade: 'Z' } },
  ]
  for (const payload of invalidCases) {
    const bad = await callRoute(routes, PATHS.batch, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    assert.equal(bad.status, 400, `batch update ${JSON.stringify(payload)} 返回 ${bad.status}`)
  }
  // 参数校验失败时不应该已经动过任何一条
  const updateCallsBefore = callPmem.argsOf('update').length
  await callRoute(routes, PATHS.batch, {
    method: 'POST',
    body: JSON.stringify({ ids: ['m1'], op: 'update', patch: { kind: 'book' } }),
  })
  assert.equal(callPmem.argsOf('update').length, updateCallsBefore, '参数校验失败却已经写了库')
}

// 运维白名单：只认四个名字
{
  const { routes, callPmem } = makeConsole({
    rules: [
      { args: ['doctor'], reply: { ok: true, data: { integrity: true } } },
      { args: ['stats'], reply: { ok: true, data: { db_size_bytes: 1000, db_path: 'x' } } },
      { args: ['vacuum'], reply: { ok: true, data: { before_bytes: 1000, after_bytes: 800 } } },
      { args: ['evolve', 'run'], reply: { ok: true, data: { layers: ['patterns'] } } },
      { args: ['net', 'stats'], reply: { ok: true, data: { entities: 2, relations: 1 } } },
      { args: ['net', 'extract'], reply: { ok: true, data: { memories: 3, relations: 2 } } },
    ],
  })
  assert.ok(ACTION_NAMES.includes('doctor'))

  const doctor = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'doctor' }),
  })
  assert.equal(doctor.status, 200, JSON.stringify(doctor.body))
  assert.equal(doctor.body.result.integrity, true)

  const vacuumDry = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'vacuum', dry_run: true }),
  })
  assert.equal(vacuumDry.status, 200)
  assert.equal(vacuumDry.body.dry_run, true)
  assert.deepEqual(vacuumDry.body.planned, ['vacuum'])
  assert.equal(vacuumDry.body.before.db_size_bytes, 1000)
  assert.equal(callPmem.argsOf('vacuum').length, 0, 'dry_run 不该真的 VACUUM')

  const vacuum = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'vacuum', analyze: true }),
  })
  assert.equal(vacuum.status, 200)
  assert.deepEqual(callPmem.argsOf('vacuum').at(-1), ['vacuum', '--analyze'])

  const evolve = await callRoute(routes, PATHS.action, {
    method: 'POST',
    body: JSON.stringify({ name: 'evolve', dry_run: true, layer: ['patterns', 'tuning'] }),
  })
  assert.equal(evolve.status, 200, JSON.stringify(evolve.body))
  assert.deepEqual(evolve.body.layers, ['patterns', 'tuning'])
  assert.deepEqual(callPmem.argsOf('evolve').at(-1), [
    'evolve', 'run', '--layer', 'patterns', '--layer', 'tuning', '--dry-run',
  ])
  const badLayer = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'evolve', layer: ['shell'] }),
  })
  assert.equal(badLayer.status, 400)

  const extractDry = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'net-extract', dry_run: true }),
  })
  assert.equal(extractDry.status, 200)
  assert.deepEqual(extractDry.body.planned, ['net', 'extract'])
  assert.equal(extractDry.body.stats.entities, 2)

  const extract = await callRoute(routes, PATHS.action, {
    method: 'POST',
    body: JSON.stringify({ name: 'net-extract', limit: 10, reset: true }),
  })
  assert.equal(extract.status, 200)
  assert.deepEqual(callPmem.argsOf('net').at(-1), ['net', 'extract', '--limit', '10', '--reset'])

  const badName = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'shell' }),
  })
  assert.equal(badName.status, 400)
  assert.match(badName.body.error, /doctor/)
  const dashName = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: '-rf' }),
  })
  assert.equal(dashName.status, 400)
  const badLimit = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'net-extract', limit: 0 }),
  })
  assert.equal(badLimit.status, 400)
  const badDry = await callRoute(routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'vacuum', dry_run: 'yes' }),
  })
  assert.equal(badDry.status, 400)
  const getWrite = await callRoute(routes, PATHS.action)
  assert.equal(getWrite.status, 405)

  const denied = makeConsole({
    rules: [{ args: ['doctor'], reply: cliError('E_DENIED', '维护窗口') }],
  })
  const deniedOut = await callRoute(denied.routes, PATHS.action, {
    method: 'POST', body: JSON.stringify({ name: 'doctor' }),
  })
  assert.equal(deniedOut.status, 403)
}

rmSync(backupDir, { recursive: true, force: true })

console.log('dsh-pmem console ok')
