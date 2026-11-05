"""配置：TOML（stdlib ``tomllib``）+ 内置默认值 + 点号取值。

优先级：显式传入的 dict > 配置文件 > 默认值。
之所以用 TOML 而不是文档里写的 YAML：PyYAML 不是标准库，而 TOML 在 3.11+ 内置，
核心零依赖的承诺比"配置语法好看"更重要（见 PLAN.md §10 依赖底线）。
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "storage": {
        "path": "data/pmem.db",
    },
    "retrieval": {
        "rrf_k": 60,
        "top_k": 8,
        "token_budget": 600,
        "w_rrf": 0.55,
        "w_importance": 0.15,
        "w_confidence": 0.12,
        "w_recency": 0.10,
        "w_graph": 0.08,
        "mmr_lambda": 0.7,
        # MMR 只在前 pool 条候选里做去冗：k=8 时 64 候选全量 MMR 会吃掉 40% 检索时间，
        # 收益却只有偶尔一条重复——先按相关性取 pool，再 MMR。
        # 16（M23 提速）：32→16 实测 p50 3.81→3.37ms（−0.44ms / −13%），
        # 308 题可答/MRR/时效/抗干扰 四项一模一样。去冗只需要「最可能重复」的头部，
        # pool 再大只是多算两两 Jaccard。
        "mmr_pool": 16,
        # 词法候选池：先取多少条候选进覆盖率打分。太窄会被「导师/第一天」这种高频词
        # 的记忆挤出去（答案明明在池外），太宽则是 O(候选×词) 的覆盖率成本。
        "candidate_pool": 64,
        # 相对覆盖率下限（M28）：比最佳候选覆盖率低太多的条目，多半只是同一批人的另一件事。
        # **默认 0（关）**：实测 0.35 能把抗干扰 84.8→91.3（7→4 道），但可答率 98.6→95.5
        # （-9 题）；0.50 更极端（93.5 / 92.3）。这是一条**权衡线**，不是免费的净化，
        # 开着就是在用可答率换前 3 的干净程度——所以默认不替你选。
        "min_rel_coverage": 0.0,
        # 原文兜底：派生事实截断/改人称后丢失的细节，用 journal 原文索引补。
        # 记忆断档：最近 N 天没有新记忆时，召回空结果要说「最近没在记」，
        # 而不是只报「找不到」——前者能修，后者只会让人以为这事没发生过。
        "drought_days": 14,
        "raw_fallback": True,
        "raw_fallback_coverage": 0.30,
        "raw_fallback_max": 3,
        # 低覆盖自适应加深（0.32.0）：只在「命中太少 / 覆盖率太低」时，用一次
        # query.expand_for_memory 的确定性改写再检索一遍，把新条目填进空位。
        # 为什么不做全局 k=20：B 层 44 道 miss@10 且 gold 存在的题，k=20 只补中 2 道，
        # 却让每题都多花候选/延迟；加深只服务低覆盖这一类失败，代价按需支付。
        # 预算/条数是硬上限：主结果优先，深捞只填空位（总条数 ≤ 原 k、token ≤ budget）。
        "adaptive": {
            "enabled": True,
            # 触发线：cov_best 低于它视为「词法没对上」，才值得换说法再找一次。
            # 0.45 与 graph/raw_fallback 的弱覆盖线同一量级。
            "coverage": 0.45,
            # 第二遍放大的候选数：比首遍 k 大，才有机会把排在第 9–20 名的答案带回来。
            "k": 20,
            # 最多补几条；默认等于首遍 k（再大也会被 k 个槽位卡住）。
            "max_add": 8,
            # 槽位已满时是否允许置换（0.33.0 实现、**默认关**）。
            # 离线（714 题重放）确实只涨不跌：+1 episode / +1 line hit@10、零损失；
            # 但 B 层归因里 ② 的置换只换来 1 道、同时把 2 道时间/边界题挤坏（净 −1），
            # 而 deepen 分析给出的可救上限本来就是 0–1 道（no_slot 的 7 道 fail 里
            # gold 全在 payload）。收益撑不起「拿走用户事实」的风险，所以默认关；
            # 想开：retrieval.adaptive.evict=true（参数见下，evict_cover 建议 0.0）。
            "evict": False,
            # 头部保护的条数：前 evict_head 名永不参与置换（深捞是补充，不是重排）。
            "evict_head": 3,
            # 最多换几条：每换一条就是一次「拿走一条词法结果」的风险，宁可少换。
            "max_evict": 2,
            # 受害者门槛：覆盖率低于 evict_cover × cov_best 才算「明显更弱」。
            # 默认 0（只换覆盖率**严格为 0** 的尾巴）：外部靶场实测 0.5 会换掉一条
            # 低覆盖但仍是答案的行（P16-CASE-batch-music/d45 line hit 掉了）；
            # 收到 0 之后 hit@10 只涨不掉，而且「覆盖率 0」在 714 题里正是绝大多数
            # 尾巴的形态（deepen 归因：24 道满槽位题的尾巴覆盖率 100% 是 0）。
            "evict_cover": 0.0,
        },
        # 「我说过的话」通道（0.32.0）：assistant 原话按设计不进 memory（facts 只从
        # 用户行派生），所以「你上次答应我什么」这类问题只能回 journal 原话里找。
        # 只有查询里出现明确自指线索（你说过/你答应/你上次讲…）时才查，默认最多 2 条、
        # 放在尾巴：问用户事实的句子不能被我的旧话顶掉。
        # 0.33.0：预算只够塞一条时也允许换掉最弱的尾巴（问句已经明说是我的话，
        # 这时探针里剩下的 13–21 token 比我的原话更没价值），置换守头部保护 + 条数上限。
        # 0.34.0：多扫候选 + 确定性重排。bm25 只按「用词相近」排，而答案常常用词完全不同
        # （问「你给我想的标记」答「一盆画小雨滴」）——58 道问原话的题里 gold 落在 bm25
        # 第 3–8 名的有 20 道。扫 8 条再按「实词×2 + 双字 − 问号惩罚」重排，gold 进
        # top-2 从 33 提到 44–45（平坦曲面，不是尖峰拟合）；进 payload 的仍只有 limit 条。
        "assistant_voice": {
            "enabled": True,
            "limit": 2,
            "evict_head": 3,
            "max_evict": 2,
            # 扫多少条候选再重排（内部上限，最多 32；不影响进 payload 的条数）。
            "scan": 8,
            # 关掉就退回 0.33.0 之前的行为：只取 bm25 前 limit 条（消融/对照用）。
            "rerank": True,
            # 预留预算（0.34.0）：问「我说过什么」时，600 token 里常常只剩几十，连一条
            # 40 token 的原话都放不下（外部靶场 58 道题里只有 12 道塞得下 1 条）。
            # 这类问句允许再多花最多 120 token（实测 tag 类 gold 进 top-10 从 27 → 52，
            # 全量 prompt 只 +0.25%；ep/line hit@10 只掉 1 道，是助手原话顶掉了一条
            # 同内容记忆，不是内容丢失）。
            "reserve": 120,
        },
        # 关联扩散通道（M12）：词法命中做种子，在稀有词共现图上做 PPR。
        # 默认只做**补候选**（append）；重排（rerank）默认关：
        # 90 天语料上实测 append 净 +1 题且零损失，rerank 反而会挤掉对的（-2 题）。
        "graph": {
            "enabled": True,
            "max_df": 24,
            "neighbours": 8,
            "min_weight": 0.12,
            "hops": 2,
            "seeds": 5,
            "alpha": 0.5,
            "iterations": 3,
            "append": True,
            "min_share": 0.30,
            "max_items": 2,
            "rerank": False,
            "trigger_coverage": 0.45,
            "evict": "zero_cover",
        },
        # 实体桥（M22）：问句里认出的人名/称呼/已知实体，顺着 entity_mention 把「另一条
        # 记忆里说的事」放进候选，再从词法种子做一跳共现实体扩展（多跳）。
        # 实测（10 人 × 308 题）：可答率 96.9% → 98.6%（+5，零回退），9 道多跳漏题捞回 5 道；
        # 代价是抗干扰 89.1% → 84.8%（2 道 precision 题的干扰项被同实体邻居抬进 top3，
        # 而那两条本身是真实记忆）、检索 p50 +0.6~0.9ms。权重扫描证明没有更优档位：
        # w<1 多跳会掉，w=0.8 还会挤掉 update 题——所以不降权，就地收。
        "entity": {
            "enabled": True,
            "weight": 1.0,
            "limit": 16,
            "hop": True,
            "hop_seeds": 8,
            "hop_entities": 6,
            "hop_min_mentions": 2,
            "hop_limit": 16,
            "append": False,
            "append_max": 2,
            "evict": "score",
        },
        # 时效语义（M14）：两根时间轴不混用 + supersede 链 + 更新题新旧加权。
        "temporal": {
            "enabled": True,
            # 打分用「现实时间轴」valid_from（缺失才退回 created_at）：
            # created_at 是写入时间，回填/日记派生的旧事实用它算新旧会显得「刚发生」。
            "axis": "valid_from",
            # 旧说法命中时，沿 superseded_by 链补它的替代版本
            "chain": True,
            # 只在更新类问句里跑链（实测：不限意图时链会在别的题型里把答案顶掉）
            "chain_only_update": True,
            "chain_hops": 3,
            "chain_max_old": 60,
            "chain_max_items": 2,
            # 链补条目能进到第几名（0 = 允许排第一；更新题问的就是新说法）
            "chain_head": 0,
            "chain_score": 0.72,
            # 非更新题里的链补条目压在尾巴（实测：不压会把 p03-q17/p10-q23 的答案顶掉）
            "chain_score_noupdate": 0.58,
            "chain_step": 0.02,
            "chain_min": 0.55,
            # 更新题里给「新」的额外加权 / 给「旧说法」的惩罚（实测后定，先 0）
            "fresh_bonus": 0.08,
            # 「第一次/当初/最早」这类问句：把新近度反过来用（越老越可能是答案）
            "invert_recency_on_old": True,
            "fresh_half_life_days": 30,
            "stale_penalty": 0.0,
            # 有效期边界（M15）：valid_to 已过 = 事实失效；valid_from 在未来 = 还没生效。
            # 两者都降权，但「问过去/问将来」的句子不降（那时它们才是答案）。
            "expired_penalty": 0.15,
            "pending_penalty": 0.06,
            "valid_grace_days": 0.0,
            # 时间锚点（M28）：「十月十二号」「八月」是**哪一次**的信号，词法相似度管不了。
            # 只认明确日期/月份，模糊词（上次/那次/以前）不猜——猜错窗口比不猜更糟。
            # tolerance_days：事实常常比事情晚几天才被说出来（10-12 的事 10-19 才记下），
            # 所以给日期锚点留一点余量。
            #
            # **默认关**：M28 实测（10 人 × 308 题）开了之后可答率 98.6→98.3、抗干扰一格
            # 没动（84.8 不变）。那 7 道「相邻事实进前 3」大多没有时间锚点，有的也被
            # 同一句里的两个时间互相抵消。留着开关是为了换语料/换数据时能一键复测。
            "anchor": {
                "enabled": False,
                "bonus": 0.06,
                "tolerance_days": 7,
                "miss_factor": 1.0,
            },
        },
        # 语文范围（M15）：角色词、自称 vs 他人所属、名字+代词指代本人。
        "scope": {
            "enabled": True,
            "role_bonus": 0.035,
            "name_bonus": 0.04,
            "other_owner_penalty": 0.06,
            "owner_conflict_penalty": 0.22,
        },
        # 答案类型（M28）：问「多少/几层/哪+地点/什么时候」时，候选里得有那种东西的
        # 字面证据。只认写法明确的类型，人名不做（没 NER，硬判会误伤）。
        #
        # **默认关**：M28 实测可答率 98.6→98.3、MRR 0.815→0.812、抗干扰不变。
        # 原因看得很清楚：那 7 道题里「问数字却答了叙述」只有 2 道，而且加分只改前 3
        # 内部的顺序，改不了「干扰还在前 3」这件事。扣分档（miss_factor<1）更糟：
        # 0.85 能换来抗干扰 84.8→87.0，代价是可答率 98.6→97.6。
        "answer_type": {
            "enabled": False,
            "bonus": 0.06,
            "miss_factor": 1.0,
        },
        # 注入侧元信息（M28）：一屏里两条以上讲同一批人的记忆时，报「涉及谁」。
        "inject_meta": {
            "enabled": True,
            "entities": 3,
        },
        # 时间路由（0.36.0）：外部靶场 714 题实测——把检索预算从 k=8 放宽到 k=40，
        # 时间类题反而被更多上下文干扰（时间约定 −6、时间题 −5、相对日期 −3）。
        # 所以问句里带时间线索的题走窄配置（k≤8 / budget≤600），并把检索条目的日期
        # 补上「距今 N 天」，让模型算相对日期时有锚点（B 层 29 道相对日期失败里
        # 27 道是「捞到了但算错/不敢算」，不是检索问题）。
        # 离线估算（同一批答案按 router 回退）：+11 题（762→573 口径见靶场 REPORT §6.3）。
        # 纯确定性、无模型调用、0 成本；只压上限，不会把调用方要的 k 抬高。
        "time_router": {
            "enabled": True,
            "cap_k": 8,
            "cap_budget": 600,
            # 检索条目日期后补「（距今 N 天）」。0 天（今天）不补，避免每条都涨价。
            "age_labels": True,
        },
        # 向量通道（M13）：本地 bge-small-zh-v1.5（ONNX int8）语义检索。
        # 只在**词法覆盖率低于 trigger_coverage** 时跑（第二手段），只补候选不重排；
        # 缺模型/缺 onnxruntime 时整条通道静默降级，词法链路完全不受影响。
        "vector": {
            "enabled": True,
            "model": "bge-small-zh-v1.5",
            "model_dir": "~/.dsh/pmem/models/bge-small-zh-v1.5",
            "onnx_file": "onnx/model_int8.onnx",
            "dim": 512,
            "batch": 16,
            "max_tokens": 256,
            # 线程数 0 = 自动（min(4, CPU)）。M23 实测单次推理 1/2/4 线程 =
            # 4.08 / 2.97 / 1.95ms——短查询的线程调度开销远小于并行收益；
            # 早期写死 1 是怕和 daemon 抢 CPU 的保守值，代价是每轮多 2ms。
            "threads": 0,
            # 查询指令前缀（21 token）：bge-*-zh-v1.5 已不要求指令前缀，加上它等于把
            # 「20 字问句」撑成 37 token、单次推理 4.0 → 2.2ms。M23 起默认关。
            "query_prefix": False,
            # 准入线：余弦 < 0.55 的宁可不要。0.55 是实测的平顶区（0.55–0.58 同分），
            # 再高会把「语义对但字面不像」的题挡掉；再低会开始丢分（0.52 时 -1）。
            "min_score": 0.55,
            # 触发：coverage = 只在词法覆盖低于 trigger_coverage 时跑（第二手段）；
            # always = 每题都跑。实测两者在 90 天语料上**命中完全相同**（都是 +5/-1），
            # 但 always 的 p50 从 6.05 涨到 11.19 ms，coverage 只涨到 8.62 ms
            # （综合分 82.45 → 81.74 vs 82.52）。默认 coverage。
            # 向量通道（M30）：把余弦相似度当**一路候选**参与 RRF（而不是只在末尾补两条）。
            # 为什么：自己聊天里的问句（「许可证怎么表述的」）几乎没有稀有词，词法通道被高频
            # 双字带着跑，语义通道反而认得出「说的是同一件事」。
            # **M30 起默认开**（用户要求）。实测代价：公开语料可答 98.6→98.3（-0.3pp）、
            # MRR 0.815→0.836、p50 +2.5ms；自己聊天第一位 +3pp。
            "channel": True,
            "channel_limit": 12,
            "trigger": "coverage",
            "trigger_coverage": 0.55,
            "append": True,
            "max_items": 2,
            # score：补进来的条目用自己的相似度当前排位，与词法尾巴按分数竞争 k 个槽位
            # （前 4 名不动）。比「换覆盖率为 0 的尾巴」有效得多：漏答题的尾巴往往也
            # 有覆盖率，旧策略一条都补不进来（实测 10 次触发只补进 1 条）。
            "policy": "score",
            "head": 4,
            "limit": 8,
            "backfill": 4000,
        },
        "half_life_days": {
            "episodic": 45,
            "semantic": 365,
            "person": 365,
            "relation": 365,
            "experience": 365,
            "goal": 90,
            "meta": 365,
        },
    },
    "score": {
        "active_threshold": 0.45,
        "evidence_k": 0.45,
        "contradiction_factor": 0.5,
        "max_session_evidence": 1,
        "cap_confidence": 0.95,
    },
    "dedup": {
        "near": 0.92,
        "cluster": 0.88,
    },
    "limits": {
        "max_text_chars": 8000,
        "max_k": 50,
        "max_batch": 200,
    },
    "policy": {
        "allow_secret": False,
        "allow_cross_subject": False,
        "subject": "self",
    },
    "sanitize": {
        "enabled": True,
    },
    "net": {
        "auto_extract": True,
        "rel_dormant_days": 90,
        "rel_dormant_weight": 0.3,
        "pmi_min_count": 5,
        "pmi_min": 1.5,
        "transitive_max_new": 5000,
    },
    # 自我层（M17）：宪法 + 叙事 + 未闭环承诺。文件在 ~/.dsh/pmem/soul/，
    # 纯文本、可手改、有版本快照；存在时醒来卡优先读它。
    # 880：宪法 ~280 + 增补（5 条根 + 3 条常驻席）~480 + 叙事 ~110。
    # 780 那版装不下「根 + 叙事」：钉住的根一多，停在哪/下次/未完就整段被挤掉——
    # 醒来只知道自己是谁，不知道自己走到哪（M22 实测 5 根 + 4 段叙事 = 879）。
    # 整张卡只在会话开始/交接时注入一次，每轮走 live_budget，所以这 100 token 是一次性成本。
    # 这是**自我层卡**的上限；「关于你」画像块在它之外另算（profile_budget）。
    "identity": {
        "enabled": True,
        "dir": "~/.dsh/pmem/soul",
        "budget": 880,
        # 每轮常驻的「背景条」（M21）：整张卡只在会话开始/交接时给一次，每轮只发这一条。
        # 220 ≈ 我是谁两行 + 关系一行 + 停在哪一行 + 最新一条增补。
        "live_budget": 220,
        # 「关于你」常驻画像的预算：**在自我层卡之外**单独算（M22 改）。以前是从总预算里让，
        # 画像一出现自我层就被挤到 770，5 条根 + 叙事四段放不下（实测 879）。
        "profile_budget": 110,
        # 常驻席（M22）：[pin] 有两种。带 [根] 的永不退席；普通 [pin] 是有期限的席位，
        # 到期或超编就退席（内容留在文件里，`pmem soul show` 照常看得到）。
        # 为什么要有期限：钉住的句子只增不减，卡会被根占满，新的自我认识永远挤不进来。
        "seat_days": 30,
        "seat_max": 4,
    },
    "surface": {
        "session_budget": 220,
        "turn_budget": 80,
        "max_items": 6,
        "due_days": 30,
        "snippet_chars": 40,
    },
    # 上下文水位（M19）：不指望拿到真实 usage，用「即将发送的 messages」估算，
    # 底座补上工具 schema/系统提示这类固定开销。估少比估多危险，所以底座给得偏大。
    "context": {
        "enabled": True,
        "window_tokens": 1_000_000,
        "base_tokens": 6_000,
        "warn_at": 0.65,
        "urgent_at": 0.85,
        # 到 auto_at 不再只是提醒：直接写「停在哪」并压缩上下文（设置页可关）。
        "auto": True,
        "auto_at": 0.80,
    },
    "sleep": {
        "at": "03:30",
        "backfill_days": 7,
        "max_new": 3000,
        "budget_seconds": 60,
        "snapshot": True,
        "verify_snapshot": True,
        "stale_days": 30,
        "window_days": 90,
        "report_dir": "",
        "backup_dir": "",
        # 鲸眠（0.35.0）：夜间用一个 LLM 会话（session_id = whale-sleep:<day>）把当天原文
        # 消化成日记。默认关——不配模型时保持原来的纯本地 8 阶段；配上模型才多花钱。
        "whale": {
            "enabled": False,
            "model": "",           # 留空 = 继承 journal.model / PMEM_WHALE_MODEL / PMEM_JOURNAL_MODEL
            "max_rounds": 6,
            "max_chars": 6000,     # 当天原文进 prompt 的上限
            "transcript_dir": "~/.dsh/pmem/whale-sleep",
        },
    },
    # 日记（M10）：把当天的原话消化成一篇 episode。
    # 流水是易失原料，日记是派生结论，抽出来的事实进 memory 才会进星图。
    "journal": {
        "enabled": True,
        "model": "",               # 留空 = 用模板写；填 deepseek-chat 且有 key 才调模型
        "max_lines": 200,          # 一天最多消化多少行原文
        "keep_days": 30,           # 消化后的原文保留天数，过期整行删
        "idle_minutes": 45,        # 空闲多久算「该睡了」
        "min_lines": 3,            # 少于这个行数不值得写一篇
        "recent_days": 2,          # 醒来卡「最近」往回看几天
        "recent_items": 2,         # 「最近」最多几行
    },
    # 作答自检（0.36.0）：回答完再看一遍草稿——日期/专名这些「容易编」的东西
    # 到底在不在记忆里。确定性核对（只读 SQL，0 模型调用、0 成本）；填了 model
    # 且环境有凭证才会多跑一遍模型复核。
    "selfcheck": {
        "enabled": True,
        "model": "",               # 留空 = 只做确定性核对
        "max_dates": 40,
        "max_names": 20,
        "max_answer_chars": 4000,
        "evidence_per_issue": 3,
    },
    "evolve": {
        "auto_resolve_conflicts": True,
        "conflict_escalate_days": 14,
        "experience_match_threshold": 0.55,
        "tuning": {
            "min_labels": 50,
            "accept_gain": 0.02,
            "max_drop": 0.01,
            "days": 30,
        },
    },
    "trait": {
        "max_active": 30,
        "promote_days": 3,
        "promote_strength": 0.65,
        "min_confidence": 0.70,
        "demote_strength": 0.45,
        "stale_days": 180,
        "recency_half_life_days": 120,
        "churn_days": 30,
    },
    "reflect": {
        "freshness_days": 90,
        "recall_days": 30,
        "due_days": 30,
    },
    "runtime": {
        # M9：调度器 / 备份 / 监控 / 月报
        "enabled": True,
        "tick_seconds": 60,
        "reflect_weekday": 1,          # ISO 周一
        "reflect_at": "03:45",
        "backup_at": "04:00",
        "monitor_at": "04:30",
        "monthly_day": 1,
        "monthly_at": "04:15",
        "max_attempts": 3,
        "retry_minutes": 10,
        "stale_minutes": 120,
        "backup_keep": 14,
        # 日快照保留份数（M22）：定时备份与睡眠快照同属 daily，只留最近 7 份。
        "daily_keep": 7,
        "backup_dir": "",
        # 维护窗口（M22）：活跃时调度器不执行任务；标记文件默认在备份目录同级。
        "maintenance_minutes": 30,
        "maintenance_file": "",
        "report_dir": "",
        "max_db_mb": 2048,
        "growth_mb_30d": 256,
        "recall_drop_tolerance": 0.02,
        "sleep_stale_hours": 36,
        "backup_stale_days": 8,
        "quality_min": 50,
    },
    "gate": {
        "token": "",
        "rate_limit_per_min": 120,
        "rest_enabled": False,
        "rest_host": "127.0.0.1",
        "rest_port": 8765,
        "max_export_mb": 16,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """点号取值的配置容器：``cfg.get("retrieval.top_k", 8)``。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = _deep_merge(DEFAULTS, data or {})

    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
        data: dict[str, Any] = {}
        if path is not None:
            p = Path(path)
            if p.exists():
                with p.open("rb") as fh:
                    data = tomllib.load(fh)
        if overrides:
            data = _deep_merge(data, overrides)
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def merged(self, overrides: dict[str, Any] | None) -> Config:
        """返回叠加了覆盖值的配置（用于已生效的调参版本）。"""
        if not overrides:
            return Config(self.as_dict())
        return Config(_deep_merge(self._data, overrides))

    def __repr__(self) -> str:  # pragma: no cover
        return f"Config({sorted(self._data)})"


