# 写给「正在帮你装 PMem 的那个 DSH」

> 有人对你说「**帮我安装 https://github.com/dnciyun/dsh-PMem**」的时候，先别急着 `pip install`。
>
> 这不是一个普通插件：它要**接住一个人和他那条鱼的记忆**。装错了，改的是别人最私人的东西。
> 这份文件是给**你**（DSH）看的操作手册：先问清楚，再动手；动之前先备份。

---

## 0. 三十秒说清它是什么

PMem 是**一个东西**，两层皮：

- **记忆内核**（Python + 一个 SQLite 文件）：原话流水、记忆单元、日记、实体关系、自我层（宪法/增补/叙事）、夜间睡眠、星图；
- **DSH 外壳**（插件 `dsh-pmem`）：每轮的背景条、查询工具、设置页、星图页。它顺手把 MCP 注册块写好并拉起 MCP。

所以安装只有一步：**装插件 = 内核和外壳都装好**。不要拆成"先装 MCP 再装插件"。

---

## 1. 动手之前：这 8 个问题必须问用户

**逐条问，不要替用户选**。每条后面写着「为什么」和「影响什么」，你可以直接照念。

| # | 问题（可以直接念） | 为什么问 / 影响什么 |
|---|---|---|
| 1 | 这台机器上的这条鱼，是**陪你过日子**，还是给它**干活**？ | 陪聊：常驻卡 + 宽注入（每轮 300~500 token）更自然；干活：预算收紧到 200 左右，优先工具调用。 |
| 2 | 记忆库想放在哪？默认是**仓库里的 `data/pmem.db`**，也可以放别处。 | 放仓库里方便跟着仓库走；放独立目录更安全（不怕 `git clean`）。无论放哪都要定期备份。 |
| 3 | 现在**有没有旧记忆要接管**？是哪个：`dsh-soul` / `surfcore` / `dsh-hippocampus` / 别的记忆插件 / 纯文本或 markdown 文件 / 没有？ | 有旧记忆就必须走第 5 节的迁移流程；没有就直接新建。**先问清楚，别直接装完当空库用。** |
| 4 | 旧数据**要不要保留**（默认保留，只读）？ | 永远默认保留。删旧库这件事，除非用户明确说"删"，否则不做。 |
| 5 | 这个 DSH **会不会多开会话 / 多台机器**？ | 多会话：备份要勤（`pmem backup run`），并注意别让两个进程同时批量写同一个库。 |
| 6 | 要不要**接 QQ / 微信 / B站**这类桥？**接了的话，群里的话要不要也写进记忆？** | 桥接有工具白名单，PMem 的工具默认被拒。要接就得改白名单（只放读和"记一句"）。另外桥接端说的话默认**进不了**记忆——要进就得加钩子，见 4.5 节。 |
| 7 | 每轮愿意花多少 **token** 记着这些事？（约 200 / 400 / 1000+） | 直接影响"记得多细"。默认约 400 tok/轮；调到 200 省但容易丢细节；1000+ 适合长对话但贵。 |
| 8 | **可以重启一次 DSH 吗？** | 必须重启：插件 bundle、MCP 工具表、桥接白名单都是启动时读的。不重启 = 装了个寂寞。 |

问完再做一次环境自检：

```bash
python --version        # 需要 3.11+
pmem dsh status         # 装过就体检；没装过会提示
```

---

## 2. 安装（一条命令，然后重启）

```bash
git clone https://github.com/dnciyun/dsh-PMem
cd dsh-PMem
pip install -e .
pmem init                 # 建库（幂等，可反复跑）
pmem dsh install          # 登记 bundle + 写 MCP 注册块（默认 profile=desktop）
# 让用户重启 DSH
```

- 装完 `pmem` 不在 PATH？用 `python -m pmem.gate.cli <子命令>` 代替（所有命令同理）；
- 装到别的 profile：`pmem dsh install --profile <名字>`；
- 机器上有多个 python：`pmem dsh install --python "C:\path\to\python.exe"`（MCP 会用它拉起）；
- 只预演不落盘：加 `--dry-run`。

