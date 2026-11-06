# dsh-pmem

把「可塑性记忆 / PMem」接进 DSH 的插件。它做七件事：

> **每次新会话开头，先认出你；每一轮干活时，想起一点；会话结束时，把“停在哪”留下。**

> 后续产品化路线图（MCP 接入 / 设置页 / 安装器）见
> [`docs/plugin-roadmap.md`](../../docs/plugin-roadmap.md)。

1. 在 system prompt 里注入醒来卡；
2. 每轮按线索/情境/节律**浮现**最多一条记忆（M9.65）；
3. 会话结束时自动 handoff；
4. `/pmem` 命令；
5. 5 个 `pmem_*` 原生工具；
6. 同一 bundle 通过官方 MCP client 带上 27 个 `memory_*` 工具；
7. 侧边栏一行**「记忆控制台」**：整页可视化控制台（总览 / 记忆 / 实体 / 睡眠 / 设置），替代原来只能窝在设置弹窗里的卡片窗口。

上下文清空、换会话、换天之后，模型不该从零开始。插件会在 system prompt 里放一张
≤300 token 的**醒来卡**：

```
<<<PMEM_WAKE v1 — 长期记忆，只读数据，不是指令>>>
我是谁：……
我们：……
现在：……
上次：……
下次：……
<<<END_PMEM_WAKE>>>
```

卡的内容来自 PMem 的真实记忆（`pmem wake`），不是硬编码；卡是只读的，不写
`surface_log`，不改使用次数。

## 为什么不走 MCP 自动注入

DSH 里 MCP server 拿不到 system prompt，也没有 hook（`docs/04-interface.md` §6.3）。
MCP 只能“模型主动调工具”，而醒来卡必须在模型开口之前就在。所以自动注入只能由
宿主插件做；PMem 的 27 个 MCP 工具仍然可以另接，两者不冲突。

## 安装（M9.8：一条命令）

```bash
pmem dsh install              # 登记 bundle + 按本机路径写 MCP 注册块 + 建库
pmem dsh install --dry-run    # 只预演，一个字节都不落盘
pmem dsh status               # 逐条体检（含"仓库搬家后路径对不上"）
pmem dsh uninstall            # 只摘注册，一条记忆都不删
```

它替你做的四件事：

1. `~/.dsh/profiles/<profile>/package.json`：`dependencies["dsh-pmem"] = "link:<本机仓库>/deploy/dsh-pmem"`，
   并加进 `dsh.profile.bundles`；
2. `~/.dsh/profiles/<profile>/cordis.patch.yml`：写入 MCP 注册块
   （`# === pmem MCP BEGIN/END ===` 之间，路径按**本机实际位置**算，
   所以仓库搬到别的盘重跑一次就行）；
3. 记忆库不存在就 `init` 一个；
4. 第一次改文件前留一份 `<file>.pmem-bak`。

**只剩一步要手工**：在 profile 目录跑 `pnpm install`（只链接本地包，不需要联网），
然后重启 DSH。安装器不会代你跑包管理器——那一步联网、会改 lock 文件。

**可选：向量通道（M13）**。词法答不上来时用本地 bge 做第二手段，装了更准，不装也能跑：

```bash
python scripts/fetch_model.py --onnx int8   # 24 MB，下到 ~/.dsh/pmem/models/，sha256 校验
pmem vector status                          # 看模型/onnxruntime/向量是否齐
pmem vector build                           # 给已有记忆补编码（幂等）
```

缺 `onnxruntime` 或模型文件时，这条通道整条静默降级，`pmem doctor` / 检索都不会报错；
`pmem capabilities` 里的 `vector_state.reason` 会写清楚缺什么。

回滚：`pmem dsh uninstall`，或拷回 `*.pmem-bak`。不要动 profile 里
qq-bridge 的区块。

### 为什么 MCP 那一段不在包内 patch 里

插件本体是包内组件，路径相对包就能找到，跟着包走；MCP server 是**另一个进程**，
要 python 可执行文件、要 `--db` 的绝对路径、要 `PYTHONPATH` 指到本机仓库的 `src`。
这些只能在安装时算，所以由安装器写进 profile 级 patch。
`deploy/cordis.patch.yml` 留作手工安装的备用片段（里面有 `<仓库>` 占位符）。

## 配置