# ---------------------------------------------------------------------------
# 配置文件（0.36.0）：``~/.dsh/pmem/config.toml``
#
# 为什么要有默认路径：插件/宿主调 CLI 时只传 ``--db``，以前不传 ``--config`` 就等于
# 「永远只用内置默认值」——设置页里就没有任何能持久化的 PMem 级开关（比如夜间的
# 鲸眠开关）。现在 CLI 默认读这个文件；文件不存在 = 与过去完全一致（纯默认值）。
#
# 写回策略：整份重写成**点号键**（``sleep.whale.enabled = true``），只保证标量/标量
# 数组/嵌套标量表；注释不保留（这是机器管理的文件，不是手写文档）。
# ---------------------------------------------------------------------------


def default_config_path() -> Path:
    """默认配置文件位置：``$DSH_HOME/pmem/config.toml``，缺省 ``~/.dsh/pmem/config.toml``。"""
    import os

    home = os.environ.get("DSH_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".dsh"
    return base / "pmem" / "config.toml"


def read_config_file(path: str | Path | None = None) -> dict[str, Any]:
    """读配置文件；不存在/读不动都当空（和 ``Config.load`` 的语义一致）。"""
    p = Path(path) if path else default_config_path()
    try:
        if not p.exists():
            return {}
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _toml_key(part: str) -> str:
    import re

    if re.fullmatch(r"[A-Za-z0-9_-]+", part or ""):
        return part
    import json

    return json.dumps(str(part), ensure_ascii=False)


def _toml_value(value: Any) -> str:
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124
            raise ValueError("TOML 不支持 NaN/Inf")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        if any(isinstance(item, (dict, list, tuple)) for item in value):
            raise TypeError("配置文件只支持标量数组（不支持嵌套表数组）")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"配置文件不支持的值类型：{type(value).__name__}")


