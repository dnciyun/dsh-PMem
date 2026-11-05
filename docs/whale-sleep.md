# 鲸眠（Whale Sleep）：夜间开一个 LLM 会话把记忆"想"一遍

> 0.35.0 起可选。默认仍是不花钱的纯本地 8 阶段睡眠；打开 `[sleep.whale] enabled` 之后，
> 每晚的**日记消化**交给一次真正的模型会话——名字叫「鲸眠」。

## 为什么要有它

原来的夜间是两段：

1. `journal.digest` —— 把当天流水写成日记（默认模板模式，不调模型）；
2. `sleep.orchestrator` —— 8 阶段确定性巩固（去重 / 关系 / 冲突 / 规律 / 特质 / 反思 / 快照）。

模板日记的问题是"像流水账"：它只挑几句原话、按时间串起来，不判断哪件事更值得记。
「鲸眠」把第 1 步交还给模型——就像人睡前把今天过一遍电影，但**只让它写日记**：

- **鲸眠负责"想"**：读当天原文 + 醒来卡 + 最近几天的日记，必要时用只读工具核对旧记忆，
  最后写出一页日记，并指出还没闭环的事；
- **代码负责"算"**：去重、关系、冲突、规律、特质、反思、快照仍然由 8 阶段确定性流程做。

为什么不让模型直接写 facts：facts 由日记原文派生（`journal._finish`），这样每条结论都能
回到原话。模型只写日记正文；编造的专名/自造句子会被 `validate` 整篇打回模板版——

    宁可流水账，也不要假日记。

## 一次鲸眠长什么样

```
session_id = whale-sleep:<YYYY-MM-DD>
会话名     = 鲸眠
材料       = 当天全部原文（你：/我：）+ 醒来卡 + 最近 3 篇日记
工具       = memory_search / memory_get（只读，不能绕过日记写结论）
输出       = {"title", "lines", "loops", "notes"} 一个 JSON
落库       = journal.write_episode → episode + 派生事实（带 evidence）
随后       = orchestrator 8 阶段（可 --no-sleep 跳过）
转录       = ~/.dsh/pmem/whale-sleep/<day>.jsonl
```

转录是逐行 JSON，四种记录：`meta`（会话/模型/日期）、`message`（system、原文、模型输出）、
`tool`（工具名、参数、结果）、`result`（判定、回合数、token、日记落库结果）。
所以「鲸眠那天想了什么」事后可以完整翻出来，不是黑盒。

## 两种跑法：宿主模式（不要 key） vs CLI 模式

模型的凭证在**宿主**手里，不在 PMem 手里。所以鲸眠分两种接法：

| | 宿主模式（推荐） | CLI 模式 |
| --- | --- | --- |
| 谁调模型 | 宿主（DSH 会话，就是「我」） | PMem 进程自己 |
| 要不要 API key | **不要** | 要（`PMEM_BENCH_API_KEY` / `DEEPSEEK_API_KEY`） |
| 适用 | 平时在 DSH 里用 | 无宿主的服务器 / 系统计划任务 |
| 会话与转录 | 新开的「鲸眠」DSH 会话本身就是思考过程 | 转录写在 `~/.dsh/pmem/whale-sleep/<day>.jsonl` |
| 成本 | 走宿主已有的模型额度 | 单独计费 |

**宿主模式**（0.35.2）只用两个 MCP 工具，PMem 不碰任何凭证：

```
宿主在夜里新建一个会话（名字「鲸眠」）
  → memory_whale_prep(day_key?)     拿到：当天原文 + 醒来卡 + 最近日记 + 写作规则
  → 模型在这个会话里过一遍、必要时 memory_search / memory_get 核对
  → memory_whale_finish(title, lines, loops, session_id=本会话 id)
      → journal.write_episode 落日记（校验不过回落模板）
      → 追加一条鲸眠转录（source=dsh_host，带宿主会话 id）
      → 默认接着跑固定 8 阶段
```

在 DSH 里定时做这件事：建一个每天 03:30 的定时任务，目标是**新会话**，提示词写
「你是鲸眠：先调 memory_whale_prep，读完材料再写日记，最后调 memory_whale_finish」。
本机已经建好这样一个任务（见下面的「本机配置」）。

**CLI 模式**保留给没有宿主的环境：`pmem sleep whale` / `pmem sleep big` 由 PMem 进程
直接调模型，所以要自己配 key；JSON 不合规、模型报错都会回落模板，日记不会丢。

## 怎么开

配置（`pmem.toml` 或 `--config`）：

```toml
[sleep.whale]
enabled = true
model = "deepseek-chat"        # 或 "deepseek-flash"；留空则继承 [journal] model / PMEM_WHALE_MODEL
max_rounds = 6
max_chars = 6000
transcript_dir = "~/.dsh/pmem/whale-sleep"
```

手动跑一次（默认处理昨天）：

```sh
pmem sleep whale --day 2026-11-04          # 鲸眠写日记 + 接着跑 8 阶段
pmem sleep whale --day 2026-11-04 --show   # 跑完把转录尾巴打出来
pmem sleep whale --day 2026-11-04 --dry-run  # 只跑会话，不落库、不跑 8 阶段
pmem sleep whale --no-sleep                # 只写日记，不跑 8 阶段
```

凭证沿用 PMem 的约定：`PMEM_BENCH_API_KEY` 或 `DEEPSEEK_API_KEY`（与 `pmem bench` 同一套）。
常驻调度器（daemon）里打开 `enabled` 后，`journal_tick` 到期时会先走鲸眠；原来跨天流水要等
「在场的模型自己写日记」（`journal.due` 的 `awaiting_model`），开了鲸眠就由夜里的会话接管。
**鲸眠失败（没凭证、模型报错、JSON 不合法）会自动退回模板 digest**，日记不会丢，错误写进 tick 结果。

## 成本

一夜一次会话：当天原文（截到 `max_chars`）+ 少量工具往返，量级是几 k 输入、几百输出，
按靶场价目（入 ¥1/M、出 ¥4/M）约 **¥0.01–0.03 / 夜**。默认关闭，不配模型就是 0。

## 大睡一场

`pmem sleep big` 把已有的记忆按天从头睡一遍——默认全部有原文的日子，可以选日期范围：

```sh
pmem sleep big                                    # 全部，从最早一天睡到最近一天
pmem sleep big --from 2026-05-01 --to 2026-05-31  # 只睡这一段
pmem sleep big --limit 10 --skip-digested         # 只补还没消化的 10 天
pmem sleep big --template                         # 不调模型，模板写日记（0 成本）
pmem sleep big --dry-run                          # 演练：不落库，只算
```

每天：先消化当天流水（鲸眠写日记；失败自动回落模板），再跑固定 8 阶段；
单天失败不拖垮整轮，出错记在当天结果里。重复跑是幂等的——已经消化过的天不会有第二篇日记。

## 测试

`tests/test_whale_sleep.py` 用假模型覆盖五条：正常写日记、编造专名被打回、
工具调用往返、没有原文时 `no_lines`、`--dry-run` 不落库；`tests/test_whale_host.py`
覆盖宿主模式：prep 材料、finish 落库+转录+8 阶段、编造回落、MCP 分发。
全仓测试照旧离线可跑。

## 本机配置

这台机器上：DSH 定时任务每天 03:30 新建一个标题为「鲸眠：睡前整理今天的记忆」的会话，
不配 API key，用 DSH 自己的模型；提示词里只让它调 `memory_whale_prep` / `memory_whale_finish`。
CLI 模式只在手动补睡历史时用：`pmem sleep big --template` 或（配了 key 时）`pmem sleep big`。
