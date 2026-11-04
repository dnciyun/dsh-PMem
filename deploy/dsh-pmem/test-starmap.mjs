// 星图渲染器的静态体检。
//
// 不具备"真的开一个浏览器编译 shader"的能力，所以这里守的是**能被静态守住的东西**：
//   · 三份资源都在、都不是空壳；
//   · 每个 shader 都以 #version 300 es 开头（WebGL2 唯一接受的形式）；
//   · 着色器里的花括号/圆括号配平（漏一个就是全黑屏）；
//   · 资源里不能出现 </script> 或 </style>（离线单文件会把它们内联进去）；
//   · 界面层和渲染器之间的契约字段没被改名。
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createContext, runInContext } from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const dir = join(here, 'starmap')

const read = name => readFileSync(join(dir, name), 'utf8')
const space = read('space.js')
const ui = read('ui.js')
const css = read('space.css')

// ---- 文件 -----------------------------------------------------------------
for (const [name, text, min] of [
  ['space.js', space, 20000],
  ['ui.js', ui, 15000],
  ['space.css', css, 8000],
]) {
  assert.ok(text.length > min, `${name} 太短，像是被截断了`)
}
assert.ok(statSync(join(dir, 'space.js')).isFile())

// ---- 内联安全性 -----------------------------------------------------------
for (const [name, text, bad] of [
  ['space.js', space, '</script'],
  ['ui.js', ui, '</script'],
  ['space.css', css, '</style'],
]) {
  assert.ok(!text.toLowerCase().includes(bad), `${name} 里出现了 ${bad}，离线内联会提前闭合`)
}

// ---- 着色器 ---------------------------------------------------------------
// space.js 里的着色器都是 [ ... ].join('\n') 形式的字符串数组。
const blocks = [...space.matchAll(/var\s+(VS|FS)_([A-Z_]+)\s*=\s*\[([\s\S]*?)\]\.join\(/g)]
assert.ok(blocks.length >= 8, `只找到 ${blocks.length} 个着色器，可疑`)

for (const [, kind, name, body] of blocks) {
  const lines = [...body.matchAll(/'((?:[^'\\]|\\.)*)'/g)].map(m => m[1])
  assert.ok(lines.length > 3, `${kind}_${name} 没有内容`)
  assert.equal(lines[0].trim(), '#version 300 es',
    `${kind}_${name} 第一行必须是 #version 300 es，实际是 ${JSON.stringify(lines[0])}`)
  const source = lines.join('\n')
  const count = ch => source.split(ch).length - 1
  assert.equal(count('{'), count('}'), `${kind}_${name} 花括号不配平`)
  assert.equal(count('('), count(')'), `${kind}_${name} 圆括号不配平`)
  if (kind === 'FS') {
    assert.ok(source.includes('out vec4'), `${kind}_${name} 没有输出变量`)
    assert.ok(source.includes('precision'), `${kind}_${name} 没有声明精度`)
  }
  assert.ok(!source.includes('varying'), `${kind}_${name} 用了 GLSL ES 1.0 的 varying`)
  assert.ok(!source.includes('gl_FragColor'), `${kind}_${name} 用了 GLSL ES 1.0 的 gl_FragColor`)
}

