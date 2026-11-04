# PMem 接入 DSH：安装与回滚

## 1. 先确认 MCP server 能独立启动

```bash
cd <你的仓库路径>
$env:PYTHONPATH='<你的仓库路径>\src'
python -m pmem.gate.mcp_server --db data/pmem.db
```

进程会等在 stdin；按 Ctrl-C 退出。启动失败会直接报错，不会静默。

## 2. 注册

把 [`cordis.patch.yml`](cordis.patch.yml) 里的 `- insert:` 条目整段复制到：

```
~.dsh\profiles\desktop\cordis.patch.yml
```

要求：

- 必须在 `# === qq-bridge MCP BEGIN/END ===` 区块**之外**；
- `serverName` 用 `pmem`，不要撞 `snowluma` / `snowluma-host` / `web-search-safe`；
- `failOnStartupError: false`，PMem 挂掉不连带整条插件加载失败；
- 不要用 `instructions` 承载动态记忆。

## 3. 验证

重启 DSH 后，模型侧应出现：

```
mcp__pmem__memory_capabilities
mcp__pmem__memory_search
...
```

先调 `memory_capabilities`，确认 `server`/`schema_version` 与 `degraded`。
若工具没出现，先看 DSH 日志里的 MCP 启动错误，再检查 `PYTHONPATH` 与 `cwd`。

## 4. 回滚

删除 `- insert:` 这一整段（只删 `mcp-pmem`），重启 DSH。
如果 DSH 起不来，通常是 YAML 缩进错了：`- insert:` 顶格，`- id:` 缩进 4 空格，
`config:` 缩进 6 空格。回滚不需要动数据库。

## 5. 自动化检查

```bash
python tools/check_cordis.py deploy/cordis.patch.yml
```

它只做静态检查（serverName、条目数、qq-bridge 区块、`failOnStartupError`），
不重启 DSH，也不修改你的 profile。

## 6. 长期运行（M9，可选）

MCP 接入和夜间常驻是两件事：MCP 只在模型调用时醒；睡眠/备份/监控/月报需要一个常驻或计划任务。

前台跑（调试用，Ctrl-C 停）：

```powershell
cd <你的仓库路径>
$env:PYTHONPATH='<你的仓库路径>\src'
python -m pmem.gate.cli --db data/pmem.db daemon
```

Windows 任务计划程序（每 10 分钟 tick 一次，不做系统服务）：

```powershell
schtasks /Create /TN "PMem Daemon" /SC MINUTE /MO 10 /TR "cmd /c cd /d <你的仓库路径> && set PYTHONPATH=<你的仓库路径>\src && python -m pmem.gate.cli --db <你的仓库路径>\data\pmem.db daemon --once" /F
schtasks /Run /TN "PMem Daemon"
```

验证：

```powershell
python -m pmem.gate.cli --db data/pmem.db monitor
python -m pmem.gate.cli --db data/pmem.db backup list
```

任务计划程序里应能看到 `PMem Daemon`；`monitor` 的 `jobs` 检查应显示 5 个任务、dead 0。
删除：

```powershell
schtasks /Delete /TN "PMem Daemon" /F
```

注意：

- 只跑**一个**调度器；同时开两个 `pmem daemon` 会重复 tick；
- 第一次 tick 从下一个计划时点开始，不追装之前的空档；装好之后关机再开才会补偿；
- `pmem daemon --once` 失败会返回非 0；任务计划程序的“上次运行结果”可看；
- 备份默认在 `data/backups/`，只保留最近 14 份 manual；`daily` 快照不自动删；
- 这是本机计划任务，不是 Windows 服务：注销/关机时不会跑；接受补偿式补跑。