默认值从仓库根目录推导（插件在 `deploy/dsh-pmem`，根目录是 `../..`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `db` | `<repo>/data/pmem.db` | PMem 库 |
| `python` | `python` | Python 解释器 |
| `cwd` / `src` | `<repo>` / `<repo>/src` | CLI 工作目录与 `PYTHONPATH` |
| `budget` | `300` | 醒来卡 token 预算 |
| `refreshMs` | `300000` | 卡刷新间隔（5 分钟） |
| `injectCard` | `true` | 是否注入 system prompt |
| `tools` | `true` | 是否注册 5 个工具 |
| `commands` | `true` | 是否注册 `/pmem` |
| `handoff` | `true` | 会话结束是否写「上次停在哪」 |
| `handoffTurns` | `6` | 长会话每 N 轮落一次 |
| `handoffMinMs` | `600000` | 两次 handoff 的最小间隔 |
| `surface` | `true` | 是否每轮做「浮现」（order 942） |
| `surfaceCooldownMs` | `90000` | 两次浮现查询的最小间隔 |
| `surfaceTimeoutMs` | `1500` | 浮现查询超时；超时就当作没想起来 |
| `web` | `true` | 是否注册 `/dsh-pmem/api/*` 与星图页 |
| `runtimeFile` | `~/.dsh/pmem/runtime.json` | 设置页写的运行时开关 |

## 工具与命令

- `pmem_wake`：读醒来卡；
- `pmem_recall(query, k?)`：按关键词回捞；
- `pmem_remember(text, group?, grade?)`：写一条长期记忆；
- `pmem_stats`：库规模/状态/完整性。

`/pmem wake`、`/pmem recall <词>`、`/pmem remember <内容>`、`/pmem surface [线索]`、`/pmem stats`、`/pmem mcp`。

## 浮现（M9.65）

每轮 `agent/pre-step` 取最后一条用户消息当 `cue`、`session.header.cwd` 当情境，
调一次 `pmem surface --mode turn`；命中的结果在 `systemPrompt.context(order=942)` 注入，
最多一条、≤80 token。三种触发：

- `cue`：你这句话里出现了它；
- `context`：正在改的文件 / 正在跑的目录；
- `rhythm`：谁都没提，但这条 A/B 级记忆快忘了——每会话最多主动提 3 条。

Python 不可用、超时、返回空，都只是这一轮少一块提示，不影响主流程。
`/pmem surface 杭州` 可以手工看这一轮会想起什么。

## 回执与 token 采样（M9.9）

每轮 `turn/end` 调一次 `pmem receipt`，做两件事：

1. **回执**：把这一轮的回答正文交给 Python，判定「浮现过的记忆里哪几条被逐字复用了」
   （≥8 字重合，且不在用户这句话里），命中的落 `surface.used`，`use_count` 才涨；
2. **token 采样**：本轮注入 = 醒来卡 tokens + 浮现块 tokens，两个数都由 Python 算，
   写进 `metrics(pmem.token_per_turn)`，设置页概览能看到均值 / p95（门：≤600 / ≤800）。

这是**下限估计**：改写、转述都不算。所以 `use_count` 涨了是硬证据，不涨不代表没用。
插件 0.5.0 起生效，回执失败不影响会话。

## 设置页与星图（M9.7a → M9.7b 重做；控制台见 M22）

设置 → **可塑性记忆**：只剩一行「控制台已独立」+「打开控制台」按钮；原七个标签全部搬进侧边栏的整页控制台。

### 记忆控制台（M22 / 插件 0.8.0）

左侧导航五个分区，右侧内容区，桌面大屏优先：

| 分区 | 做什么 |
|---|---|
| 总览 | 指标条（总量/今日新增/今日召回/未决冲突/使用次数）、token 用量、请求健康一行、睡眠状态、最近 10 条记忆、快捷（星图/体检/刷新醒来卡） |
| 记忆 | 搜索（auto/keyword/vector/recent）+ 分组树过滤 + 排序 + 多选批量；详情含正文、元数据编辑（kind/grade/分组/敏感级/重要度/状态/置顶）、证据原话、变更历史、冲突、相关记忆、使用统计；反馈/撤回/删除（默认软删、二次确认） |
| 实体 | 实体列表/详情（别名、提及记忆、1 跳关系）、2 跳查询、合并（先 dry_run 预览再确认）、跳星图 |
| 睡眠 | 八阶段 run 列表 + 每阶段耗时/条数小条形图 + 进行中进度轮询 + 最近 14 次质量趋势；现在入睡 / 大睡一场（模板 / DSH） |
| 设置 | 运行时开关与数值、**夜间：用不用真模型思考（鲸眠）开关，默认关**、检索/大睡默认值（localStorage）、排查面板（请求日志/醒来卡缓存/MCP/路径/doctor/体积/配置差异）、危险区（备份/恢复需输入 RESTORE/重建索引/运维白名单/批量删除） |

