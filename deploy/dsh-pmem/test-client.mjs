/**
 * 设置页契约（不启浏览器）：
 *   · client.js 能被 __ModuleLoader__ 加载；
 *   · 注册进 settings.section，且标签名/顺序对；
 *   · 七个标签齐全（含末尾「夜间」），星图**不在**标签里，只作为链接；
 *   · 组件在假 React 下能被调用一次，不炸。
 *
 * 运行：node test-client.mjs
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const idle = () => {}
const React = {
  createElement: () => null,
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
      const module = { exports: {} }
      client = factory(name => (name === 'react' ? React : {}))
    },
  },
}

await import('./client.js')
assert.ok(client, 'client.js 没有被 ModuleLoader 加载')
assert.equal(typeof client.apply, 'function', '没有导出 apply')
assert.ok(Array.isArray(client.inject), 'inject 不是数组')
assert.deepEqual(client.inject, ['slots'], 'inject 不对')
assert.equal(client.API, '/dsh-pmem/api')
assert.equal(client.STARMAP, '/dsh-pmem/starmap', '星图链接不对')

// 桌面壳的主窗口 setWindowOpenHandler（app.asar main.js）只把 http/https 交给系统浏览器，
// **从不新建 Electron 窗口**；页面 origin 是 dsh-app://app/，相对路径 window.open('/…')
// 会被解析成 dsh-app://…（非 http/https）→ 静默 deny，0 请求，看起来就是「点了没反应」。
// 而插件路由在带签名的会话 cookie 后面，系统浏览器没有这个 cookie → 拿到 401。
// 结论：设置页里既不能开新窗口，也不能靠整页导航；只有页内同源 fetch 才有凭据。
const source = readFileSync(new URL('./client.js', import.meta.url), 'utf8')
assert.ok(!/target:\s*['"]_blank/.test(source), '设置页里不能开新窗口：桌面壳不会为插件建 Electron 窗口')
assert.ok(!/target="_blank"/.test(source), '同上')
assert.ok(!source.includes('_blank'), '不该再出现 _blank')
assert.ok(!source.includes('target='), '不该再出现 target=')
// 夜间流水已经改成设置面板里的第 7 个标签：同源 fetch，不开新窗口。
assert.ok(!/window\.open\(\s*['"`]\/dsh-pmem\/night/.test(source), '夜间页不能开新窗口')
assert.ok(!source.includes("window.open('/dsh-pmem/night'"), '夜间页不能开新窗口')
// 别只挡一种写法：拼接 URL、无 _blank 的 window.open、整页导航都是同一个坑。
assert.ok(!source.includes('window.open'), '设置页里不该用 window.open：桌面壳会静默 deny，用户看到的就是「点了没反应」')
assert.ok(!/location\.(href|assign)\s*=\s*['"`][^'"`]*\/dsh-pmem/.test(source), '不能靠整页导航打开插件页面（会离开设置面板）')
assert.ok(source.includes("['night', '夜间']"), '缺少「夜间」标签')
assert.ok(source.includes("setTab('night')"), '概览的「夜间页」按钮应该切到「夜间」标签')
assert.ok(source.includes("'/sleep/now'"), '夜间标签缺少「现在入睡」的 /sleep/now 调用')
assert.ok(source.includes("'/sleep'"), '夜间标签缺少 /sleep 列表数据源')
// 夜间标签的第二课：内容要把人话摆在前面，高度要收在设置弹窗里。
// 直接把 stats JSON 甩上去 = 花括号天书；卡片不折叠 + max-height 100vh = 标签栏被滚出可视区、切不回别的页。
assert.ok(!/JSON\.stringify\(\s*stage\.stats/.test(source), '阶段摘要不能直接甩 JSON')
assert.ok(source.includes('pmem-run-toggle'), '睡眠卡片要能点开/收起')
assert.ok(source.includes('aria-expanded'), '折叠按钮要暴露展开状态')
assert.ok(/max-height:\s*min\(\s*700px/.test(source), '面板高度要收在设置弹窗里（否则标签栏会被滚出去）')
assert.ok(source.includes('stageSummary'), '阶段要有人话摘要')
// 星图不再嵌在设置页里：页内浮层有概率被窗口边缘压住关闭按钮，
// 所以只保留「在浏览器打开」这一条（插件自己的回环只读服务 + 系统浏览器）。
assert.ok(!source.includes('pmem-overlay'), '页内浮层已经去掉了，不该再出现')
assert.ok(!source.includes('iframe'), '设置页里不该再有 iframe')
assert.ok(source.includes("'/starmap/open'"), '缺少「在浏览器打开」的接口调用')
assert.ok(source.includes('browserUrl'), '没有把浏览器地址显示出来')
assert.ok(source.includes('关掉开关就不起服务'), '缺了开关的说明')

// 七个标签，且星图不占标签
const labels = client.TABS.map(([, label]) => label)
assert.deepEqual(labels, ['概览', '醒来卡', '记忆', '自动 handoff', 'MCP', '高级', '夜间'])
assert.ok(!labels.some(label => label.includes('星图')), '星图不应该占设置标签页')

// 注册契约
const registered = []
const ctx = {
  effect(fn) { fn() },
  slots: {
    inject(name, factory) {
      assert.equal(name, 'settings.section')
      return factory()
    },
    register(descriptor, Component) {
      registered.push({ descriptor, Component })
      return () => {}
    },
  },
}
client.apply(ctx)
assert.equal(registered.length, 1, '没有注册 settings.section')
const [{ descriptor, Component }] = registered
assert.equal(descriptor.name, 'settings.section')
assert.equal(descriptor.id, 'dsh-pmem')
assert.equal(typeof descriptor.order, 'number')
assert.equal(descriptor.label(), '可塑性记忆')
assert.equal(typeof Component, 'function', '组件不是函数')

// 组件在假 React 下跑一次：只验证"没炸 + 没有在渲染期发请求"
const rendered = Component({})
assert.ok(!rendered, '假 React 应该返回 null')

console.log('dsh-pmem client ok')
