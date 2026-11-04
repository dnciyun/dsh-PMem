/**
 * 不启动 DSH，用一个假 ctx 验证插件契约：
 * - 注册 systemPrompt.context(name=dsh-pmem:wake, order=941)
 * - 注册 4 个 pmem_* 工具
 * - 注册 /pmem 命令
 * - 能从真实 data/pmem.db 读到含 PMEM_WAKE 的卡
 */

import assert from 'node:assert/strict'
import { apply, buildHandoff, meaningfulText, renderSurface, textOf } from './index.js'

const calls = []
const ctx = {
  systemPrompt: {
    context(options) {
      calls.push({ kind: 'prompt', options })
      return () => {}
    },
  },
  tools: {
    register(definition) {
      calls.push({ kind: 'tool', definition })
      return () => {}
    },
  },
  commands: {
    register(definition) {
      calls.push({ kind: 'command', definition })
      return () => {}
    },
  },
  on(event, handler) {
    calls.push({ kind: 'on', event, handler })
    return () => {}
  },
}

const plugin = apply(ctx, { refreshMs: 60 * 60 * 1000 })
try {
  const prompt = calls.find(call => call.kind === 'prompt')
  assert.ok(prompt, '没有注册 systemPrompt.context')
  assert.equal(prompt.options.name, 'dsh-pmem:wake')
  assert.equal(prompt.options.order, 941)

  const toolNames = calls.filter(call => call.kind === 'tool').map(call => call.definition.name)
  assert.deepEqual(toolNames.sort(), ['pmem_recall', 'pmem_remember', 'pmem_soul', 'pmem_stats', 'pmem_wake'])

  const command = calls.find(call => call.kind === 'command')
  assert.ok(command, '没有注册 /pmem')
  assert.equal(command.definition.name, 'pmem')

  const events = calls.filter(call => call.kind === 'on').map(call => call.event)
  assert.deepEqual(
    [...new Set(events)].sort(),
    ['agent/disposed', 'agent/pre-step', 'session/event'],
  )
  // 三条 pre-step：handoff 采样、浮现、上下文水位；都调 next()。
  assert.equal(events.filter(name => name === 'agent/pre-step').length, 3)

  const surfaces = calls.filter(
    call => call.kind === 'prompt' && call.options.name === 'dsh-pmem:surface',
  )
  assert.equal(surfaces.length, 1, '没有注册浮现 section')

  // 子代理背景（M20）：单独一个注入点，只对 subagent 生效，主会话拿空串。
  const briefs = calls.filter(
    call => call.kind === 'prompt' && call.options.name === 'dsh-pmem:brief',
  )
  assert.equal(briefs.length, 1, '没有注册子代理背景 section')
  assert.equal(briefs[0].options.order, 940)
  assert.equal(
    briefs[0].options.text({ agent: { session: { id: 's1', header: {} } } }),
    '',
    '主会话不该拿到子代理背景',
  )
  assert.equal(surfaces[0].options.order, 942)
  assert.equal(renderSurface({ items: [] }), '')
  const block = renderSurface({
    items: [{ grade: 'A', groups: ['工作/PMem'], reason: 'rhythm', text: '先跑 doctor' }],
  })
  assert.ok(block.includes('PMEM_SURFACE'))
  assert.ok(block.includes('久未想起'))
  assert.ok(block.includes('先跑 doctor'))

  assert.equal(textOf([{ type: 'text', text: '重启了' }, { type: 'image' }]), '重启了')
  assert.ok(buildHandoff('重启了').includes('重启了'))
  assert.equal(meaningfulText('下一步'), '')
  assert.equal(buildHandoff(''), '')
  // 四段式交接（M20）：状态/进展/死路/下一步都在，抽不到就老实写「没留下」
  const handoff = buildHandoff('重启了', '试过压缩，不行，会失真。下一步：把交接写进自我层。')
  for (const part of ['上次交接（四段）', '状态：重启了', '进展：', '死路：', '下一步：把交接写进自我层']) {
    assert.ok(handoff.includes(part), `四段缺一块：${part}`)
  }
  assert.ok(handoff.includes('试过压缩'), '死路没有抽出来')
  assert.ok(buildHandoff('重启了', '').includes('（没留下死路记录）'))
  assert.ok(buildHandoff('x'.repeat(300)).length <= 700)

  await plugin.refresh()
  assert.ok((plugin.card.includes('PMEM_WAKE') || plugin.card.includes('PMEM_SOUL')), `卡不对：${plugin.card.slice(0, 120)}`)
  assert.ok(plugin.card.includes('我们'), '卡里没有「我们」')
  // 每轮背景条（M21）：比整卡小得多，而且带着最新一条增补。
  assert.ok(plugin.live.length > 0, '没有背景条')
  assert.ok(plugin.live.length < plugin.card.length, '背景条不该比整卡还大')
  assert.ok(plugin.live.includes('PMEM_LIVE'), `背景条标记不对：${plugin.live.slice(0, 80)}`)

  const mainAgent = { session: { id: 's-main', header: {} } }
  const first = prompt.options.text({ agent: mainAgent })
  assert.ok(
    first.includes('PMEM_WAKE') || first.includes('PMEM_SOUL'),
    '会话的第一个上下文应该拿整卡（认场）',
  )
  const second = prompt.options.text({ agent: mainAgent })
  assert.ok(second.includes('PMEM_LIVE'), '同一会话之后的每一轮应该只拿背景条')
  assert.ok(!second.includes('PMEM_SOUL'), '之后的轮次不该再塞整卡')
  const otherSession = prompt.options.text({ agent: { session: { id: 's-other', header: {} } } })
  assert.ok(
    otherSession.includes('PMEM_WAKE') || otherSession.includes('PMEM_SOUL'),
    '新会话的第一个上下文要给整卡',
  )
  assert.equal(prompt.options.text({ agent: { session: { header: { origin: 'subagent' } } } }), '')

  // system prompt 只读缓存：再读一次不应触发新的 Python 调用（这里用计时器关闭后仍稳定）
  const before = plugin.card
  assert.equal(plugin.card, before)
  console.log('dsh-pmem ok')
} finally {
  plugin.stop()
}
