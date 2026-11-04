# PMem 接入 DSH

> **如果你是 DSH 在替用户装**：先读 [`../INSTALL-FOR-DSH.md`](../INSTALL-FOR-DSH.md)
> ——要逐条问用户的问题、接入的坑、迁移与接管记忆的步骤都在那儿；本文只讲手动步骤与排障。

PMem 是**一个东西**：记忆器官（MCP 内核）+ 它在 DSH 里的外壳（插件）。
插件负责在对话里出现——每轮背景条、查询原话的工具、设置页、星图页；
MCP 是它对外的唯一门，**由插件自己拉起**。

所以安装只有一步：**装插件 = 两个都装好**。不需要单独配 MCP，也不用二选一。

---

## 1. 一条命令

```bash
pmem dsh install     # 登记 bundle + 按本机路径写 MCP 注册块（带 BEGIN/END 标记）
pmem dsh status      # 逐条体检：bundle / MCP 片段 / python / 库
pmem dsh uninstall   # 只摘注册，一条记忆都不动
```

装完重启 DSH。之后：

- 设置页会多一个 **PMem** 面板；
- 对话里它会自动带记忆（每轮一条很小的背景条，细节按需查）；
- 工具表里会出现 `mcp__pmem__*`。

**验证**（重启后）：先调 `memory_capabilities`，看 `server` / `schema_version` / `degraded`。

---

## 2. 手工安装（排障 / 非 DSH 宿主）

`pmem dsh install` 做的就是下面两件事，手工做一遍可以定位问题。

**a) 确认 MCP 能独立启动**

```bash
cd <你的仓库路径>
python -m pmem.gate.mcp_server --db data/pmem.db
```

进程会等在 stdin；按 Ctrl-C 退出。启动失败会直接报错，不会静默。

**b) 把 MCP 片段登记进 profile**

把 [`cordis.patch.yml`](cordis.patch.yml) 里的 `- insert:` 条目整段复制到
`~/.dsh/profiles/<你的 profile>/cordis.patch.yml`，然后重启 DSH。

要求：

- 必须在 `# === qq-bridge MCP BEGIN/END ===` 区块**之外**（那个区块会被别的脚本整体重写）；
- `serverName` 用 `pmem`，别撞已有的 MCP server 名；
- `failOnStartupError: false`：PMem 挂掉不连带整条插件加载失败；
- 不要把动态记忆塞进 `instructions`（那是静态提示，不是记忆）。

> 其他宿主（不是 DSH）：MCP 是同一套东西的另一张脸，注册 `pmem mcp` 即可，
> 不需要重复安装，也不需要改数据。

---

## 3. 回滚

- DSH 里：`pmem dsh uninstall`（只删注册，数据留着）；
- 手工装的：删除 `- insert:` 那一整段（只删 `mcp-pmem`），重启 DSH。

如果 DSH 起不来，通常是 YAML 缩进错了：`- insert:` 顶格，`- id:` 缩进 4 空格，
`config:` 缩进 6 空格。回滚不需要动数据库。

---

## 4. 自动化检查

```bash
python tools/check_cordis.py deploy/cordis.patch.yml
```

只做静态检查（serverName、条目数、qq-bridge 区块、`failOnStartupError`），
不重启 DSH，也不修改你的 profile。

---

## 5. 长期运行（可选）

MCP 只在模型调用时醒；睡眠 / 备份 / 监控 / 月报需要一个常驻或计划任务。

**前台跑**（调试用）：

```powershell
cd <你的仓库路径>
python -m pmem.gate.cli --db data/pmem.db daemon
```

**Windows 任务计划程序**（每 10 分钟 tick 一次，不做系统服务）：

```powershell
schtasks /Create /TN "PMem Daemon" /SC MINUTE /MO 10 /TR "cmd /c cd /d <你的仓库路径> && set PYTHONPATH=<你的仓库路径>\src && python -m pmem.gate.cli --db <你的仓库路径>\data\pmem.db daemon --once" /F
schtasks /Run /TN "PMem Daemon"
```

验证：

```powershell
python -m pmem.gate.cli --db data/pmem.db monitor
python -m pmem.gate.cli --db data/pmem.db backup list
```

注意：

- 只跑**一个**调度器（两个 `pmem daemon` 会重复 tick）；
- 第一次 tick 从下一个计划时点开始，不追装之前的空档；关机再开才会补偿；
- 备份默认在 `data/backups/`，只保留最近 14 份手动备份（日快照按保留策略轮转）；
- 这是本机计划任务，不是 Windows 服务：注销/关机时不会跑，靠补偿式补跑。