// 片元着色器必须声明输出，且不能同时用旧 API。
assert.ok(space.includes('gl_PointCoord'), '星体着色器没有点精灵衰减')
assert.ok(/vec3\s+aces\(/.test(space), '缺少色调映射')
assert.ok(space.includes('uThreshold'), '缺少 bloom 亮部提取')

// ---- 渲染器 / 界面层的契约 ------------------------------------------------
for (const key of ['x3', 'y3', 'z3']) {
  assert.ok(space.includes(key), `渲染器没有读 ${key}`)
}
for (const name of ['setData', 'setCursor', 'setFilter', 'flyTo', 'resetView', 'snapshot', 'stats']) {
  assert.ok(space.includes(`${name}:`), `渲染器没有暴露 ${name}`)
}
assert.ok(ui.includes('window.PMemStar'), '界面层没有拿到渲染器')
assert.ok(ui.includes("API + '/starmap'"), '界面层没有去取星图数据')
assert.ok(!/target:\s*['"]_blank/.test(ui), '星图里不能开新窗口（桌面壳会拦）')
assert.ok(ui.includes('pmem-starmap-close'), '被嵌入时无法关闭浮层')

// ---- 来源着色：日记派生的记忆必须一眼可辨 --------------------------------
// 静态断言只能证明字符串在文件里，证明不了「上色真的不同」。
// space.js 是 IIFE 且只依赖 window，所以能在 vm 里真跑一遍 colorOf。
const sandbox = createContext({ window: {} })
runInContext(space, sandbox)
const star = sandbox.window.PMemStar
assert.ok(star && typeof star.colorOf === 'function', '渲染器没有暴露 colorOf')
assert.ok(Array.isArray(star.JOURNAL_COLOR), '渲染器没有暴露日记派生颜色')
const journalColor = star.colorOf({ type: 'memory', grade: 'A', kind: 'semantic', source_kind: 'journal' })
const plainColor = star.colorOf({ type: 'memory', grade: 'A', kind: 'semantic' })
assert.notDeepEqual(journalColor, plainColor, '日记派生的记忆和同等级普通记忆同色')
assert.deepEqual(journalColor, star.JOURNAL_COLOR, 'journal 来源没有用 JOURNAL_COLOR')
// 暖色 R > G > B，且不能撞任何等级色，否则「一眼可辨」不成立。
assert.ok(journalColor[0] > journalColor[1] && journalColor[1] > journalColor[2],
  `日记派生色不是暖色：${journalColor}`)
for (const [grade, color] of Object.entries(star.GRADE_COLOR)) {
  assert.notDeepEqual(journalColor, color, `日记派生色撞了等级色 ${grade}`)
}
// 向后兼容：老 JSON 没有 source_kind，必须照常上色而不是崩。
assert.deepEqual(star.colorOf({ type: 'memory', kind: 'episodic' }), star.KIND_COLOR.episodic,
  '没有 source_kind 的记忆上色变了')
assert.deepEqual(star.colorOf({ type: 'memory', grade: 'B' }), star.GRADE_COLOR.B,
  '没有 source_kind 的分级记忆上色变了')

// 图例：界面层必须解释这枚琥珀色，不然颜色就只是噪音。
assert.ok(ui.includes('日记派生'), '图例里没有「日记派生」')
assert.ok(ui.includes('JOURNAL_COLOR'), '图例没有用渲染器的日记派生颜色')
assert.ok(ui.includes('sm-flegend-item'), '图例条目没有按现有 DOM 写法加')
assert.ok(ui.includes('source_kind'), '详情/图例没有读来源字段')
assert.ok(css.includes('.sm-flegend-dot'), '图例色块没有样式')

// ---- 三种宿主形态 ---------------------------------------------------------
// 离线单文件（内嵌数据）/ 插件路由（SSE）/ 独立浏览器服务（轮询）。
assert.ok(ui.includes('__PMEM_DATA__'), '不支持离线单文件')
assert.ok(ui.includes('__PMEM_EMBED__'), '不支持独立浏览器服务')
assert.ok(ui.includes('STANDALONE'), '没有独立模式的判断')
assert.ok(/function pollData\(/.test(ui), '独立模式没有轮询')
assert.ok(ui.includes('DATA_URL'), '数据地址没有集中一处')

// ---- 动效：必须有，而且不能只活在注释里 --------------------------------
assert.ok(space.includes('spawnMeteor'), '没有流星生成')
assert.ok(space.includes('drawMeteors'), '没有流星绘制')
assert.ok(space.includes('drawFront'), '没有时间回放的光锋')
assert.ok(space.includes('mode.intro'), '没有入场动画')
assert.ok(space.includes('breathe'), '镜头没有呼吸')

// ---- 记忆名开关 -----------------------------------------------------------
assert.ok(ui.includes('LABEL_KEY'), '记忆名没有持久化')
assert.ok(ui.includes("'lite'"), '记忆名没有精简档')
assert.ok(ui.includes('cycleLabels'), '记忆名没有切换入口')
assert.ok(ui.includes('名称·关'), '记忆名关不掉')
assert.ok(ui.includes('animateNumber'), '数字没有滚动')

// ---- 夜空：没有星云，但有银河与远景星系 -----------------------------------
assert.ok(!space.includes('FS_NEBULA'), '星云已经去掉了，不该再有它的着色器')
assert.ok(!space.includes('progNebula') && !space.includes('progDisk'), '星云程序还在')
assert.ok(!space.includes('noiseTex'), '星云的噪声纹理还在（开机要多花 ~200ms）')
assert.ok(space.includes('buildMilkyWay'), '没有银河带')
assert.ok(space.includes('buildFarGalaxies'), '没有远景星系')
assert.ok(space.includes('uTwinkle'), '没有可调的闪烁幅度')
assert.ok(!space.includes('drawSelectionRing'), '选中环已经去掉了')

// 颗粒必须加在色调映射之后：加在前面，黑背景会被 gamma 抬成一片灰。
const gammaAt = space.indexOf('pow(max(col, 0.0), vec3(0.4545))')
const grainAt = space.indexOf('col += g * uGrain;')
assert.ok(gammaAt > 0, '找不到色调映射')
assert.ok(grainAt > gammaAt, '颗粒必须在色调映射之后')

// ---- 无障碍 / 交互的最低要求 ---------------------------------------------
assert.ok(css.includes('prefers-reduced-motion'), '没有照顾减少动效的偏好')
assert.ok(css.includes(':fullscreen'), '没有全屏样式')
assert.ok(ui.includes('requestFullscreen'), '没有全屏入口')
assert.ok(ui.includes("'escape'"), '没有处理 Esc')

// ---- 尺寸单位 -------------------------------------------------------------
// size 是"视觉大小"不是世界半径；忘了换算就是满屏白球（已经踩过一次）。
assert.ok(/SIZE_UNIT\s*=\s*0\.0\d+/.test(space), '尺寸单位没有换算常量')
assert.ok(!/floats\[o \+ 7\] = n\.size;/.test(space), '星体尺寸没有乘 SIZE_UNIT')

// ---- 独立服务 -------------------------------------------------------------
const solo = readFileSync(join(here, 'standalone.js'), 'utf8')
assert.ok(solo.includes('127.0.0.1'), '独立服务没有绑回环')
assert.ok(solo.includes('listen(0') , '独立服务没有用随机端口')
assert.ok(!/listen\(\s*\d{2,}/.test(solo), '独立服务用了固定端口，会和别的程序抢')
assert.ok(solo.includes('injectStandaloneConfig'), '独立服务没有注入配置')
for (const name of ['space.js', 'ui.js', 'space.css']) {
  assert.ok(solo.includes(name), `独立服务没有提供 ${name}`)
}
assert.ok(solo.includes('openInBrowser'), '没有拉起浏览器的实现')

// ---- 目录里不该有临时文件 -------------------------------------------------
const junk = readdirSync(dir).filter(name => /\.(bak|orig|tmp|log)$/.test(name))
assert.deepEqual(junk, [], `starmap 目录里有多余文件：${junk.join(', ')}`)

// ---- 夜空底图：不是一张贴图，是按世界方向算的银河 -------------------------
// 之前那版是一片均匀的深蓝加几颗点，看上去就是一块黑布。
// 现在底图每个像素都要反推一条世界方向，所以它才挂在天球上、会随镜头透视。
assert.ok(space.includes('FS_SKY'), '没有银河底图着色器')
assert.ok(space.includes('progSky'), '银河底图没有编译进来')
assert.ok(/SKY_DIV\s*=\s*[2-8]/.test(space), '银河底图没有降分辨率，多层 fbm 会拖慢开机')
assert.ok(space.includes('galacticBasis') && space.includes('galacticDir'), '没有银道坐标基底')
assert.ok(space.includes('uRight') && space.includes('normalize(uFwd'), '天空不是按世界方向算的')
// 尘埃带必须同时压暗底图和颗粒：只压底图的话，暗缝盖不住星，看起来像两层没对齐。
assert.ok(space.includes('function skyShade'), '尘埃没有烘进颗粒')
assert.ok(/skyShade\(dir\)\.dim/.test(space), '背景星没有吃到尘埃遮挡')
assert.ok(space.includes('filaments'), '尘埃带不是纤维状，会糊成一块斑')
// 星等要是幂律：绝大多数星暗到看不见，少数亮到有星芒，否则星星一样大。
assert.ok(space.includes('BG_LAYERS') && space.includes('Math.pow(u, 2.2)'), '星等不是幂律')
assert.ok(space.includes('brightVao'), '没有近场亮星')
assert.ok(/uMode, 1\.0\);[\s\S]{0,700}?brightVao/.test(space), '亮星没有光晕那一遍，没有体积感')
// 星系是带旋臂的四边形，不是一个圆斑。
assert.ok(space.includes('FS_GAL') && space.includes('uGalaxy'), '星系还是点精灵圆斑')
assert.ok(!space.includes('scene.galCount'), '星系还按点精灵在画')
// 不要再铺常数辉光：那正是黑布感的来源。
assert.ok(!space.includes('vec3(0.004, 0.0055, 0.011)'), '常数辉光又回来了')
assert.ok(space.includes('PIX_REF'), '背景星尺寸没有反推世界尺寸')
console.log('dsh-pmem starmap ok')
