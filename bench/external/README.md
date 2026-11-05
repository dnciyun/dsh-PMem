# 外部基准 · A 层（证据召回代理）

把 PMem 接到外部独立基准「大肥鱼 · 聊天记忆系统评测」上，**只跑免费层 A：不调任何模型**，
只回答一个问题：**给定截止点和问题，记忆系统能不能把判分证据召回来？**

这不是官方成绩，也不是「答对率」。官方要的是模型用召回到的上下文作答、再由 rubric 判分；
A 层量的是作答之前的**材料可得性**。两者相关但不等价，报告里所有数字都必须按代理指标读。

## 1. 准备外部仓库

```bash
git clone https://github.com/Ornn8/chat-memory-benchmark.git .tmp/external-bench/chat-memory-benchmark
```

先确认数据完整（离线、不联网）：

```bash
cd .tmp/external-bench/chat-memory-benchmark
python datasets/natural-timegrid-20261101-v2/read_exam.py --verify-all
# {"verified_inputs": 714, "manifest_files": 9, "network_calls": 0,
#  "status": "offline_integrity_and_input_boundary_pass"}
```

本脚本**只读**该仓库；不会改它、不会把答案喂回任何模型。

## 2. 跑 A 层

在 PMem 仓库根目录：

```bash
PYTHONPATH=src python bench/external/chat_memory_bench.py \
  --benchmark-root .tmp/external-bench/chat-memory-benchmark \
  --k 10 \
  --out .tmp/external-a/report.json \
  --md .tmp/external-a/report.md
```

常用参数：

| 参数 | 含义 |
|---|---|
| `--personas P01,P12` | 只跑部分人物（默认 6 个全跑） |
| `--limit N` | 每个人物只跑前 N 题（冒烟用） |
| `--k 10` | 检索返回条数 |
| `--out/--md` | JSON / Markdown 报告路径（默认 `.tmp/external-a/report.json/md`；`--vector` 时默认 `report-vector.json/md`） |
| `--keep` | 保留临时库到 `.tmp/external-a/run-<YYYYmmdd-HHMMSS>/dbs/`；默认用系统 temp 并自动清理 |
| `--vector` | 打开向量通道（生产对照；默认关，保持确定性主结果） |
| `--compare <report.json>` | 与一份「向量关」的结果对照，报告里生成开/关差值（用于 `--vector` 跑） |

主结果（确定性，向量关）：

```bash
PYTHONPATH=src python bench/external/chat_memory_bench.py \
  --benchmark-root .tmp/external-bench/chat-memory-benchmark --k 10 \
  --out .tmp/external-a/report.json --md .tmp/external-a/report.md
```

生产对照（向量开，并和上面的结果对照）：

```bash
PYTHONPATH=src python bench/external/chat_memory_bench.py \
  --benchmark-root .tmp/external-bench/chat-memory-benchmark --k 10 --vector \
  --compare .tmp/external-a/report.json \
  --out .tmp/external-a/report-vector.json --md .tmp/external-a/report-vector.md
```

默认全量 6 人物在本级机器上约 **15 秒**（向量关）/ **30 秒**（向量开），
远低于「全量 <10 分钟」的门槛。

## 3. 它到底怎么跑

每个人物**一个隔离临时 PMem 库**（默认显式关向量保证确定性；`--vector` 打开向量；都不碰 `data/pmem.db` 和用户配置）：

1. 按逻辑时间把历史逐轮喂进 `journal.append`（user / assistant 各一行，`ts` 用该轮逻辑时间，
   同时 `clock.set_now`）；
2. 某个**逻辑日整天完整进入历史后**跑一次 `journal.digest(day_key=...)`（无模型 `template_draft` 兜底；
   脚本会硬检查 `journal.build_writer(config) is None`，一旦发现模型 writer 就中止，不偷偷调模型）；
3. 按 `cutoff_turn` 升序回放问题：先喂到 cutoff（期间按天 digest），再把时钟拨到 `question.now`；
4. 取两路上下文：
   - `memory`：原生检索 `store.search(question, k)` 返回的条目；
   - `window`：截止点前最多 **6 个完整回合**的 user+assistant 原文（模拟「最近六轮」短期窗口）；
   - `wake`：醒来卡文本，只作为加分观测，不进主指标；
5. 证据匹配（确定性、不调模型）：对每题 `scored_fact_ids` 的每个 fact，把
   `evidence.facts[].meaning` 与 `evidence.sources[].quote` 的每一条**分别**当独立判据
   （`{meaning, quote1, quote2, ...}` 逐项算字符 bigram 覆盖），取覆盖最高的一项，
   ≥ 阈值即认为该 fact 可重建（主指标）；旧口径「meaning + 全部 quote 拼一起」保留为
   `strict_union` 保守下界。阈值默认 0.35，另报 0.25 / 0.5 敏感性。
6. 指标：fact 级命中 → 每题平均，再对题平均；分组看 `content_tags` × `interval_days` 和 persona。
   向量开/关差值由 `--compare` 生成（总表三档、按人物、按考点/时间档 combined 差）。