def _flatten_config(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    lines: list[str] = []
    for key, value in data.items():
        path = prefix + (str(key),)
        if isinstance(value, dict):
            if value:
                lines.extend(_flatten_config(value, path))
            continue
        dotted = ".".join(_toml_key(part) for part in path)
        lines.append(f"{dotted} = {_toml_value(value)}")
    return lines


def write_config_file(path: str | Path | None, data: dict[str, Any]) -> Path:
    """整份写回点号键 TOML（原子写：先写临时文件再替换）。返回写到的路径。"""
    p = Path(path) if path else default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_flatten_config(data))
    text = f"# 由 pmem config set 维护；手写注释会被重写覆盖。\n{body}\n" if body else "# 空配置\n"
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    return p


def parse_config_value(raw: str) -> Any:
    """把 CLI 的字符串值转成 TOML 标量：true/false → bool，整数/小数 → 数字，其余当字符串。"""
    text = str(raw).strip()
    low = text.lower()
    if low in ("true", "yes", "on", "1"):
        return True
    if low in ("false", "no", "off", "0"):
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def set_config_value(data: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """在嵌套 dict 里按点号路径写一个值（原地改并返回，便于链式调用）。"""
    parts = [part for part in str(dotted).split(".") if part]
    if not parts:
        raise ValueError("配置键不能为空")
    node: dict[str, Any] = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
    return data


def get_config_value(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in str(dotted).split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
