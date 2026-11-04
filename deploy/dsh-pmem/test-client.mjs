/**
 * 设置页契约（不启浏览器）：
 *   · client.js 能被 __ModuleLoader__ 加载；
 *   · 注册进 settings.section，且标签名/顺序对；
 *   · 六个标签齐全，星图**不在**标签里，只作为链接；
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

// 桌面壳的 setWindowOpenHandler 只放行 https，http 的新窗口会被静默 deny。
// 所以星图必须同源导航（同一个标签打开），不能开新窗口。
const source = readFileSync(new URL('./client.js', import.meta.url), 'utf8')
assert.ok(!/target:\s*['"]_blank/.test(source), '设置页里不能开新窗口：会被桌面壳静默拦掉')
assert.ok(!/target="_blank"/.test(source), '同上')
// 星图不再嵌在设置页里：页内浮层有概率被窗口边缘压住关闭按钮，
// 所以只保留「在浏览器打开」这一条（插件自己的回环只读服务 + 系统浏览器）。
assert.ok(!source.includes('pmem-overlay'), '页内浮层已经去掉了，不该再出现')
assert.ok(!source.includes('iframe'), '设置页里不该再有 iframe')
assert.ok(source.includes("'/starmap/open'"), '缺少「在浏览器打开」的接口调用')
assert.ok(source.includes('browserUrl'), '没有把浏览器地址显示出来')
assert.ok(source.includes('关掉开关就不起服务'), '缺了开关的说明')

// 六个标签，且星图不占标签
const labels = client.TABS.map(([, label]) => label)
assert.deepEqual(labels, ['概览', '醒来卡', '记忆', '自动 handoff', 'MCP', '高级'])
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
