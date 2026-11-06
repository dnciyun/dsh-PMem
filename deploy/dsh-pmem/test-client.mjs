/**
 * 客户端契约（不启浏览器）：
 *   · client.js 能被 __ModuleLoader__ 加载，inject 含 slots + layout；
 *   · 三个 slot：设置页一行入口（带宿主 close）/ 侧边栏海马图标 / root main keyed slot；
 *   · 控制台 6 个分区（总览/醒来卡/记忆/实体/睡眠/设置），没有问答、没有键盘快捷键；
 *   · 顶栏状态灯、记忆排序/加载更多/分组树、实体真路由、危险区真路由都在；
 *   · 不开新窗口、不整页导航、不用 innerHTML、不引 UI primitives；
 *   · 组件在假 React（记录节点）+ mock fetch 下各渲染一次不炸，渲染期不发请求。
 *
 * 运行：node test-client.mjs
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const idle = () => {}

const React = {
  createElement(type, props, ...children) {
    return { type, props: props ?? {}, children }
  },
  useCallback: fn => fn,
  useEffect: idle,
  useMemo: fn => fn(),
  useRef: value => ({ current: value }),
  useState: value => [typeof value === 'function' ? value() : value, idle],
}

let client = null
globalThis.window = {
  __ModuleLoader__: {
    load({ id, factory }) {
      assert.equal(id, 'dsh-pmem', 'bundle id 不对')
      client = factory(name => (name === 'react' ? React : {}))
    },
  },
}

await import('./client.js')
assert.ok(client, 'client.js 没有被 ModuleLoader 加载')
assert.equal(typeof client.apply, 'function', '没有导出 apply')
assert.deepEqual(client.inject, ['slots', 'layout'], 'inject 必须同时包含 slots 和 layout')
assert.equal(client.NS, 'dsh-pmem')
assert.equal(client.PANEL_ID, 'pmem-console')
assert.equal(client.API, '/dsh-pmem/api')
assert.equal(client.CONSOLE, '/dsh-pmem/api/console')
assert.equal(client.STARMAP, '/dsh-pmem/starmap')
assert.deepEqual(
  client.CONSOLE_NAV.map(([, label]) => label),
  ['总览', '醒来卡', '记忆', '实体', '睡眠', '设置'],
  '控制台应该是六个分区（醒来卡不能没有页面）',
)

// ── 源码契约 ────────────────────────────────────────────────────────────────
const source = readFileSync(new URL('./client.js', import.meta.url), 'utf8')

assert.ok(!source.includes('window.open'), '不能 window.open：桌面壳会静默 deny')
assert.ok(!source.includes('_blank'), '不能 _blank')
assert.ok(!source.includes('target='), '不能 target=')
assert.ok(!/location\.(href|assign)\s*=/.test(source), '不能整页导航')
assert.ok(!source.includes('innerHTML'), '不能直接写 innerHTML')
assert.ok(!source.includes('iframe'), '不该有 iframe')
assert.ok(!source.includes('@deepseek-ai/dsh-client-ui-primitives'), '图标要内联 SVG')
assert.ok(!source.includes('/console/chats'), '产品里没有问答')
assert.ok(!source.includes('EventSource'), '没有 SSE')
assert.ok(!source.includes('onKeyDown'), '不加键盘快捷键')
assert.ok(source.includes("const PANEL_ID = 'pmem-console'"))
assert.ok(/selectPanel\??\.?\(PANEL_ID\)/.test(source), '打开控制台要走 ctx.layout.selectPanel')
assert.ok(source.includes('props?.close?.()'), '切面板后要关掉设置 dialog（宿主给的 close prop）')
assert.ok(source.includes('记忆控制台'))
// primary 按钮不能用写死的白字：深色主题里 fill 本身是近白色（真实 GUI 验收发现白块）。
for (const rule of ['.pmem-btn-primary{', '.pconsole-btn-primary{']) {
  const at = source.indexOf(rule)
  assert.ok(at >= 0, `缺少按钮规则 ${rule}`)
  const line = source.slice(at, source.indexOf('\n', at))
  assert.ok(line.includes('--dsw-alias-button-primary-fill'), `${rule} 要用宿主按钮 fill token`)
  assert.ok(line.includes('--dsw-alias-label-primary-foreground'), `${rule} 前景要用宿主前景 token`)
  assert.ok(!line.includes('color:#fff'), `${rule} 不能写死白字`)
}
assert.ok(source.includes('打开控制台'))
assert.ok(source.includes('function ConsolePage'))
// 模型通道灯：数据来自 /console/bootstrap 的 model 块；不能永远是灰
assert.ok(source.includes('boot?.model'), '状态灯要读 bootstrap.model')
assert.ok(source.includes('modelInfo.available === true'), '模型通道绿灯判定要用 available')
assert.ok(source.includes('宿主没有可用的模型通道'), 'available=false 时要有明确文案')
assert.ok(source.includes('已配置：'), 'available=true 时要显示 provider/model')
// P1 真路由
for (const endpoint of [
  "apiConsole('/entities?",
  "CONSOLE + '/entity?",
  "apiConsole('/net/query'",
  "CONSOLE + '/net/merge'",
  "apiConsole('/backup'",
  "CONSOLE + '/backup/file?name=",
  "apiConsole('/vector/rebuild'",
  "apiConsole('/memory/batch'",
  "apiConsole('/action'",
]) {
  assert.ok(source.includes(endpoint), `缺少真实接口调用：${endpoint}`)
}
// 前端行为
assert.ok(source.includes('MEMORY_SORTS'), '缺少排序分段按钮')
// task-9：真 offset/sort/since/until 分页
assert.ok(source.includes('offset: String(nextOffset)'), '检索要传 offset')
assert.ok(source.includes('setOffset('), 'offset 要按「本页条数」递增')
assert.ok(source.includes('data.offset'), '要用后端夹取后的 offset 作为下一页起点')
assert.ok(source.includes('runSearch({ offset, append: true })'), '加载更多要用 offset 翻页并追加')
assert.ok(source.includes("params.set('since'"), '日期范围要传服务端 since')
assert.ok(source.includes("params.set('until'"), '日期范围要传服务端 until')
assert.ok(source.includes('MEMORY_SORT_API'), '排序要映射成后端 sort 值')
assert.ok(source.includes("grade: 'importance'"), 'grade 要映射成后端 sort=importance')
assert.ok(source.includes('仅显示最相关的 200 条'), 'capped 时要提示 200 条池上限')
assert.ok(source.includes('hasMore'), '要用后端 hasMore 控制加载更多')
assert.ok(!source.includes('已到接口上限 50'), '旧的上限 50 提示应删掉')
assert.ok(!source.includes('sortMemories'), '前端排序函数应删掉（改用后端 sort）')
assert.ok(!source.includes('前端过滤）'), '日期范围不再是前端过滤')
assert.ok(source.includes('加载更多'), '缺少「加载更多」')
assert.ok(source.includes('分组过滤（含子组·多选）'), '缺少分组树过滤')
assert.ok(source.includes('pconsole-dot'), '缺少顶栏状态灯')
assert.ok(source.includes('今日召回'), '总览指标应该是「今日召回」')
assert.ok(source.includes('空库引导'), '空库要有引导卡')
// 批量改元数据走 op=update（后端 task-7 已交付），不再循环单条 /memory/update
assert.ok(source.includes("op: 'update'"), '批量改元数据应该走 /memory/batch op=update')
assert.ok(source.includes('ids: selectedIds'), '批量请求要带 ids')
assert.ok(!source.includes('setBatchProgress'), '批量 update 一次请求即可，不需要逐条进度 state')
// 备份 kind 必须是后端真实枚举 manual / daily / pre-migrate
assert.ok(source.includes("'pre-migrate'"), '备份 kind 要用后端的 pre-migrate')
assert.ok(!source.includes('pre-sleep'), '后端不接受 pre-sleep 这个备份 kind')
assert.ok(!source.includes('pre-evolve'), '后端不接受 pre-evolve 这个备份 kind')
// 单条元数据编辑：kind / 分组一次 POST，按 applied/failed 逐字段提示
assert.ok(source.includes('kindDraft'), '单条元数据编辑缺 kind 草稿状态')
assert.ok(source.includes('groupDraft'), '单条元数据编辑缺分组草稿状态')
assert.ok(source.includes('pconsole-group-options'), '分组输入要有 datalist 建议（来自 /console/groups）')
assert.ok(source.includes('saveMetaEdits'), '缺「保存 kind / 分组」的保存函数')
assert.ok(source.includes('partialFail') || source.includes('part-fail') || source.includes('部分字段没保存成功'), '要按 applied/failed 反馈')
assert.ok(source.includes('applied'), '要读后端 applied 字段')
assert.ok(source.includes('failed'), '要读后端 failed 字段')
assert.ok(!source.includes('用「批量改元数据」走'), '已过时的 kind/分组 note 应该删掉')
// 使用统计：use_count + last_used_at 不能丢
assert.ok(source.includes("kvRow('使用次数'"), '详情缺使用次数')
assert.ok(source.includes("kvRow('最近使用'"), '详情缺最近使用时间')
for (const legacy of ['自动 handoff', "['night', '夜间']", 'pmem-tabs']) {
  assert.ok(!source.includes(legacy), `旧设置页残留：${legacy}`)
}

// ── 注册契约 ────────────────────────────────────────────────────────────────
const registered = []
const injected = []
const panelCalls = []
const closeCalls = []
const ctx = {
  effect(fn) {
    fn()
    return () => {}
  },
  slots: {
    inject(name, factory) {
      injected.push(name)
      return factory ? factory() : () => {}
    },
    register(descriptor, Component) {
      registered.push({ descriptor, Component })
      return () => {}
    },
  },
  layout: {
    selectPanel(id) {
      panelCalls.push(id)
    },
  },
}
client.apply(ctx)

assert.deepEqual([...injected].sort(), ['main', 'settings.section', 'sidebar.panellist'])
assert.equal(registered.length, 3)
const byName = new Map(registered.map(entry => [entry.descriptor.name, entry]))

const settings = byName.get('settings.section')
assert.ok(settings)
assert.equal(settings.descriptor.id, 'dsh-pmem')
assert.equal(settings.descriptor.label(), '可塑性记忆')

const sidebar = byName.get('sidebar.panellist')
assert.ok(sidebar)
assert.equal(sidebar.descriptor.id, 'pmem-console')
assert.equal(sidebar.descriptor.order, 1)
assert.equal(sidebar.descriptor.label(), '记忆控制台')

const main = byName.get('main')
assert.ok(main)
assert.equal(main.descriptor.key, 'pmem-console')
assert.equal(typeof main.Component, 'function')

// ── 元素树工具 ──────────────────────────────────────────────────────────────
function flatten(node) {
  const out = []
  const visit = value => {
    if (value === null || value === undefined || typeof value === 'boolean') return
    if (Array.isArray(value)) {
      value.forEach(visit)
      return
    }
    if (typeof value === 'object') {
      visit(value.children)
      return
    }
    out.push(String(value))
  }
  visit(node)
  return out.join('')
}

function walk(node, visit) {
  if (node === null || node === undefined || typeof node === 'boolean') return
  if (Array.isArray(node)) {
    node.forEach(child => walk(child, visit))
    return
  }
  if (typeof node !== 'object') return
  if (typeof node.type === 'function') {
    const props = { ...node.props }
    if (node.children.length === 0) {
      // keep props.children
    } else if (node.children.length === 1) {
      props.children = node.children[0]
    } else {
      props.children = node.children
    }
    walk(node.type(props), visit)
    return
  }
  visit(node)
  for (const child of node.children ?? []) walk(child, visit)
}

function findAll(root, predicate) {
  const found = []
  walk(root, node => {
    if (predicate(node)) found.push(node)
  })
  return found
}

// 文本提取要展开函数组件（MetricCard / Panel），否则 title 类文案会被漏掉。
function textOf(root) {
  const out = []
  const visit = value => {
    if (value === null || value === undefined || typeof value === 'boolean') return
    if (Array.isArray(value)) {
      value.forEach(visit)
      return
    }
    if (typeof value !== 'object') {
      out.push(String(value))
      return
    }
    if (typeof value.type === 'function') {
      const props = { ...value.props }
      if (value.children.length === 1) props.children = value.children[0]
      else if (value.children.length > 1) props.children = value.children
      visit(value.type(props))
      return
    }
    visit(value.children)
  }
  visit(root)
  return out.join('')
}
const byClass = name => node => String(node?.props?.className ?? '').split(' ').includes(name)

// ── 组件渲染（假 React + mock fetch） ────────────────────────────────────────
let fetchCalls = 0
globalThis.fetch = async () => {
  fetchCalls += 1
  return { ok: true, status: 200, json: async () => ({ ok: true }) }
}

const bootFull = {
  version: '0.8.0',
  status: { db: { memory_total: 12, integrity_ok: true, db_size_bytes: 4096, schema_version: 3 }, mcp: { tools: 27 } },
  model: { available: true, provider: 'deepseek-account', model: 'deepseek-flash', providers: ['deepseek-account'], source: 'default', note: null },
  counts: { memory_total: 12, today_new: 2, today_recalls: 7, open_conflicts: 1, uses: 30 },
  wake: { text: '卡', tokens: 100, chars: 260 },
  profile: { traits: [], open_goals: [] },
  recent: [],
  sleep: {},
  degraded: [],
}
const statusFull = {
  version: '0.8.0',
  db: { integrity_ok: true, db_size_bytes: 4096, schema_version: 3, by_status: { active: 10 } },
  mcp: { serverName: 'pmem', tools: 27 },
  tokens: { p95: 100, mean: 90, samples: 4 },
  wake: { chars: 260 },
  runtime: { spec: { budget: { type: 'int', min: 120, max: 1200, step: 20, label: '预算', unit: 'token' } } },
}

assert.doesNotThrow(() => client.ConsolePage({}), 'ConsolePage 渲染不炸')
assert.doesNotThrow(
  () => client.ConsoleOverview({ boot: bootFull, status: statusFull, loading: false, notify: idle, goto: idle, refreshedAt: new Date() }),
  'ConsoleOverview 渲染不炸',
)
assert.doesNotThrow(
  () => client.ConsoleMemory({ notify: idle, initialId: '', onConsumeInitial: idle }),
  'ConsoleMemory 渲染不炸',
)
assert.doesNotThrow(() => client.ConsoleEntities({ notify: idle, goto: idle }), 'ConsoleEntities 渲染不炸')
assert.doesNotThrow(() => client.ConsoleSleep({ notify: idle }), 'ConsoleSleep 渲染不炸')
assert.doesNotThrow(
  () => client.ConsoleWake({ boot: bootFull, status: statusFull, notify: idle, goto: idle }),
  'ConsoleWake 渲染不炸',
)
assert.doesNotThrow(
  () => client.ConsoleSettings({ boot: bootFull, notify: idle }),
  'ConsoleSettings 渲染不炸',
)
assert.equal(fetchCalls, 0, '渲染期不该发请求')

// ── 结构断言 ────────────────────────────────────────────────────────────────
const pageTree = client.ConsolePage({})
assert.ok(String(pageTree.props.className).includes('pconsole'))
const tablist = findAll(pageTree, node => node.props?.role === 'tablist')[0]
assert.ok(tablist, '缺少 tablist')
assert.equal(tablist.props.onKeyDown, undefined, '不加键盘快捷键（tablist 不要 onKeyDown）')
const tabs = findAll(pageTree, node => node.props?.role === 'tab')
assert.equal(tabs.length, 6)
assert.deepEqual(tabs.map(tab => textOf(tab.children)), ['总览', '醒来卡', '记忆', '实体', '睡眠', '设置'])
const lights = findAll(pageTree, byClass('pconsole-dot'))
assert.equal(lights.length, 3, '顶栏应该有 3 个状态灯（数据库/模型/MCP）')

// 海马图标：16px 内联 SVG，currentColor
const icon = sidebar.Component({ size: 16 })
assert.equal(icon.type, 'svg')
assert.equal(icon.props.width, 16)
assert.equal(icon.props.height, 16)
assert.equal(icon.props.stroke, 'currentColor')
assert.ok(icon.children.length >= 5, '海马轮廓至少 5 段路径')
assert.equal(sidebar.Component({}).props.width, 16)

// 设置页「打开控制台」：切面板 + 调宿主 close（真实 GUI 发现 dialog 不关）
const settingsTree = settings.Component({ close: () => closeCalls.push('close') })
const openButton = findAll(
  settingsTree,
  node => node.type === 'button' && textOf(node.children).includes('打开控制台'),
)[0]
assert.ok(openButton, '设置页缺少「打开控制台」')
panelCalls.length = 0
openButton.props.onClick()
assert.deepEqual(panelCalls, ['pmem-console'])
assert.deepEqual(closeCalls, ['close'], '打开控制台后要关掉设置 dialog')

// 记忆页：排序分段 + 分组树 + 加载更多文案
const memoryTree = client.ConsoleMemory({ notify: idle, initialId: '', onConsumeInitial: idle })
const segs = findAll(memoryTree, byClass('pconsole-seg-btn'))
assert.equal(segs.length, 4, '排序应该有四个分段按钮')
assert.deepEqual(segs.map(seg => textOf(seg.children)), ['相关度', '时间', '重要度', '使用次数'])
assert.ok(textOf(memoryTree).includes('分组过滤（含子组·多选）'))
assert.ok(source.includes('加载更多'), '记忆页要有「加载更多」（当前无结果时不渲染按钮，故查源码）')

// 实体页：真路由的表单/合并入口都在（初始无数据也不白屏）
const entityTree = client.ConsoleEntities({ notify: idle, goto: idle })
const entityText = textOf(entityTree)
assert.equal(
  findAll(entityTree, node => node.props?.placeholder === '搜实体名').length,
  1,
  '实体页缺少搜索框',
)
assert.ok(entityText.includes('还没有实体'))

// 设置页：危险区真路由按钮
const settingsFull = client.ConsoleSettings({ boot: bootFull, notify: idle })
const settingsText = textOf(settingsFull)
for (const label of ['生成备份', '重建索引', '运维动作（白名单）', '刷新列表']) {
  assert.ok(settingsText.includes(label), `危险区缺少：${label}`)
}

// 总览：今日召回 + 空库引导；不再有「待办」
const overviewText = textOf(client.ConsoleOverview({
  boot: bootFull, status: statusFull, loading: false, notify: idle, goto: idle, refreshedAt: new Date(),
}))
assert.ok(overviewText.includes('今日召回'))
assert.ok(!overviewText.includes('待办'))
const emptyText = textOf(client.ConsoleOverview({
  boot: { ...bootFull, counts: { ...bootFull.counts, memory_total: 0 } },
  status: statusFull,
  loading: false,
  notify: idle,
  goto: idle,
  refreshedAt: new Date(),
}))
assert.ok(emptyText.includes('空库引导'), '空库要有引导卡')

// 醒来卡页：能看见卡全文（之前只有「刷新醒来卡」按钮，卡本身在界面上看不见）
const wakeTree = client.ConsoleWake({ boot: bootFull, status: statusFull, notify: idle, goto: idle })
const wakePageText = textOf(wakeTree)
assert.ok(wakePageText.includes('醒来卡'), '醒来卡页要有标题')
assert.ok(wakePageText.includes('卡'), '要能看见 bootstrap.wake.text 的全文')
assert.equal(
  findAll(wakeTree, node => node.props?.['aria-label'] === '醒来卡全文').length,
  1,
  '醒来卡要有全文块（pre）',
)
assert.equal(
  findAll(wakeTree, node => node.props?.['aria-label'] === '醒来卡预算').length,
  1,
  '醒来卡要有每轮预算输入',
)
assert.ok(source.includes("api('/config'"), '改预算要写回 runtime（/config）')
const emptyWakeText = textOf(client.ConsoleWake({
  boot: { ...bootFull, wake: { text: '' } },
  status: statusFull,
  notify: idle,
  goto: idle,
}))
assert.ok(emptyWakeText.includes('还没有可唤醒的记忆'), '空卡要有说明，不能白屏')

// ── 纯函数 ──────────────────────────────────────────────────────────────────
assert.deepEqual(client.highlightParts('薄荷和薄荷糖', '薄荷'), ['', '薄荷', '和', '薄荷', '糖'])
assert.deepEqual(client.highlightParts('任意正文', ''), ['任意正文'])
const summary = client.stageSummary({ name: 's1_collect', stats: { memories: 12 } })
assert.ok(summary.includes('记忆 12 条'))
assert.ok(!summary.includes('{'))

// openConsolePanel 独立可测：缺 layout 时不炸
const direct = []
client.openConsolePanel({ layout: { selectPanel: id => direct.push(id) } })
assert.deepEqual(direct, ['pmem-console'])
assert.doesNotThrow(() => client.openConsolePanel({}))

console.log('dsh-pmem client ok')