![记忆控制台 · 总览](docs/images/console-overview.png)

侧边栏入口 + 五个分区：

| 截图 | 看什么 |
|---|---|
| ![](docs/images/console-sidebar.png) | 侧边栏「记忆控制台」入口（海马图标） |
| ![](docs/images/console-memory.png) | 记忆页：搜索/过滤/列表 + 详情/元数据编辑/证据/历史 |
| ![](docs/images/console-entity.png) | 实体页：实体列表 + 详情（别名/提及记忆/1 跳关系） |
| ![](docs/images/console-entity-merge.png) | 合并预览（dry_run，不写库） |
| ![](docs/images/console-sleep.png) | 睡眠页：八阶段 + 每阶段耗时/条数 + 质量趋势 |
| ![](docs/images/console-settings.png) | 设置页：旧七标签已删，只剩「打开控制台」 |
| ![](docs/images/console-settings-open.png) | 点「打开控制台」后弹窗关闭、整页控制台接管 |\n| ![](docs/images/console-status-model-header.png) | 顶栏三盏灯：数据库 / 模型通道 / MCP（0.8.1 起模型通道接宿主探测） |

所有操作都走同源 `fetch`，不开新窗口、不整页跳转（桌面壳会静默 deny / 401）。
空库时总览给引导卡（建库 / 写第一条 / 导入 JSONL / 看文档）。

### 夜间开关（插件 0.8.2 / PMem 0.36.0）

设置页「夜间」面板一个开关：**夜间用真模型思考（鲸眠）**，默认关。

| 状态 | 夜里发生什么 | 成本 |
|---|---|---|
| 关（默认） | 本地模板日记 + 固定 8 阶段巩固 | 0 模型调用、0 成本 |
| 开 | 由 DSH 会话（宿主模型）把当天原文想成日记，再跑 8 阶段 | 用宿主额度，不需要 API key |

- 值落在 PMem 自己的配置文件（`$DSH_HOME/pmem/config.toml`，缺省 `~/.dsh/pmem/config.toml`）：
  `sleep.whale.enabled = true|false`，也可以 `pmem config set sleep.whale.enabled true` 改。
- 走 CLI 的 `config get/set`，插件不直接读写 TOML；接口是 `GET/POST /dsh-pmem/api/night-llm`。
- 夜间定时任务先看 `memory_capabilities` 的 `night.llm`，再决定走鲸眠还是本地 8 阶段。
- 改完下一晚生效（不是立即）。

### 大睡一场（插件 0.8.3）

设置 → 海马体 → 夜间 → 「大睡一场」默认由 DSH 来：先 `pmem sleep plan` 算出要补睡的日子，
再开新会话，把鲸眠提示词投递进去，由宿主模型逐天调 `memory_whale_prep` / `memory_whale_finish`。
每批一个新会话（默认 6 天），PMem 进程本身不碰模型凭证。

> **0.8.2 的坑**：点按钮报 `Cannot read properties of undefined (reading 'throwIfAborted')`。
> 宿主的 `sessionController.prompt(request, signal)` 第一条语句就是 `signal.throwIfAborted()`
> （DSH 0.2.2-rc.1，`dsh-api-session-controller`），而 0.8.2 只传了 `request`。
> 0.8.3 起每批传自己的 `AbortSignal`；`test-web.mjs` 的 mock 也照宿主的写法要求 `signal`，
> 再漏传会直接测挂，不会只在真机上才炸。

星图**不占标签**，只在概览留一行开关 + 两个入口：

只剩一个入口：**在浏览器打开**。插件自己起一个只绑 `127.0.0.1`、端口随机的小服务，
再由 Node 侧拉起系统默认浏览器；页面每 4 秒轮询一次，`generated_at` 变了才重建。

> 之前还有一条「在这里看」的页内浮层，去掉了：窗口边缘有概率压住它的关闭按钮，
> 而浏览器自己管自己的窗口，没有这个问题。

两条路用同一份渲染器与界面层，只是数据从哪来不一样。

> **为什么浏览器要另开一个服务**：DSH 的插件路由全过登录闸，系统浏览器没有
> DSH 的会话 Cookie，必然 401；而桌面壳只放行 https 的新窗口，页面里也没法自己
> 拉浏览器。所以只能由插件侧起一个不含登录闸的本地只读服务。
> 它不提供任何写接口，只认五个固定路径，端口随机，只绑回环。