**重启后逐条验证，不要只看"装好了"三个字：**

```bash
pmem dsh status           # bundle / MCP 片段 / python / 库 四项都该是 ✓
pmem doctor               # 完整性 ok、事件链 verified、不变量通过
pmem stats                # 记忆条数、状态分布、integrity_ok、db_path
```

再让宿主模型调一次 `memory_capabilities`，确认 `server` / `schema_version` / `degraded`。
工具表里应该出现 `mcp__pmem__*`（记忆查询类）和 `pmem_*`（原生：wake/recall/remember/stats）。

**装坏了怎么退**（一条命令，记忆一条不动）：

```bash
pmem dsh uninstall        # 只摘注册，不碰数据库
```

---

## 3. 接入时最容易踩的 6 个坑

1. **改 profile 之前先备份** `~/.dsh/profiles/<profile>/`（`cordis.patch.yml` 与 `package.json`）。
2. **不要手改 MCP 注册块**：它在 `cordis.patch.yml` 里带 `# === pmem MCP BEGIN ===` / `END` 标记，
   由 `pmem dsh install` 整体维护。手改会被下一次安装覆盖。
3. **中文参数在 PowerShell 里会乱码**（`pmem retract "那句错话"` 传进去变问号）：
   写临时 `.py` 脚本调用，或设 `PYTHONIOENCODING=utf-8`；别用 `python - <<EOF`。
4. **两个进程别同时批量写同一个库**：PMem 用 `BEGIN IMMEDIATE` + busy 重试，单条写没问题；
   但如果两个进程都在灌几万条，会互相等锁。灌数据时串行做。
5. **别把 PMem 当"第二个 MCP"重复注册**：它已经由插件拉起，再手写一份会冲突/重复。
6. **DSH 版本太老可能没有 `tools.restrict`/`tools.guard`/`settings.section`**：
   装完 `pmem dsh status` 若报插件加载失败，先升级 DSH，或退回 `deploy/README.md` 的手工安装。

### 3.1 桥接（QQ / 微信 / B站）白名单

桥接会话里有**工具白名单**，PMem 的工具默认不在里面，会看到：

```
工具 "pmem_recall" 不在 QQ 桥接白名单内，已拒绝
```

要接就改桥接的白名单（例如 SurfCore 的 `qq-tool-restrict.mjs`），**只加这些**：

- 原生：`pmem_wake` / `pmem_recall` / `pmem_remember` / `pmem_stats`
- MCP：`memory_search` / `memory_get` / `memory_trace` / `memory_stats` / `memory_history` /
  `memory_capabilities` / `memory_profile` / `memory_surface` / `memory_add`

**不要加** `memory_update` / `memory_delete` / `memory_import` / `memory_retract` / `memory_sleep` /
`memory_evolve` / `memory_consolidate` / `memory_soul` / `memory_group` 这一批：
群里的消息不可信，不能给它改账和改「我是谁」的钥匙。改完同样要**重启 DSH**。

---

## 4. 迁移记忆 / 接管记忆（重点，先读完再动手）

### 4.1 四条铁律

1. **先备份，再迁移**：`cp -r <老数据目录> <老数据目录>.bak-<日期>`，或用旧插件自己的备份命令；
2. **不删源**：迁移只读老数据，从不"搬走"再删。老目录留着，出问题能对照；
3. **先 dry-run，再真写**：`pmem import --dry-run`，看条数与冲突数，再正式导；
4. **导入后对账**：条数、抽样 10~20 条关键记忆（名字、关系、承诺、偏好）搜得到、内容对得上，
   再让用户决定"以后只用 PMem"。

### 4.2 情况 A：从 DSH 会话日志迁移（最常用）

DSH 的会话记录本身就是记忆原料，PMem 带一个只读回填器：

```bash
pmem backfill dsh --dry-run --days 365      # 先看会抽多少
pmem backfill dsh --days 365                # 正式回填（写进记忆库）
pmem doctor
```

