# 时间路由 · 作答自检 · 夜间开关（0.38.0）

> 三件事都是「用外部靶场的失败面反推出来的小改动」，都默认确定性、0 模型调用；
> 唯一花模型额度的是夜间开关（默认关）。

## 1. 时间路由：问时间的句子少喂一点，多给一个锚点

### 为什么

外部靶场（chat-memory-benchmark，714 题，六人物）实测：把检索预算从 `k=8 /
budget=600` 放宽到 `k=40 / budget=2400`，总通过 +16，但**时间类题变差**——
时间约定 −6、时间题 −5、相对日期 −3（**代次/口径：早期一轮、旧判卷 `v4-pro`、口径未对齐
→ C 级，不可与新币种基线并列**；干净对照见 §5：旧币种 k=8→k=40 = **543→572（+29）**、
自检 **572→579（+7）**；新币种基线 **592/714**）。原因不玄：问「上周五」的题，答案通常只有
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
age_labels = true   # ⚠️ 死配置：全仓无读取点（0.38.0 实测），行为恒为「补距今标签」
```

只影响**这一次**检索的上限，不改默认 `top_k`、不写库；调用方显式要更小 k 时不会被抬高。
`age_labels` 是历史遗留键：`grep -r age_labels src/` 只命中 `base/config.py` 的默认值，
没有任何代码读它——**别把它当开关**（关不掉；条目一直带「距今 N 天」）。

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
   都没有 → `kind="name"`。**宁可少报**：命中常见词表、库里任何一行原文里出现过的，
   一律不报。
   **与实现对账（0.38.0）**：确定性专名核对 `_check_names()` **不读 `question`**——question
   只被记录、并传给可选的模型复核（见 `memory/selfcheck.py` 的 `_check_names` 签名与
   `check()` docstring）。所以「问句里出现过的名字一律不报」**目前不成立**：同一个名字若
   只出现在问句、不在库里，仍会被报成 `kind="name"`。

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
  （**546/562/573 是早期旧判卷口径，C 级，不可与新币种并列**；干净对照：旧币种
  k=8→k=40 = **543→572（+29）**、自检 **572→579（+7）**；新币种基线 **592/714**。）

## 6. 作答计划（0.37.0）：把「该怎么答」从散文变成字段

G1 试点（外部靶场 96 题、四臂同判卷）实测：把 4 条作答纪律当散文塞进 system prompt，
「未知当事实」从 31 题降到 21、编造日期 12→6、通过数 +9/92 题（答案长度 75→46 字，−39%）。
代价是「错否」8→13：变保守，该肯定的说成了没记到。

散文有两个毛病：**没法单独开关**（想关一条只能改字符串），**没法被消费**（宿主 / CLI / MCP
各自拼自己的 prompt，同一件事解释三遍）。所以 0.37.0 把纪律做成结构化的「作答计划」。

### 做什么

- `memory/answerplan.py`：`build_plan(store, question, when=?, evidences=?)` → 一个 dict：
  `terms`（复用 `memory.query.content_terms`，不重新判断问句）、`time_scoped`（复用
  `retrieval.timecue`）、`date_basis`（「今天」是哪天，相对日期的换算基准）、
  `evidence`（证据卡：id / day / role / kind / confidence / age_days / text）、
  `must_not_add`（不许补的类目）、
  `rules`（每条只有 `code` + `text`）、`policy`（生效策略）；
- `render(plan)`：**唯一**渲染入口，产出「作答纪律：…」那一段。**CLI / MCP / SDK 都经由
  `answer_plan()` 调它**（`gate/sdk.py` 里 `plan["render"] = answerplan.render(plan)`）；
  **宿主（`deploy/`）不调**——0.38.0 静态检查：`deploy/` 里没有 `answer_plan()` 调用点
  （只有 `test-mcp.mjs` 断言工具 `memory_answer_plan` 存在，那是在测 MCP 工具表）。
  （同一侧的 `relative_dates` 也如此：文档说「宿主读配置后传进来」，实际宿主不读也不传。）
- 纪律全在配置里：`answer.plan.enabled` / `max_sentences` / `suppress[]` / `assert_when_evidenced` /
  `allow_creative` / `date_basis_required`。**加一类「不许补」= 往 suppress 加一个 code**
  （已有 code 见 SUPPRESS_LABEL），不新开分支；未知 code 原样保留（前向兼容）。

### 怎么用

```bash
pmem plan 上次说的夜班见闻，我还欠你几件？                 # 打印纪律块 + 一句话摘要
pmem plan 上周三我们聊了啥？ --when 2026-11-06T21:00:00+08:00
pmem plan 我住哪个城市来着？ --from-search                  # 顺手检索一次，把结果当证据卡
pmem plan --json ...                                        # 结构化输出，给宿主直接消费
```

MCP：memory_answer_plan({question, when?, evidences?}) → {terms, time_scoped, date_basis,
evidence, must_not_add, rules, policy, render, summary}。

### 边界

- 只算「该怎么答」，**不改答案本身**；要不要照做由调用方决定；
- 不重新理解问句（问句解析只有 `query` / `timecue` 两处，这里只读）；
- 证据卡只做字段归一，不排序、不打分（排序仍是检索的事）；
- 0 模型调用、0 写入。

### 6.1 全量实测后默认改为关（0.37.0，回滚）

拿 714 题全量跑了一遍（作答 deepseek-v4.1-flash / low，判卷 v4.1-flash / high，同币种基线 592）：

| 口径 | 基线（重判） | 加上作答计划 | 净 |
|---|---|---|---|
| 全量 | 586/705 | **529/705** | **−57** |
| 调参集 | 508/611 | 457/611 | −51 |
| 留出集 | 78/94 | 72/94 | −6 |

掉得最多的三类：第三方事实 17、资料不足 14、助手自己说过的话 10；判卷理由里
「未满足 required」12、「否认证据」11、「无证据日期」10。
根因是**两套指令叠加**：材料模板里本来就有「不确定就直说不知道」，再加一段纪律，
答案变成「材料里没写明…」这种免责式措辞，被判成否认证据；同时为了守「不补细节」，
rubric 要求的建议（比如「可以问卖家」）也一起没了。

所以 0.37.0 的 `answer.plan.enabled` **默认 false**（代码保留、能单独开关、有单测），
**但 `enabled` 不是总开关**：它只控制第一条 discipline（只答被问的），其余 3 条仍会渲染；
总闸待 0.39 加。要等重新设计过再默认开。
