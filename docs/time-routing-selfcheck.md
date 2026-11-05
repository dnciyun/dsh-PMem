# 时间路由 · 作答自检 · 夜间开关（0.36.x）

> 三件事都是「用外部靶场的失败面反推出来的小改动」，都默认确定性、0 模型调用；
> 唯一花模型额度的是夜间开关（默认关）。

## 1. 时间路由：问时间的句子少喂一点，多给一个锚点

### 为什么

外部靶场（chat-memory-benchmark，714 题，六人物）实测：把检索预算从 `k=8 /
budget=600` 放宽到 `k=40 / budget=2400`，总通过 +16，但**时间类题变差**——
时间约定 −6、时间题 −5、相对日期 −3。原因不玄：问「上周五」的题，答案通常只有
一两条，多给的 30 多条候选只是把 600 token 摊薄、把真正那一天挤到后面。

另一半是算错：B 层「相对日期」29 道失败里 27 道是「捞到了但算错/不敢算」。日期
本身有，缺的是「这条离现在是多久」。

### 做什么

- `memory/retrieval/timecue.py`：`is_time_question(q)` 确定性识别时间线索
  （上周/昨天/星期X/去年/10月29日/N天前/N个月/多久/什么时候/几号…）。
- `retrieval.search`：命中时间题的这次检索把 `k` 压到 ≤ `cap_k`、`budget` 压到
  ≤ `cap_budget`；返回里多一个 `meta.time_router = {routed, k, budget}`，其余字段不变。
- 注入侧：`_occurred_label` 在日期后补「（距今 N 天）」（N≥1 才补），检索 item 也带
  `age_days` / `age_label`。今天的事不补「距今 0 天」。

### 配置

```toml
[retrieval.time_router]
enabled = true      # 默认开；关掉 = 完全回到旧行为
cap_k = 8
cap_budget = 600
age_labels = true
```

只影响**这一次**检索的上限，不改默认 `top_k`、不写库；调用方显式要更小 k 时不会被抬高。

### 怎么验证

- 单元测试：`tests/test_time_router.py`、`tests/test_occurred_time_render.py`
- 外部靶场：`outputs/pmem/exam.py` 不用再传 `--time-router`（产品侧已自动路由），
  同一批 714 题重答即可对比；离线估算 +11（562 → 573）。

## 2. 作答自检：回答里的日期/人名，回库里查一遍

### 为什么

回答里最容易「编得很像」的是具体日期和人名：说成「上周三」读起来通顺，只有回库
才看得出没有出处。外部靶场里「未知当事实」94 道是最大单块失分，其中一类就是
把「不知道」写成了具体日期/人名。

### 做什么

`memory/selfcheck.py`（确定性、只读、零模型）：

1. **日期**：抽答案里的 `2026-11-05` / `2026年11月5日` / `11月5日` / `11月5号`
   （没写年份按「今天」推到最近一次，且不能是 2 月 30 日这种不存在的日期），
   到 `memory.day_key` / `memory.valid_from` / `journal_line.ts` 里找同一天的证据；
   找不到 → `kind="date"`。
2. **专名**：本主体 `entity` 表里的名字（含别名）在答案里出现就算有据；答案里另外
   出现「像人名」的串（姓氏表 + 2–4 字 + 不是虚词/亲属词结尾），而实体表和全部原文
   都没有 → `kind="name"`。**宁可少报**：问句里出现的、常见词表里的、库里任何一行
   出现过的，一律不报。

返回 `{clean, issues:[{kind, span, reason, evidence}], checked:{dates,names}, note}`。
`clean=false` 只说明「有对不上的地方」，要不要改、怎么改由调用方决定。

### 怎么用

- MCP：`memory_selfcheck(answer, question?)` → 结构化清单 + `report` 文本。
- CLI：`pmem selfcheck "草稿回答"`（不传就从 stdin 读，方便管道）。
- Python：`PMem.selfcheck(answer, question=...)`。

### 边界

- 只核对**日期/专名**（确定性那遍）+ 「没出处的陈述」（模型那遍），不评判观点、不判断
  相关性、不检查「答非所问」。
- 没有可参照记载的日期（记忆里那天本来就没东西）也会报——这是有意的：回答里不该
  出现一个库里没有的精确日期。
- 模型复核是可选的第二遍：`[selfcheck] model = "deepseek-chat"`（需要凭证）。不配就是
  纯确定性、0 成本、结果可复现；配了会多一次小调用（证据 6 条 + 草稿，约 ¥0.0005/题量级），
  模型挂了/返回非 JSON 只写进 note，不影响确定性结论。issue 的 kind 会是 `"claim"`。

## 3. 夜间开关：夜里到底用不用真模型

- 配置：`[sleep.whale] enabled`，**默认 false**。
- 关（默认）：夜里只跑本地模板日记 + 8 阶段，0 模型调用、0 成本。
- 开：夜间由 DSH 会话（宿主模型，即我）把当天原文想成日记，用宿主额度，不需要 API key。
- 设置位置：DSH 设置 → 海马体 → **夜间** → 「夜间用真模型思考（鲸眠）」开关；
  改完下一晚生效。落盘走 `pmem config set sleep.whale.enabled true|false`（见下）。
- 夜间定时任务会先读 `memory_capabilities` 的 `night.llm` 再决定走哪条路。

## 4. 配置文件：`~/.dsh/pmem/config.toml`

0.36.0 之前，插件调 CLI 只传 `--db`，等于永远用内置默认值——设置页里没有能持久化
的 PMem 级开关。现在 CLI/MCP 的 `--config` 默认指向 `$DSH_HOME/pmem/config.toml`
（缺省 `~/.dsh/pmem/config.toml`）；文件不存在 = 行为与旧版完全一致。

```bash
pmem config path                       # 打印配置文件路径
pmem config get sleep.whale.enabled    # 读生效值（未写=默认值）
pmem config set sleep.whale.enabled true
pmem config list                       # 只列文件里显式写过的键
```

写回是**整份重写成点号键** TOML（`sleep.whale.enabled = true`），只保存标量/标量数组；
这是机器管理的文件，手写注释会被覆盖。

## 5. 验收口径（本轮未跑分）

- 单测：`pytest tests -q` 全绿；`node deploy/dsh-pmem/test-web.mjs` / `test-mcp.mjs` /
  `test-client.mjs` 全绿。
- 外部靶场重跑（需要有效 key，本轮未做）：同一批 714 题，基线 546 → k=40 562 →
  产品侧时间路由（离线估算）573 → 再加自检（第二遍模型复核）看「未知当事实」掉多少。