> **为什么不开新窗口**：DSH 桌面壳的 `setWindowOpenHandler` 只放行 https，
> `http://127.0.0.1` 的 `target=_blank` 会被静默 deny——点上去毫无反应。
> 同源导航虽然放行，但会丢掉当前对话视图，所以用浮层。普通浏览器里直接访问
> `/dsh-pmem/starmap` 仍是一张独立整页，两边是同一份 `starmap.html`。

### 星图是什么

不是关系图，是一片深空：

- **半径 = 时间秩序**（早的在中心），**角度 = 分组扇区**（同一组是一条螺旋臂），
  **高度 = 盘厚**；
- 762 条日志（事件 / 召回 / 指标 / 反思 / 睡眠）散成 12,192 粒**星尘**，
  亮度按时间衰减——「那天写过很多东西」这件事在盘面上就是一团更亮的灰；
- 记忆的**等级决定亮度与星芒**，S 级才有明显的六芒衍射；
- 鼠标拖拽转、滚轮推拉、右键平移；悬停高亮相连的星，点击滑出全文；
- 底部时间轴可以拖着看它一天天长出来，拖动时有一道亮环停在"亮到哪了"的半径上；
- 顶栏的眼睛图标切记忆名：**简**（分组+实体）/ **全** / **关**，记在 localStorage；
- 偶尔一颗流星划过；选中只留邻居衰减 + 右侧详情卡（不再有选中环）；闲置 20 秒镜头自己慢慢环绕；
- 地址参数可以存书签：`?t=0.55`（停在某个时刻）、`?labels=off`、`?cruise=0`。

渲染器是手写 WebGL2（`starmap/space.js`，无第三方依赖）。画面**没有星云团**，但有一整片有结构的夜空：
天空底是一张按**世界方向**算的离屏图（1/2 分辨率），所以银河带是真的挂在天球上、会随镜头透视；
带上有核球、星云团和**纤维状尘埃带**（取 fbm 等值线，不是阈值斑）。颗粒层是 15 万颗点精灵：
星等走幂律，尺寸从 0.6 到 10 像素，尘埃遮挡在生成时就烘进了每颗星的 alpha——
暗缝同时压暗底图和颗粒，两层才对得上。近场 520 颗亮星画两遍（光晕 + 核心）制造纵深；
六个远景星系是带旋臂的四边形，不是圆斑；
记忆星点是唯一有颜色、有大小、有星芒的东西。后期链是
亮部提取 → 4 级高斯 → bloom → ACES → 暗角 → 横向色散 → 颗粒
（颗粒在色调映射**之后**——放在前面会把黑背景抬成灰）。
`starmap/ui.js` 是界面层，`starmap/space.css` 是样式——三份文件都在
`files` 里，升级插件时不会漏掉。

### 能改的参数

| 类别 | 键 | 生效 |
| --- | --- | --- |
| 运行时 | `surface` / `starmap` / `starmapLive` / `budget` / `surfaceCooldownMs` / `surfaceTimeoutMs` / `handoffTurns` / `handoffMinMs` | 保存即生效，不用重启 |
| 装配期 | `injectCard` / `tools` / `commands` / `handoff` / MCP 命令与路径 | 改 profile，重启 |

运行时值写在 `~/.dsh/pmem/runtime.json`，**只覆盖显式写过的键**（`{values, explicit}`），
所以不会把 profile 里配好的值顶掉。数值有范围守卫，越界直接夹住。

### 打不开星图时

「高级 → 自检」会列出最近 40 条请求和状态码；同一份日志也写在
`~/.dsh/pmem/web.log`。按状态码分：

- `404`：路由没注册（插件没加载 / `web: false`）；
- `401`：被 DSH 的登录闸拦了（没带登录 Cookie，回主界面刷新一次）；
- `500`：处理时抛异常（看 `starmap.html` 是否存在、`data/starmap.json` 是否可读）。