注意：回填只读会话文件，不改 DSH 数据；同一作用域重复跑用 `--reset` 先清上一次的回填结果。

### 4.3 情况 B：从别的记忆插件迁移（dsh-soul / surfcore / hippocampus / 文本库）

没有一键转换器（各家表结构不一样，硬写等于瞎猜），正确做法是**让用户看着你写一个一次性转换器**：

1. 打开老库看清结构（SQLite 用 `sqlite3 <db> ".tables"`、`.schema <表>`；JSONL 先 `head` / `head -c`）；
2. **一条记忆 = 一行文本**。宁少勿编：拿不准的字段不猜，放进 `extra.source`；
3. 转成 PMem 的 JSONL，格式如下（首行必须是 header）：

```json
{"type":"pmem_export","version":"v1","schema_version":"1.10.0","created_at":"2026-11-04T00:00:00Z","embedding_model":null,"counts":{"memory":2}}
{"type":"memory","id":"01IMPORT0000000000000000001","text":"用户的猫叫年糕，是只橘猫","kind":"semantic","provenance":"external","status":"active","sensitivity":"personal","created_at":"2026-10-01T12:00:00Z","day":"2026-10-01","source":{"kind":"imported","ref":"dsh-soul"},"extra":{"origin_table":"nodes","origin_id":"n-42"}}
{"type":"memory","id":"01IMPORT0000000000000000002","text":"2026-10-03 他说想换工作，但还没下定决心","kind":"episodic","provenance":"external","status":"active","sensitivity":"personal","created_at":"2026-10-03T21:30:00Z","day":"2026-10-03","source":{"kind":"imported","ref":"dsh-soul"}}
```

- `id` 必须唯一（自己生成 ULID 或加前缀即可）；`text` 必填；
- `kind` ∈ `episodic/semantic/person/relation/experience/goal/meta`；
- `provenance` ∈ `user_explicit/user_confirmed/external/observed/inferred/unconfirmed`
  —— **迁移进来的一律用 `external`**，别冒充"用户亲口说过"；
- `status` 建议 `active`；拿不准的老条目用 `candidate`（它会进检索，但会被标"候选·还没确认"）；
- `sensitivity` 默认 `personal`，明显是隐私的用 `secret`（导出时会自动打码）；
- 行首别加 BOM，编码 UTF-8。

4. 导入：

```bash
pmem import ./import.jsonl --dry-run                 # 只校验，不写库
pmem import ./import.jsonl --on-conflict skip        # 正式导入（重复 id 跳过）
pmem doctor                                          # 导入后体检
pmem search "年糕"                                    # 抽一条验证
```

5. **让用户看一遍再确认**：把"共 X 条、跳过 Y 条、抽样 5 条"发给他。

> 老插件的派生数据（关系图、特质、冲突）**不要硬导**。PMem 会自己从原话和记忆里重算
> （`pmem net extract` / 夜间 `pmem sleep`）。硬搬派生层只会把别人的算法结果当成事实。

### 4.4 接管：别双写，先只读并行

- **新写入只走一个系统**：切到 PMem 之后，旧的记忆插件要停用（或至少不再写）。双写一定会分叉；
- **旧数据只留档**：`~/.dsh/dsh-soul/`、`~/.dsh/hippocampus/` 这些目录**不要删**，
  留一份"原始账本"随时比对；
- **并行期判定标准**：抽样 20 条关键记忆（我是谁、关系、承诺、偏好、上个月做的事），
  在 PMem 里搜得到、内容对得上，再宣布"接管完成"；
- **可回滚**：`pmem dsh uninstall` + 恢复 profile 备份 = 回到装之前，记忆库原封不动。

### 4.5 桥接钩子：让 QQ / 微信里说的话也进记忆（可选，但很值）

**问题**：桥接（SurfCore 这类）每轮塞给 agent 的是**唤醒包装**（`【当前时间】/【当前角色】/【你的身份】…`），
真正的群消息要 agent 自己调工具才读得到。PMem 插件只抓「DSH 的 user message」——
所以在不加钩子的情况下，**QQ 里说十句话，记忆库一句话都没有**；另一边（B站、主会话）当然也就查不到。

