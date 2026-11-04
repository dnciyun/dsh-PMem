/**
 * 日记流水的两个纯函数：剥运行时上下文、裁行。
 *
 * 这一组守的是一个真出现过的 bug：DSH 的运行时上下文**有时排在用户真消息前面**，
 * 于是日记流水第一行记成了 `Current DSH file policy: danger-full-access…`，
 * 醒来卡的「上次」也变成这一段。
 */

import assert from 'node:assert/strict'
import { stripRuntimeContext } from './index.js'

const cases = [
  [
    'Current DSH file policy: danger-full-access. The DSH file sandbox does not restrict file modifications by available operations.\n\n我用旧的就好了',
    '我用旧的就好了',
  ],
  [
    '[骨架]\n- 约束: 这是别人的记忆插件\n[索引] 5590 条可检索（seq 7–26848）\n[提示] 本轮问题与卡片重合很低\n我说的话',
    '我说的话',
  ],
  ['普通一句话', '普通一句话'],
  ['', ''],
  [
    'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\nCurrent working directory: "F:\\\\Ai\\\\AWB"\n\nBrowser time zone for this request: Europe/Rome\n真消息在这里',
    '真消息在这里',
  ],
]

for (const [input, want] of cases) {
  const got = stripRuntimeContext(input)
  assert.equal(got, want, `期望 ${JSON.stringify(want)}，实际 ${JSON.stringify(got)}`)
}

// 不能把真消息吃掉：没有运行时前缀时原样返回
assert.equal(stripRuntimeContext('你好呀'), '你好呀')

console.log('test-journal.mjs ok （剥运行时上下文 ' + cases.length + ' 例）')