| 路由 | 作用 |
| --- | --- |
| `GET /dsh-pmem/api/status` | 库 / 卡 / 浮现命中率 / 星图 / handoff / MCP |
| `GET /dsh-pmem/api/debug` | 自检：config 实际值 + 最近请求 |
| `GET /dsh-pmem/api/wake` | 醒来卡原文 |
| `GET /dsh-pmem/api/memories?q=` | 检索 |
| `GET/POST /dsh-pmem/api/config` | 运行时开关 |
| `GET /dsh-pmem/api/starmap` | 星图 JSON（节点/连线/星尘/时间轴） |
| `GET /dsh-pmem/api/starmap/view` | 离线导出那份单文件 HTML |
| `POST /dsh-pmem/api/starmap/regen` | 重跑 `pmem star --no-png` |
| `GET /dsh-pmem/api/starmap/events` | SSE：`hello` → 变了发 `refresh` |
| `GET /dsh-pmem/starmap` | 星图页（WebGL2） |
| `GET /dsh-pmem/api/starmap/url` | 浏览器通道的地址（没起过返回 `null`） |
| `POST /dsh-pmem/api/starmap/open` | 起独立服务并拉起系统浏览器 |
| `GET /dsh-pmem/starmap/space.js` | 渲染器（白名单） |
| `GET /dsh-pmem/starmap/ui.js` | 界面层（白名单） |
| `GET /dsh-pmem/starmap/space.css` | 样式（白名单） |

控制台路由（前缀 `/dsh-pmem/api/console`，共 20 条；详见 `docs/console-design.md` 与 `docs/console-requirements.md`）：
`bootstrap`、`memories`、`memory`、`memory/feedback`、`memory/update`、`memory/delete`、`memory/batch`、`entities`、`entity`、`net/query`、`net/merge`、`backup`（GET/POST）、`backup/file`、`vector/rebuild`、`action`、`profile`、`groups`、`conflicts`、`stats`、`doctor`。

安全：只接受回环；写操作要同源 + `application/json` + body ≤ 256 KB；
再过 `connection.requestRejection`；只读白名单文件，不接受路径参数。

运行时开关写在 `~/.dsh/pmem/runtime.json`（`surface` / `starmap` / `starmapLive`），
不改 profile；profile 里关掉的能力运行时打不开。

## MCP（27 个工具）

`cordis.patch.yml` 里除了 `dsh-pmem`，还 insert 了一条官方
`@deepseek-ai/dsh-mcp-client`：

```yaml
- id: mcp-pmem
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: pmem
    transport: stdio
    command: 'python'
    args: ['-m', 'pmem.gate.mcp_server', '--db', '...']
```

- 27 个 `memory_*` 工具由 MCP 提供；原生 5 个 `pmem_*` 保留；
- MCP 起不来不影响醒来卡和原生工具（`failOnStartupError: false`）；
- `/pmem mcp` 或 `probeMcp()` 可查 20 工具/延迟；
- 包内 patch 只注册插件本体；MCP 那一段由 `pmem dsh install` 按本机路径生成
  （见上面「为什么 MCP 那一段不在包内 patch 里」）。

## 实现要点

- **不阻塞请求**：卡在插件启动时异步生成，缓存到内存和
  `~/.dsh/pmem/wake-card.txt`；`systemPrompt.context` 只读缓存。
- **子代理不继承**：`agent.session.header.origin === 'subagent'` 时返回空，子代理
  没有稳定身份，也不该拿“我们”的关系记忆。
- **Python 挂了不影响宿主**：刷新失败保留上一张卡；工具调用才报错。
- **只读注入**：`wake` 不写 `surface_log`、不改 `use_count`，所以每轮读它不会把
  记忆“用热”或写脏。
- **自动 handoff**：`agent/pre-step` 记最后一条有意义的用户消息；`turn/end`
  每 6 轮且间隔 ≥10 分钟落一次；`agent/disposed` 落最终点。写 `我们/停在哪`
  （等级 B，`--idem` 幂等），写完刷新卡。太短、纯客套、命令行不写；失败不打断会话。

## 验证

```bash
node deploy/dsh-pmem/test.mjs
node deploy/dsh-pmem/test-handoff.mjs
node deploy/dsh-pmem/test-mcp.mjs
node deploy/dsh-pmem/test-client.mjs   # 设置入口 + 侧边栏/整页面板注册契约
node deploy/dsh-pmem/test-console.mjs  # 控制台纯函数 / 视图归一化 / 写动作
node deploy/dsh-pmem/test-web.mjs      # 真 HTTP：既有 API + 控制台 20 条路由
node deploy/dsh-pmem/test-starmap.mjs  # 星图静态契约
```

第一个用假 ctx 确认：注册了 `dsh-pmem:wake` 的 system prompt、5 个工具、`/pmem` 命令，
并且能从真实库读到含 `PMEM_WAKE` 的卡。第二个用临时库跑完整 handoff 链路：
假会话 → `我们/停在哪` → 醒来卡出现「上次」。第三个用临时库确认 MCP：
`initialize` → 27 个 `memory_*` 工具 → `memory_add` → `memory_stats`。