**做法**：在桥接的「消息入库」处加一个钩子，把用户原话写进 PMem 的**原话流水**
（`journal turn`），先不写记忆表——等夜里消化成日记/事实，或者 agent 自己 `pmem_remember`。

SurfCore 的 `config.json` 里加：

```json
"pmemBridge": {
  "enabled": true,
  "python": "python",
  "src": "<PMem 仓库>/src",
  "db": "<PMem 仓库>/data/pmem.db",
  "cwd": "<PMem 仓库>",
  "sessionPrefix": "pmem-bridge:",
  "maxChars": 400,
  "debounceMs": 2000
}
```

钩子行为（实现见 `surfcore/src/bridge.js` 的 `pmemBridgeIngest`）：

- **只写用户消息**，不写 agent 自己的回复；
- 批量：2 秒窗口攒一次，一次最多 12 行，只起一个 python 子进程，失败静默（不影响桥接）；
- 跳过 `/` 开头的命令、跳过命中敏感正则的消息（路径/凭据）——宁可漏一句，不把钥匙写进库；
- 同一句话连发去重。

**验证**：重启桥接后，在 QQ 里说一句「今天试一下跨端记忆」，然后在 PMem 仓库里：

```bash
pmem journal recent            # 能看到刚写进来的流水
pmem trace "跨端记忆"           # 按字查原话，能定位到会话和行号
pmem journal digest --all      # 消化成日记 + 事实（夜里会自动跑）
pmem search "跨端记忆"          # 消化后，B站/主会话也搜得到
```

**代价**：每 2 秒至多一个 python 子进程（约 200 ms，异步）；这些内容本来就该进本地库，不额外外发。

**红线**：钩子只写流水，**不写记忆表、不做摘要、不改别人系统的数据**；它只是「把说过的话补录下来」。

---

### 4.6 换机器

```bash
pmem export --out pexport.jsonl      # 记忆原文 + 元数据（不含向量，导入后按需重算）
pmem import pexport.jsonl --dry-run
pmem import pexport.jsonl
# 默认导出不含事件日志；要整条账本加 --with-events。
# 事件是哈希链，只能导进空 event_log 的库（换机器建议直接拷备份文件）。
```

---

## 5. 装完要向用户汇报什么（别只说"装好了"）

1. 库在哪（`pmem stats` 里的 `db_path`）、多少条记忆、`integrity_ok` 是不是 true；
2. 接管了哪些老数据、共几条、跳过了几条、抽样 3 条给他看；
3. 每轮会占多少 token、常驻卡现在长什么样（`pmem soul show` 或醒来卡）；
4. 备份在哪、怎么回滚（`pmem backup list` / `pmem dsh uninstall`）；
5. 还有哪几件事**没做**（例如：还没跑夜间睡眠、还没接桥、还没配 daemon）——**没做的别贴着说做完了**。

---

## 6. 红线（任何情况下都别做）

- 不删用户的记忆库、不删自我层文件、不删旧插件的数据目录（除非用户明确说删）；
- 不用 `--hard` 删除；要"撤"用 `pmem retract`（撤回是留痕，不是清账）；
- 不把 `~/.dsh/.credentials.yaml`、token、密钥写进记忆或导出的 JSONL；
- 不替用户决定"我们的关系应该是什么样"——自我层归用户，怎么写由他和他的鱼定；
- 不在 QQ/微信群这种不可信环境里放删除/导入/批量写入的工具。

---

## 7. 出问题先看这四样

```bash
pmem dsh status      # 装在不在、MCP 片段对不对、python 是不是同一个
pmem doctor          # 完整性 / 事件链 / SQLite 版本 / 19 项不变量
pmem stats           # integrity_ok / db_path / 条数分布
pmem trace "那句话"   # 按字查原话：在不在、谁说的、哪次会话第几行
```

`db_path` 这一项尤其要看：**报错时第一件事是确认它读的到底是不是你想的那个库。**