## 4. 指标定义

| 指标 | 定义 |
|---|---|
| `memory_recall`（主） | 每题：`scored_fact_ids` 中被「原生检索条目」覆盖 ≥ 阈值的 fact 占比；再对题平均。fact 的判据是 `{meaning, quote1, quote2, ...}` 逐项取最优（任何一项被召回即事实可重建）。 |
| `window_recall` | 同上，但上下文换成最近 6 个完整回合原文。 |
| `combined_recall` | 同上，上下文是 memory ∪ window（任一覆盖即算召回）。 |
| `memory_gain_pp` | `combined_recall − window_recall`（百分点）：长期记忆相对短期窗口挣了多少。 |
| `strict_union_recall`（保守下界） | 旧口径：把 fact 的 meaning + **全部** `sources[].quote` 拼成一个 bigram 集合再算覆盖率。长 quote 会稀释分母，所以它普遍低于主指标，作为下界保留。 |
| `memory_recall（排除 journal 原话兜底）` | 把检索结果里带 `source=journal` 的原话兜底条目剔除后的 memory_recall；更接近「巩固后的日记事实」这一层。 |
| `vector_diagnostics` | 请求/实际生效的向量后端、模型、是否降级；以及真正走到向量通道的题数与平均 vec 结果条数。 |
| 阈值敏感性 | 0.25 / 0.35 / 0.50 三档同口径重算主指标。 |

**重要**：PMem 的检索有「原话兜底」通道，会把 `journal_line` 原话直接返回。它是真实能力
（长期原话检索），但和「日记 digest 派生事实」不是一层。报告里同时给两行，就是不让原话兜底
把「巩固后的记忆」分数抬得看不清。

## 5. 已知局限（读数字前先看）

1. **代理指标**：bigram 覆盖匹配 ≠ 官方 rubric 判分，也不是答对率；措辞改写、代词指代、
   多跳推理都可能漏匹配，误判两个方向都真实存在（人工抽查见报告/Markdown）。
2. **阈值与分母都是启发式的**：0.35 是拍出来的；短 fact（如「妈妈生日三月八号」）更容易被长条目覆盖，
   长 fact 反而吃亏。主指标按「meaning / 每条 quote 分别算、取最优」；旧口径（全部 quote 拼接）
   作为 `strict_union` 下界保留。注意 evidence 的 quote 里可能混有**铺垫句**（问题/上下文），
   或一条 fact 由多句组成而命中的只覆盖其中一部分：实测低覆盖边界样本里约 3/10 属于这种假阳性，
   所以主指标更像**上界**，读「事实可重建」时不能等同于「能答对整道题」。
3. **窗口基线偏乐观**：最近 6 回合原文里常有原话；`interval_days=0` 附近的 window 分数接近复制原文。
4. **digest 走无模型模板**：`template_draft` 不是模型日记，派生事实的质量就是模板质量；
   这一层不评估「模型会不会写日记」。
5. **向量默认关闭**：主结果是「无向量、确定性」档；`--vector` 另跑生产对照，但 embedding 版本/平台
   可能让结果不完全可复现。报告 `benchmark.vector` 记录后端（onnx 模型 / 降级）、`effective`
   与 `vector_diagnostics`；`effective=false` 时**不能**把该次结果当向量成绩。
6. **日边界按本机时区**：`journal` 的 `day_key` 是本地日期。语料逻辑时间是 +08:00，
   所以「哪天算完整」按运行机器的时区切；换时区可能让个别天的 digest 归属差一天。
   匹配按题走，不受影响，但可复现性跨时区不是逐字节一致的。
7. **单次回放**：每个 persona 一个库、按 cutoff 顺序问，不模拟多会话、不模拟 DSH 的每轮浮现/handoff。
8. 本层**不调模型、不联网、不写正式库**；默认用系统 temp 并在跑完后自动删除临时库。
   `--keep` 时保留到 `.tmp/external-a/run-<YYYYmmdd-HHMMSS>/dbs/<Pxx>.db`（连同 `-wal/-shm`），
   该目录在 `.tmp/` 下、不会进仓库；不给 `--keep` 时不会留下任何库文件。

## 6. 输出

- JSON：全量结构（totals / totals_strict_union / by_threshold / by_persona / by_interval /
  by_tag / failure_samples / spotcheck_candidates / vector_diagnostics / digest 统计 / 耗时；
  `--vector --compare` 时多一块 `comparison`）。
- Markdown：总表、向量通道实际状态、阈值敏感性、按人物、向量开/关对照（如有）、
  按时间档、考点 top/bottom、失败样本、人工抽查候选与局限。
- 终端：总表 + strict_union + 向量状态 + 每个人物一行（题数、三类召回、增量、耗时）。

失败样本优先展示「window 和 memory 都没召回」的题，带 question、证据 meaning、最高分检索条目
与覆盖率，方便人工核对代理匹配是否误判。
