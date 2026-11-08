"""Search orchestration: from a query to a budgeted, scored list.

This is the body of the old ``MemoryStore.search`` (794 lines) moved out verbatim.
It still talks to the store facade for the per-channel helpers (``store._chan_*`` /
``store._append_items`` / ...), which are one-line delegates to lexical.py and
channels.py; keeping those calls unchanged is what makes this a pure move.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ...base.clock import days_between, now
from ...base.errors import ValidationError
from .. import answer_type as answer_type_mod
from .. import groups as group_mod
from .. import journal as journal_mod
from .. import query as query_mod
from .. import scope as scope_mod
from .. import scoring
from .. import temporal as temporal_mod
from .. import vector as vector_mod
from ..text import ALIAS_TOKENS, char_ngrams, estimate_tokens, jaccard, normalize, query_variants
from . import channels as channels_mod
from . import timecue


# ---------------------------------------------------------------- P0–P3 opt-in 辅助
# 四个新能力（budget_trace / spans / aggregate / scope.project）默认全关；关时
# run_search 的行为与 0.38.0 **逐字节一致**（多出来的只有几个 cfg.get 字典点查）。
# 统计/打标都走「传 None 就立刻返回」的纯函数，主流程里不散落新分支。


def _as_float(value: Any, default: float) -> float:
    """配置数值兜底：``None``/坏字符串回默认；**0.0 是合法值**，不能用 ``or`` 吞掉。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _stage_tokens(items: list[dict[str, Any]]) -> int:
    """这一阶段新增条目占的 token（和 budget_used 同一套估算：正文 + 12）。"""
    return sum(estimate_tokens(str(item.get("text") or "")) + 12 for item in items)


def _trace_stage(
    trace: list[dict[str, Any]] | None,
    *,
    stage: str,
    offered: int,
    added: int,
    evicted: int,
    added_items: list[dict[str, Any]],
) -> None:
    """记一条预算去向：这个阶段拿进来几条、换掉几条、丢掉几条、花了多少 token。

    ``trace=None``（开关关）时立即返回，零副作用——这是「关时逐字节等价」的一部分。
    ``dropped`` 是**这个阶段自己的候选里没装进 payload 的条数**（offer - added），
    和 ``evicted``（被它挤掉的既有条目）分开记：前者说明预算/槽位不够，后者说明置换。
    """
    if trace is None:
        return
    trace.append(
        {
            "stage": stage,
            "added": int(added),
            "evicted": int(evicted),
            "dropped": max(0, int(offered) - int(added)),
            "tokens": int(_stage_tokens(added_items)),
        }
    )


def _mark_via(items: list[dict[str, Any]], stage: str) -> None:
    """给条目打来源阶段；``stage`` 为空（开关关）时立即返回，不改条目结构。

    注意：via 的开关是 ``via_on = budget_trace OR aggregate.enabled``（契约 §5.2 没有
    独立配置键；它是观测字段，不该再开一个开关）。所以**只开 spans 时条目没有 via**。
    以后若有人想「只开 spans 也按 via 分组」，要先把 ``via_on`` 扩上 spans，并给
    「只开 spans 的默认配置不出现 via」补等价性测试——否则会破坏关时逐字节等价。
    """
    if not stage:
        return
    for item in items:
        item.setdefault("via", stage)


def _attach_source_spans(store: Any, payload: list[dict[str, Any]], *, max_chars: int) -> int:
    """给带 ``source_line_id`` 的条目附 ``source_span``（只读 journal_line，不改 text）。

    0.38.0 的条目没有 ``source_line_id``：journal 来源（原话兜底/助手原话）的 ``id``
    就是 journal_line.id，所以开关打开时先把指针补齐，再按指针取原文片段。返回附了几条。
    """
    line_ids: list[str] = []
    for item in payload:
        line_id = str(item.get("source_line_id") or "")
        if not line_id and str(item.get("source") or "") == "journal":
            line_id = str(item.get("id") or "")
            if line_id:
                item["source_line_id"] = line_id
        if line_id:
            line_ids.append(line_id)
    if not line_ids:
        return 0
    excerpts = channels_mod.journal_excerpts(store, line_ids, max_chars=max_chars)
    if not excerpts:
        return 0
    attached = 0
    for item in payload:
        excerpt = excerpts.get(str(item.get("source_line_id") or ""))
        if excerpt is not None:
            item["source_span"] = excerpt
            attached += 1
    return attached


def _pack_payload(
    items: list[dict[str, Any]],
    seqs: list[int],
    *,
    budget: int,
    k: int,
    cov_by_seq: dict[int, float],
    by_seq: dict[int, dict[str, Any]],
    half_lives: Any,
    must_cover: list[str],
    type_weight: float,
    temporal_on: bool,
) -> tuple[list[dict[str, Any]], list[int], int]:
    """预算打包（P2-12，默认关）：按价值装箱决定「注入哪些 / 什么顺序」。

    价值 = coverage × (1 + type_weight × 类型覆盖率) × (0.7 + 0.3 × 新鲜度)：
    - coverage 直接用评分循环算好的 ``cov_by_seq``（不重算）；
    - 类型覆盖率 = 这条记忆对 must_cover 槽位（number/place/time）的字面证据占比；
    - 新鲜度用现实时间轴（valid_from 优先）的 recency，和打分同一把尺子。
    **只读已有数据**：不重新打分、不改 item["score"]，只是换一个装填策略。
    must_cover 为空时退化为纯价值排序（契约允许的降级路径）。
    """
    slots = [str(slot) for slot in must_cover if str(slot)]
    values: list[float] = []
    covers: list[set[str]] = []
    for seq in seqs:
        row = by_seq[seq]["__row"]
        text = str(row["text_raw"] or "")
        cover = {
            slot
            for slot in slots
            if slot in ("number", "place", "time")
            and answer_type_mod.has_evidence(text, slot)
        }
        covers.append(cover)
        cov = float(cov_by_seq.get(seq, 0.0) or 0.0)
        stamp = temporal_mod.real_axis(row) if temporal_on else str(row["created_at"] or "")
        age = days_between(stamp) if stamp else 0.0
        fresh = scoring.recency(age, float(half_lives.get(row["kind"], 365) or 365))
        hit_ratio = len(cover) / len(slots) if slots else 0.0
        values.append(cov * (1.0 + float(type_weight) * hit_ratio) * (0.7 + 0.3 * float(fresh)))
    return channels_mod.pack_items(
        items,
        seqs,
        budget=int(budget),
        k=int(k),
        values=values,
        covers=covers,
        must_cover=slots,
    )


def run_search(
    store: Any,
    query: str,
    *,
    k: int | None = None,
    budget: int | None = None,
    mode: str = "auto",
    filters: dict[str, Any] | None = None,
    client: str | None = None,
    record: bool = True,
    _deep: bool = False,
    _decay_off: bool = False,
    must_cover: list[str] | None = None,
) -> dict[str, Any]:
    """检索编排。

    ``_deep``：内部参数，低覆盖加深跑第二遍时置 True，防止无限递归；
    外部调用方（``MemoryStore.search``）不传。
    ``must_cover``：可选的「这题必须覆盖的槽位」（number/place/time…，来自 contract）。
    只有 ``retrieval.packing.enabled`` 时才被消费：装箱时先保证这些槽位有凭据条目，
    再按价值填剩余预算；没传（默认）就退化为纯价值排序。加它是因为 contract 在 SDK 侧，
    检索只在装箱时需要这几个槽位，不值得为它再开一次 DB/问句解析。
    """
    started = time.perf_counter()
    q = normalize(query or "")
    if not q:
        raise ValidationError("query 不能为空", field="query")
    if must_cover is None:
        raw_cover = filters.get("_must_cover") if isinstance(filters, dict) else None
        must_cover = list(raw_cover) if isinstance(raw_cover, (list, tuple)) else None
    k = min(k or store.cfg.get("retrieval.top_k", 8), store.cfg.get("limits.max_k", 50))
    budget = budget or store.cfg.get("retrieval.token_budget", 600)
    # 时间路由：问时间的句子（上周/昨天/10月29号/3天前…）答案通常只有一两条，
    # 大 k / 大预算只会把 600 token 摊薄、把真正那一天的那条挤到后面。
    # 这里只**本次检索**收敛，不改 cfg、不写库；默认 cap 8 条 / 600 token，
    # 可用 retrieval.time_router.{enabled,cap_k,cap_budget} 覆盖。
    router_cfg = store.cfg.get("retrieval.time_router")
    router_cfg = router_cfg if isinstance(router_cfg, dict) else {}
    time_routed = bool(router_cfg.get("enabled", True)) and timecue.is_time_question(q)
    if time_routed:
        k = min(int(k), max(1, int(router_cfg.get("cap_k", 8) or 8)))
        budget = min(int(budget), max(1, int(router_cfg.get("cap_budget", 600) or 600)))
    # ---- P0–P3 检索侧 opt-in（默认全关；关时行为与 0.38.0 逐字节一致）----
    # 这里只读配置（dict 点查），不产生 SQL；四个能力的具体代码都在自己的门内。
    trace_on = bool(store.cfg.get("retrieval.budget_trace", False))
    spans_cfg = store.cfg.get("retrieval.spans")
    spans_cfg = spans_cfg if isinstance(spans_cfg, dict) else {}
    spans_on = bool(spans_cfg.get("enabled", False))
    agg_cfg = store.cfg.get("retrieval.aggregate")
    agg_cfg = agg_cfg if isinstance(agg_cfg, dict) else {}
    aggregate_on = bool(agg_cfg.get("enabled", False))
    project_cfg = store.cfg.get("retrieval.scope.project")
    project_cfg = project_cfg if isinstance(project_cfg, dict) else {}
    project_name = str(project_cfg.get("name") or "").strip()
    project_on = bool(project_cfg.get("enabled", False)) and bool(project_name)
    # 预算打包（P2-12，默认关）：把「先到先得 + 尾部丢弃」换成按价值贪心装箱。
    # 只决定注入哪些/顺序，不改候选评分（评分早已在上一段算完）。
    packing_cfg = store.cfg.get("retrieval.packing")
    packing_cfg = packing_cfg if isinstance(packing_cfg, dict) else {}
    packing_on = bool(packing_cfg.get("enabled", False))
    must_cover_slots = [str(slot).strip() for slot in (must_cover or []) if str(slot).strip()][:4]
    budget_trace: list[dict[str, Any]] | None = [] if trace_on else None
    # item["via"] 是本次新增的结构字段：只在这两个开关任一打开时才打，
    # 否则「关时逐字节等价」不成立。
    via_on = trace_on or aggregate_on
    filters = filters or {}
    if project_on and not filters.get("group"):
        # 项目范围 = grp 路径前缀过滤（契约 §5.5）。调用方自己给了 group 就尊重调用方。
        filters = {**filters, "group": project_name}
    rrf_k = store.cfg.get("retrieval.rrf_k", 60)

    terms = query_mod.content_terms(q)
    # 语文范围配置要在召回阶段就用（角色词/名字当种子），所以提前算。
    scope_cfg = scope_mod.settings(store.cfg)
    scope_on = bool(scope_cfg.get("enabled", True))
    # 实体桥（M22）：默认关闭，由 retrieval.entity.enabled 打开；benchmark 用环境开关
    # 强制开/关（bench/longrun/systems/pmem.py），线上默认值由 Lead 决定。
    ent_raw = store.cfg.get("retrieval.entity")
    ent_cfg: dict[str, Any] = ent_raw if isinstance(ent_raw, dict) else {}
    entity_on = bool(ent_cfg.get("enabled", False))
    # 词面第二路（G2）：把「内容词」单独当一路查询，和原问句那几路 RRF 融合。
    second_raw = store.cfg.get("retrieval.second_query")
    second_cfg: dict[str, Any] = second_raw if isinstance(second_raw, dict) else {}
    second_on = bool(second_cfg.get("enabled", False))
    entity_bridge: dict[str, Any] = {}
    rawline_cfg = store.cfg.get("retrieval.rawline")
    rawline_on = isinstance(rawline_cfg, dict) and bool(rawline_cfg.get("enabled", False))
    rerank_cfg = store.cfg.get("retrieval.rerank")
    rerank_on = isinstance(rerank_cfg, dict) and bool(rerank_cfg.get("enabled", False))
    claim_cfg = store.cfg.get("claim.index")
    claim_index_on = isinstance(claim_cfg, dict) and bool(claim_cfg.get("enabled", False))
    claim_hits: list[dict[str, Any]] = []
    claim_index_mod: Any = None
    channels: dict[str, list[int]] = {}
    if mode in ("auto", "keyword"):
        # 查询改写：原句的命中永远保留，变体只往后补候选，不会挤掉原来的结果。
        content_q = " ".join(query_mod.content_terms(q))
        tok = store._chan_tokens(content_q) if content_q else []
        for seq in store._chan_tokens(q):
            if seq not in tok:
                tok.append(seq)
        # 变体里「用户」这类自称展开注入的高 df 词会把候选池灌满同主语记忆，
        # 所以**只删这个词本身**（保留「户叫」这类真正有区分度的接缝双字）。
        # 原句自己写了「用户」就按原样查。
        alias_drop = ALIAS_TOKENS if not any(word in q for word in ALIAS_TOKENS) else frozenset()
        for variant in query_variants(q):
            for seq in store._chan_tokens(variant, drop=alias_drop):
                if seq not in tok:
                    tok.append(seq)
        if tok:
            channels["tok"] = tok
        tri = store._chan_trigram(q)
        if tri:
            channels["tri"] = tri
        # 语文范围召回（M16）：问句里的名字（「X她/他」的 X）和长尾角色词（小姨/二舅）
        # 单独当一路 FTS 种子。词法主通道按 bigram 拼，名字+角色被切成接缝双字时
        # 会漏掉「小明爸爸…」这种候选，这一路按**整词**召回。
        if scope_on and bool(scope_cfg.get("recall", True)):
            scope_terms = scope_mod.recall_terms(q)
            # 只在「名字+代词」或者词法本身弱的时候才加这一路：
            # 角色词（儿子/爸爸/同事）df 不低，无条件加会把候选池灌大、检索变慢，
            # 而实测它的收益只出现在「X她/他」这类需要按名字召回的问句上。
            weak = len(channels.get("tok") or []) < k * 2
            named = bool(scope_mod.named_pronoun_subjects(q))
            if scope_terms and (named or weak):
                scope_seqs = store._chan_terms(
                    scope_terms, limit=int(scope_cfg.get("recall_limit", 16) or 16)
                )
                if scope_seqs:
                    channels["scope"] = scope_seqs
        # 实体桥（M22）：问句里出现了库里**真实存在**的实体（名字/关系称呼/别名）时，
        # 顺着 entity_mention 把「另一条记忆里说的事」放进候选；再从词法种子做一跳
        # 共现实体扩展——多跳题的典型形态是「种子里有『王子轩不交作业』，答案在
        # 『王子轩爸妈离婚』」，两条之间没有共同稀有词，只有实体能把它们连起来。
        if entity_on:
            from ...net import entities as net_entities

            try:
                entity_bridge = net_entities.memory_hits(
                    store.db.conn,
                    subject=store.subject,
                    query=q,
                    limit=int(ent_cfg.get("limit", 16) or 16),
                    seed_seqs=(channels.get("tok") or [])[
                        : int(ent_cfg.get("hop_seeds", 8) or 8)
                    ],
                    hop=bool(ent_cfg.get("hop", True)),
                    hop_entities=int(ent_cfg.get("hop_entities", 6) or 6),
                    hop_min_mentions=int(ent_cfg.get("hop_min_mentions", 2) or 2),
                    hop_limit=int(ent_cfg.get("hop_limit", 12) or 12),
                    min_mentions=int(ent_cfg.get("min_mentions", 2) or 2),
                )
            except Exception as exc:  # noqa: BLE001 实体桥是增强，挂了不能带崩检索
                entity_bridge = {"error": f"{type(exc).__name__}: {exc}"}
            if entity_bridge.get("channel"):
                channels["entity"] = list(entity_bridge["channel"])
        # 向量通道（M30）：把语义相似度**当一路候选**参与 RRF，而不是只在末尾补两条。
        # 为什么需要它：我们自己聊天里的问句（「许可证现在怎么表述的？」）几乎没有稀有词，
        # 词法通道被高频双字带着跑，长摘要反而排前面；而语义通道能认出「讲的是同一件事」。
        # 默认关：先 A/B，确认不会把公开语料的可答率带下去再开。
        vec_channel_cfg = vector_mod.settings(store.cfg)
        if bool(vec_channel_cfg.get("channel", False)) and bool(
            vec_channel_cfg.get("enabled", False)
        ):
            try:
                vector_mod.ensure_once(store.db, store.cfg)
                sims = vector_mod.search(
                    store.db,
                    store.cfg,
                    q,
                    limit=max(4, int(vec_channel_cfg.get("channel_limit", 12) or 12)),
                    min_score=float(vec_channel_cfg.get("min_score", 0.55) or 0.0),
                )
            except Exception:  # noqa: BLE001 向量挂了不能带崩词法
                sims = {}
            if sims:
                channels["vec"] = [
                    seq for seq, _score in sorted(sims.items(), key=lambda kv: -kv[1])
                ]
        # claim 通道（task-45，默认关）：claim 表的 FTS 命中作为一路参与 RRF。
        # 只读 claim 索引；claim.index.enabled=false 时这段一次都不执行（SQL 不变）。
        if claim_index_on:
            try:
                from . import claim_index as claim_index_mod_local

                claim_index_mod = claim_index_mod_local
                claim_hits = channels_mod.claim_hits(
                    store, q, limit=max(6, int(k) * 2)
                )
            except Exception:  # noqa: BLE001 claim 索引是增强，挂了不能带崩检索
                claim_hits = []
            # claim 只当索引/路由：**不进 RRF、不占 memory 候选位**；命中后回查原话，
            # 在主 payload 之后按剩余预算追加（task-48）。
        if not channels:
            # 兜底：只有词法与 trigram 都没命中才做全表 LIKE（慢，但少见）
            like = store._chan_like(q)
            if like:
                channels["like"] = like

    # 每路权重：默认全 1.0；实体通道按 retrieval.entity.weight 降权（A/B 扫参用）。
    # 只把 weights 交给 rrf，**不改各通道的 rank 列表**——rank 列表同时决定候选池
    # 里谁先进 64 条，改它副作用比调权大；调权只改融合分数。
    channel_names = list(channels)
    channel_weights: list[float] | None = None
    if "entity" in channels:
        channel_weights = [1.0] * len(channel_names)
        channel_weights[channel_names.index("entity")] = max(
            0.0, float(ent_cfg.get("weight", 1.0) or 0.0)
        )
    fused = scoring.rrf(list(channels.values()), k=rrf_k, weights=channel_weights)
    # 归一化尺子固定在**词法那几路的权重和**上（不含 entity 的权重）：这样开/关实体桥、
    # 或给实体路降权时，词法候选的 rrf_norm 一分不变，A/B 才可归因；实体路自己的贡献
    # 按 weight 线性缩放（weight<=1 时归一化值不会越过词法上限，>1 会被 _clip 截住）。
    base_channels = max(1, len(channels) - (1 if "entity" in channels else 0))
    if not fused:
        rows: list[Any] = []
    else:
        pool = max(int(store.cfg.get("retrieval.candidate_pool", 64) or 64), k * 8)
        rows = store._fetch_scored(fused, filters, limit=pool)

    candidate_sets = [query_mod.bigrams(row["text_norm"]) for row in rows]
    row_sets = {row["seq"]: s for row, s in zip(rows, candidate_sets)}
    ngram_cache: dict[int, set[str]] = {}
    half_lives = store.cfg.get("retrieval.half_life_days", {})
    row_ids = [row["id"] for row in rows]
    group_map = group_mod.group_map(store.db, "memory", row_ids)
    grade_map = group_mod.grade_map(store.db, "memory", row_ids)
    cov_best = 0.0
    cov_by_seq: dict[int, float] = {}
    pending: list[tuple[Any, float]] = []  # (row, relevance)
    term_weights = store._term_weights(terms)
    for row in rows:
        rrf_norm = scoring.rrf_normalize(fused[row["seq"]], base_channels, rrf_k)
        # 覆盖率：查询里的实词，这条记忆命中了几成。RRF 的相邻名次只差百分之一，
        # 长问句下基本是平的——所以「相关性」必须由覆盖率来定，而不是名次。
        cov = query_mod.coverage_idf_sets(
            terms, candidate_sets, row_sets[row["seq"]], term_weights or None
        )
        cov_best = max(cov_best, cov)
        cov_by_seq[row["seq"]] = cov
        relevance = (0.6 * cov + 0.4 * rrf_norm) if terms else rrf_norm
        pending.append((row, relevance))
        ngram_cache[row["seq"]] = char_ngrams(row["text_norm"][:200])

    # 两阶段检索（task-49，默认关）：对前 pool 条候选做交叉编码器重排。
    # 只在开关打开且模型/onnxruntime 齐全时才 import；缺件/异常静默走原路径。
    rerank_scores: dict[int, float] = {}
    rerank_mod: Any = None
    rerank_weight = 1.0
    if rerank_on and pending:
        try:
            from . import rerank as rerank_mod_local

            ready_ok, _why = rerank_mod_local.ready(store.cfg)
            if ready_ok:
                rerank_mod = rerank_mod_local
                rerank_settings = rerank_mod.settings(store.cfg)
                rerank_weight = float(rerank_settings.get("blend", 1.0) or 0.0)
                pool_rows = sorted(pending, key=lambda pair: -float(pair[1]))[
                    : int(rerank_settings.get("pool", 40) or 40)
                ]
                docs = [str(row["text_raw"] or "") for row, _rel in pool_rows]
                raw_scores = rerank_mod.score(q, docs, store.cfg)
                low = min(raw_scores)
                high = max(raw_scores)
                span = high - low
                for (row, _rel), raw in zip(pool_rows, raw_scores):
                    rerank_scores[int(row["seq"])] = (
                        1.0 if span <= 0 else (float(raw) - low) / span
                    )
        except Exception:  # noqa: BLE001 重排是增强：挂了就当没开
            rerank_scores = {}
            rerank_mod = None

    # 关联扩散（M12）：先用**覆盖率**选种子，再在稀有词共现图上做 PPR，
    # 把扩散质量当 ``graph_affinity`` 记进 final_score —— 只重排已有候选，不额外注入。
    # 这一格权重（w_graph）从 0.5 版起就留着没用过，现在终于有东西填了。
    spread_info: dict[str, Any] = {}
    affinity: dict[int, float] = {}
    mass_out: dict[int, float] = {}
    graph_cfg = store.cfg.get("retrieval.graph", {}) or {}
    if isinstance(graph_cfg, dict) and bool(graph_cfg.get("enabled", True)) and pending:
        # 只在**词法没把握**的时候动图：覆盖率高说明问句和某条记忆本来就对得上，
        # 这时重排只可能把对的挤掉（实测 p09-q25 就是这么丢的）。
        trigger = float(graph_cfg.get("trigger_coverage", 0.45) or 0.0)
        if cov_best >= trigger and len(rows) >= k:
            spread_info = {"skipped": "coverage", "coverage": round(cov_best, 3)}
        else:
            seeds = store._seed_weights(pending, cov_by_seq, int(graph_cfg.get("seeds", 5) or 5))
            mass = store._graph_mass(seeds, graph_cfg) if seeds else {}
            mass_out = mass
            if mass and bool(graph_cfg.get("rerank", False)):
                # 默认只补候选、不重排：实测重排会为了强邻接把对的挤到第 9 名。
                affinity = mass
            spread_info = {"seeds": len(seeds), "reached": len(mass), "coverage": round(cov_best, 3)}

    # 时效语义（M14）：两根时间轴不能混用。``created_at`` 是「这行什么时候写进库」，
    # ``valid_from`` 才是「这件事什么时候成立」——回填/日记派生出来的旧事实如果用写入
    # 时间算新旧，会显得「刚刚发生」，把真正的新说法挤下去。
    temporal_cfg = temporal_mod.settings(store.cfg)
    temporal_on = bool(temporal_cfg.get("enabled", True))
    temporal_axis = str(temporal_cfg.get("axis", "valid_from") or "valid_from")
    # 有效期边界（M15）：已失效/未生效的事实默认降权，但「问过去/问将来」的句子例外。
    expired_penalty = float(temporal_cfg.get("expired_penalty", 0.10) or 0.0)
    pending_penalty = float(temporal_cfg.get("pending_penalty", 0.06) or 0.0)
    grace_days = float(temporal_cfg.get("valid_grace_days", 0.0) or 0.0)
    past_q = temporal_on and temporal_mod.is_past_query(q)
    old_q = temporal_on and temporal_mod.is_old_query(q)
    future_q = temporal_on and temporal_mod.is_future_query(q)
    # 「问旧事」把新近度反过来用（可在配置里关掉，做消融）。
    # 但「当初…后来…」这种复合问句不算：它同时问旧事和新结果，反转会把新结果压掉
    # （实测 p09-q25「小邹当初从哪儿回来的，后来我俩怎么了」丢掉「分手」那条）。
    old_invert = (
        old_q
        and not temporal_mod.is_update_query(q)
        and bool(temporal_cfg.get("invert_recency_on_old", True))
    )
    # 语文范围（M15）：角色词、「我」vs「我的朋友」、名字+代词指代本人。
    role_bonus = float(scope_cfg.get("role_bonus", 0.035) or 0.0)
    other_owner_penalty = float(scope_cfg.get("other_owner_penalty", 0.06) or 0.0)
    owner_conflict_penalty = float(scope_cfg.get("owner_conflict_penalty", 0.08) or 0.0)
    q_self = scope_on and scope_mod.is_self_query(q)
    q_relations = scope_mod.relation_words(q) if scope_on else set()
    update_q = temporal_on and temporal_mod.is_update_query(q)
    fresh_bonus = float(temporal_cfg.get("fresh_bonus", 0.0) or 0.0) if update_q else 0.0
    stale_penalty = float(temporal_cfg.get("stale_penalty", 0.0) or 0.0) if update_q else 0.0
    fresh_hl = float(temporal_cfg.get("fresh_half_life_days", 30) or 30)
    # 时间锚点（M28）：「十月十二号」「八月」说的是**哪一次**。
    # 只认明确日期/月份，模糊词（上次/那次/以前）一律不猜——猜错窗口比不猜更糟。
    anchor_cfg = temporal_cfg.get("anchor")
    anchor_cfg = anchor_cfg if isinstance(anchor_cfg, dict) else {}
    anchor_win = (
        temporal_mod.anchor(q)
        if temporal_on and bool(anchor_cfg.get("enabled", True))
        else None
    )
    anchor_bonus = float(anchor_cfg.get("bonus", 0.06) or 0.0)
    anchor_miss = float(anchor_cfg.get("miss_factor", 1.0) or 1.0)
    anchor_tol = int(anchor_cfg.get("tolerance_days", 7) or 0)
    # 答案类型（M28）：问数字/地点/时间时，候选里没有那种东西的别抢前排。
    # person 不做字面判断（没有 NER），kind_of 给了也跳过。
    atype_cfg = store.cfg.get("retrieval.answer_type")
    atype_cfg = atype_cfg if isinstance(atype_cfg, dict) else {}
    atype = None
    if bool(atype_cfg.get("enabled", True)):
        kind = answer_type_mod.kind_of(q)
        atype = kind if kind and kind != "person" else None
    type_bonus = float(atype_cfg.get("bonus", 0.06) or 0.0)
    type_miss = float(atype_cfg.get("miss_factor", 1.0) or 1.0)
    # 软淡忘（task-40，默认关）：只影响**最终注入列表的顺序**；候选池/MMR/预算/补充路线
    # 都用未衰减分数，所以选中集合与注入条数逐题不变（714 题实测 0 差异），只有顺序变。
    decay_cfg = store.cfg.get("retrieval.decay")
    decay_on = (
        isinstance(decay_cfg, dict)
        and bool(decay_cfg.get("enabled", False))
        and not _decay_off
    )
    if decay_on:
        decay_half = _as_float(decay_cfg.get("half_life_days", 30), 30.0)
        decay_min = _as_float(decay_cfg.get("min_factor", 0.35), 0.35)
        decay_grade_weight = bool(decay_cfg.get("grade_weight", True))
        decay_use_count_weight = bool(decay_cfg.get("use_count_weight", True))
    scored: list[tuple[int, float]] = []
    stale_seqs: dict[int, str] = {}
    by_seq: dict[int, dict[str, Any]] = {}
    debug_rows: list[dict[str, Any]] | None = [] if os.environ.get("PMEM_DEBUG_SEARCH") else None
    for row, relevance in pending:
        stamp: str | None = None
        if temporal_on and temporal_axis == "valid_from":
            stamp = temporal_mod.real_axis(row)
            age = days_between(stamp) if stamp else days_between(row["created_at"])
        else:
            age = days_between(row["created_at"])
        stamp = stamp or row["created_at"]
        hl = float(half_lives.get(row["kind"], 365))
        recency_v = scoring.recency(age, hl)
        if old_invert:
            # 问「第一次/当初/最早」：新近度反过来用——越老越可能是答案。
            recency_v = 1.0 - recency_v
        score = scoring.final_score(
            rrf_norm=relevance,
            importance_v=row["importance"],
            confidence_v=row["confidence"],
            recency_v=recency_v,
            graph_affinity=min(1.0, float(affinity.get(row["seq"], 0.0))),
            w_rrf=store.cfg.get("retrieval.w_rrf", 0.55),
            w_importance=store.cfg.get("retrieval.w_importance", 0.15),
            w_confidence=store.cfg.get("retrieval.w_confidence", 0.12),
            w_recency=store.cfg.get("retrieval.w_recency", 0.10),
            w_graph=store.cfg.get("retrieval.w_graph", 0.08),
        )
        stale_kind = ""
        if (
            temporal_on
            and not past_q
            and not old_invert
            and temporal_mod.expired(row, grace_days=grace_days)
        ):
            # 有效期过了：这条事实不再成立。问「以前/当时」时不罚——那时它成立。
            stale_kind = "expired"
            score = max(0.0, score - expired_penalty)
        elif temporal_on and not future_q and temporal_mod.pending(row, grace_days=grace_days):
            # 还没生效：问「现在/最近」时它不该抢答；问「下周/计划」时反而该优先。
            stale_kind = "pending"
            score = max(0.0, score - pending_penalty)
        if stale_kind:
            stale_seqs[int(row["seq"])] = stale_kind
        # 时间锚点（M28）：问句点明了哪一天/哪个月，落在窗口里的加分，
        # 窗口外的按配置压一档（默认 1.0 = 只加分不扣分，扣分要 A/B 证明过再开）。
        anchor_state = ""
        if anchor_win is not None:
            in_win = temporal_mod.in_anchor_window(stamp, anchor_win, tolerance_days=anchor_tol)
            if in_win is True:
                anchor_state = "in"
                if anchor_bonus:
                    score = min(1.0, score + anchor_bonus)
            elif in_win is False:
                anchor_state = "out"
                if anchor_miss < 1.0:
                    score = max(0.0, score * anchor_miss)
        # 答案类型（M28）：问「多少/哪+地点/什么时候」时，候选里得有那种东西的字面证据。
        type_state = ""
        if atype:
            if answer_type_mod.has_evidence(row["text_raw"], atype):
                type_state = "hit"
                if type_bonus:
                    score = min(1.0, score + type_bonus)
            else:
                type_state = "miss"
                if type_miss < 1.0:
                    score = max(0.0, score * type_miss)
        if scope_on:
            if role_bonus:
                score += scope_mod.role_bonus(q, row["text_raw"], bonus=role_bonus)
            name_bonus = float(scope_cfg.get("name_bonus", 0.04) or 0.0)
            if name_bonus:
                score += scope_mod.name_focus(q, row["text_raw"], bonus=name_bonus)
            if other_owner_penalty and q_self and not q_relations and scope_mod.other_owner(
                row["text_raw"]
            ):
                score = max(0.0, score - other_owner_penalty)
            if owner_conflict_penalty and scope_mod.owner_conflict(row["text_raw"], q):
                score = max(0.0, score - owner_conflict_penalty)
            if scope_on and (role_bonus or other_owner_penalty or owner_conflict_penalty):
                score = min(1.0, score)
        if update_q and (fresh_bonus or stale_penalty):
            if fresh_bonus:
                score = min(1.0, score + fresh_bonus * temporal_mod.freshness(row, half_life_days=fresh_hl))
            if stale_penalty and temporal_mod.looks_stale(row["text_raw"]):
                score = max(0.0, score - stale_penalty)
        boost = group_mod.boost_for(grade_map.get(row["id"]))
        if boost:
            score = max(0.0, min(1.0, score + boost))
        if rerank_scores and int(row["seq"]) in rerank_scores:
            # blend=1 完全听重排；<1 与未归一化前的原分混合（原分本来就在 0..1）。
            rerank_value = rerank_scores[int(row["seq"])]
            score = rerank_weight * rerank_value + (1.0 - rerank_weight) * score
        if decay_on:
            idle_days = days_between(str(row["last_used_at"] or row["created_at"]))
            factor = scoring.decay_factor(
                idle_days=idle_days,
                half_life_days=decay_half,
                min_factor=decay_min,
                grade=grade_map.get(row["id"]),
                use_count=int(row["use_count"] or 0),
                grade_weight=decay_grade_weight,
                use_count_weight=decay_use_count_weight,
            )
            score = max(0.0, score * factor)
        scored.append((row["seq"], score))
        by_seq[row["seq"]] = {"__row": row}
        if debug_rows is not None:
            debug_rows.append(
                {
                    "seq": row["seq"],
                    "id": row["id"],
                    "cov": round(float(cov_by_seq.get(row["seq"], 0.0)), 3),
                    "affinity": round(float(affinity.get(row["seq"], 0.0)), 3),
                    "score": round(score, 4),
                    "anchor": anchor_state,
                    "atype": type_state,
                    "text": (row["text_raw"] or "")[:120],
                }
            )

    # 有效期边界（M15）：已失效/未生效的事实**不跟当前有效的候选抢第一**。
    # 只把它们的分数压到「最好的有效候选之下」，不直接丢弃——没有有效候选时
    # （比如整库都是历史事实）它们仍然会出现在结果里，只是排在后面。
    if temporal_on and stale_seqs:
        floor_cov = float(temporal_cfg.get("valid_cov_floor", 0.0) or 0.0)
        best_valid: float | None = None
        for seq, score in scored:
            if seq in stale_seqs:
                continue
            if float(cov_by_seq.get(seq, 0.0) or 0.0) >= floor_cov:
                best_valid = score if best_valid is None else max(best_valid, score)
        if best_valid is not None:
            capped = 0
            for idx, (seq, score) in enumerate(scored):
                if seq in stale_seqs and score >= best_valid:
                    scored[idx] = (seq, max(0.0, best_valid - 0.005))
                    capped += 1
            if capped:
                temporal_info_cap = {"stale_capped": capped, "stale": dict(stale_seqs)}
            else:
                temporal_info_cap = {"stale": dict(stale_seqs)}
        else:
            temporal_info_cap = {"stale": dict(stale_seqs)}
    else:
        temporal_info_cap = {}

    # MMR 的相似度只算一次：不缓存的话是 O(n·k²) 次 Jaccard，10k 库上就吃掉 30 ms
    sim_cache: dict[tuple[int, int], float] = {}

    def sim(a: int, b: int) -> float:
        key = (a, b) if a < b else (b, a)
        cached = sim_cache.get(key)
        if cached is None:
            cached = jaccard(ngram_cache.get(a, set()), ngram_cache.get(b, set()))
            sim_cache[key] = cached
        return cached

    # ★ 召回契约（2026-11-07 实测）：mmr_pool 是 MMR 前的**性能保护**，不是「最多给几条」的
    #   业务上限——但旧实现按绝对值截断，调用方要 40/50 条时只能拿到 ~16 条，且返回里没有
    #   任何字段说明这件事（长文档记忆实测：k=50 只回 18 条，gold 召回被这一刀压掉约 1/3）。
    #   现在保证 pool ≥ k：k≤mmr_pool 时行为与旧版逐字节一致，只有「请求数超过保护值」才放大池。
    mmr_pool = int(store.cfg.get("retrieval.mmr_pool", 32) or 0)
    pool_cap = mmr_pool
    if pool_cap and int(k) > pool_cap:
        pool_cap = int(k)
    candidate_count = len(scored)
    if pool_cap and len(scored) > pool_cap:
        # 全量 MMR 是 O(n·k²) 的 Jaccard，实测吃掉 40% 检索时间，
        # 收益只有偶尔一条近重复——先按相关性留前 pool 条，再做去冗。
        scored.sort(key=lambda pair: -pair[1])
        del scored[pool_cap:]


    picked = scoring.mmr_select(
        scored,
        sim,
        limit=max(k * 3, k),
        lam=store.cfg.get("retrieval.mmr_lambda", 0.7),
    )

    # 相对覆盖率下限（M28）：比「最佳候选」覆盖率低太多的条目，多半只是同一批人的
    # 另一件事（问「钓鱼在哪个湖」，候选里混进「府河只钓到两条小鲫鱼」）。
    # 只截**词法候选**，后面图/向量/实体补进来的多跳答案不受影响。
    floor_frac = float(store.cfg.get("retrieval.min_rel_coverage", 0.0) or 0.0)
    if floor_frac > 0 and picked and cov_by_seq:
        best_cov = max(cov_by_seq.values())
        if best_cov > 0:
            kept = [pair for pair in picked if cov_by_seq.get(pair[0], 0.0) >= floor_frac * best_cov]
            picked = kept or picked[:1]

    payload: list[dict[str, Any]] = []
    payload_seq: list[int] = []
    payload_memory_refs: dict[str, str] = {}
    for seq, score in picked:
        row = by_seq[seq]["__row"]
        payload_seq.append(seq)
        payload.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                "score": round(score, 4),
                "kind": row["kind"],
                "status": row["status"],
                "day": row["day_key"],
                # day 是发生日（0.31.0 起按 valid_from 归日）；valid_from 再给出当天内的
                # 具体时刻，渲染层才能把「昨天/今天」这类相对说法对到唯一的时间点。
                "valid_from": row["valid_from"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "provenance": row["provenance"],
                "channels": [name for name, ranks in channels.items() if seq in ranks],
                "truncated": False,
                "groups": group_map.get(row["id"], []),
                "grade": grade_map.get(row["id"]),
            }
        )
        if int(row["seq"]) in rerank_scores:
            payload[-1]["rerank_score"] = round(float(rerank_scores[int(row["seq"])]), 6)
        row_dict = row if isinstance(row, dict) else dict(row)
        if not row_dict.get("claim_id"):
            payload_memory_refs[str(row_dict.get("id") or "")] = str(
                row_dict.get("source_ref") or ""
            )

    if packing_on:
        # 打包是 opt-in 的「装填策略」：候选集与分数都不动，只换注入哪些/什么顺序。
        payload, payload_seq, used = _pack_payload(
            payload,
            payload_seq,
            budget=int(budget),
            k=int(k),
            cov_by_seq=cov_by_seq,
            by_seq=by_seq,
            half_lives=half_lives,
            must_cover=must_cover_slots,
            type_weight=float(packing_cfg.get("type_weight", 0.15) or 0.0),
            temporal_on=temporal_on,
        )
    else:
        payload, used = scoring.fit_budget(payload, budget)
    payload = payload[:k]
    payload_seq = payload_seq[: len(payload)]
    # 首遍（词法融合后的前 k 条）就是预算去向的第一站：offered 是 MMR 后进入装填的候选，
    # dropped = 预算/k 槽位没装下的部分。
    _trace_stage(
        budget_trace,
        stage="tok",
        offered=len(picked),
        added=len(payload),
        evicted=0,
        added_items=payload,
    )
    _mark_via(payload, "tok" if via_on else "")
    # 注入侧元信息（M28）：一屏里出现两条以上讲同一批人的记忆时，光看日期模型分不清
    # 「十月那次」和「八月那次」。把「涉及谁」一起给它，让它自己挑。一次 IN 查询，
    # 关掉就不查；实体表缺/坏都只是少这行备注，不影响检索。
    meta_cfg = store.cfg.get("retrieval.inject_meta")
    meta_cfg = meta_cfg if isinstance(meta_cfg, dict) else {}
    if bool(meta_cfg.get("enabled", True)) and payload_seq:
        limit_ent = max(1, int(meta_cfg.get("entities", 3) or 3))
        ent_map: dict[int, list[str]] = {}
        marks_e = ",".join("?" for _ in payload_seq)
        try:
            for ent in store.db.conn.execute(
                f"SELECT em.memory_seq AS seq, e.name AS name FROM entity_mention em "
                f"JOIN entity e ON e.id=em.entity_id "
                f"WHERE em.memory_seq IN ({marks_e}) AND e.status='active' "
                f"ORDER BY em.memory_seq, em.seq",
                tuple(payload_seq),
            ).fetchall():
                names = ent_map.setdefault(int(ent["seq"]), [])
                if ent["name"] not in names and len(names) < limit_ent:
                    names.append(ent["name"])
        except Exception:  # noqa: BLE001 实体表异常不影响检索
            ent_map = {}
        for item, seq in zip(payload, payload_seq):
            names = ent_map.get(int(seq)) or []
            if names:
                item["entities"] = names
    # 预算按**实际要注入的条目**算（fit_budget 的 used 含被 [:k] 截掉的部分）。
    spent = sum(estimate_tokens(item["text"]) + 12 for item in payload)
    # 每条已选结果现在有多「有实词依据」：补候选时只用覆盖率为 0 的尾巴换。
    payload_cov = [float(cov_by_seq.get(seq, 0.0) or 0.0) for seq in payload_seq]

    # ---- 词面第二路（G2） ---------------------------------------------
    # 「把问句压成内容词再检索一遍，把它的结果**补进**首遍」。
    # 为什么不是「多加一路通道」：通道只改融合分，候选仍按**原问句**的覆盖率排序和裁预算，
    # 实测 0 收益、还多花一次检索（g2/impl_report.md：645→640）；列表级补候选在预算内
    # 多捞回 8 道、0 损失（g2/merge_report.md 的 A2 列）。所以走老路：
    # **首遍顺序不动，只在有空位时补，满了就置换头部之后的弱尾巴**（上限 max_evict）。
    # 只跑一次：``_deep=True`` 防递归（第二遍自己不再开第二遍）。
    second_info: dict[str, Any] = {}
    # 条件触发（P1-10）：只有首遍覆盖率 cov_best 低于 trigger_coverage 才跑第二遍。
    # cov_best 是评分循环里已经算好的（不重复算）；默认 1.01 > 覆盖率上限 1.0，
    # 等价「开关一开就总是跑」，保持 0.38 语义。
    if (
        second_on
        and not _deep
        and cov_best < _as_float(second_cfg.get("trigger_coverage", 1.01), 1.01)
    ):
        content_q = " ".join(query_mod.content_terms(q))
        if content_q and normalize(content_q) != normalize(q):
            second = run_search(
                store,
                content_q,
                k=k,
                budget=budget,
                mode=mode,
                filters=filters,
                client=client,
                record=False,
                _deep=True,
            )
            second_items = list(second.get("items") or [])
            if not second_items:
                second_info = {"added": 0, "evicted": 0, "skipped": "empty"}
            else:
                second_top_cov = float((second.get("uncertainty") or {}).get("coverage") or 0.0)
                seen_ids = {str(item.get("id")) for item in payload if item.get("id")}
                added_items: list[dict[str, Any]] = []
                max_add = max(0, int(second_cfg.get("max_add", 6) or 6))
                for item in second_items:
                    if len(added_items) >= max_add:
                        break
                    item_id = str(item.get("id") or "")
                    if not item_id or item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    added_items.append(item)
                if not added_items:
                    second_info = {"added": 0, "evicted": 0, "skipped": "duplicate"}
                else:
                    payload, payload_seq, payload_cov, spent, added, evicted = store._place_items(
                        added_items,
                        payload=payload,
                        payload_seq=payload_seq,
                        payload_cov=payload_cov,
                        k=k,
                        budget=int(budget),
                        spent=spent,
                        head=max(0, int(second_cfg.get("head", 2) or 0)),
                        max_evict=max(0, int(second_cfg.get("max_evict", 3) or 0)),
                        victim_cov_max=None,
                        # 覆盖率用第二遍自己的最佳值占位（和 adaptive 同一套理由：
                        # 不让后面的「只换覆盖率 0 的尾巴」把刚补进来的条目当噪音换掉）。
                        cov_value=second_top_cov,
                    )
                    if added:
                        placed = payload[-added:]
                        id_marks = ",".join("?" for _ in placed)
                        seq_map = {
                            str(row["id"]): int(row["seq"])
                            for row in store.db.conn.execute(
                                f"SELECT id, seq FROM memory WHERE id IN ({id_marks})",
                                tuple(str(item.get("id")) for item in placed),
                            )
                        }
                        for index, item in enumerate(placed):
                            payload_seq[len(payload_seq) - added + index] = seq_map.get(
                                str(item.get("id")), 0
                            )
                    second_info = {
                        "added": added,
                        "evicted": evicted,
                        "coverage": round(second_top_cov, 3),
                    }
                    _mark_via(payload[-added:] if added else [], "second" if via_on else "")
                    _trace_stage(
                        budget_trace,
                        stage="second",
                        offered=len(second_items),
                        added=added,
                        evicted=evicted,
                        added_items=payload[-added:] if added else [],
                    )

    # ---- 低覆盖自适应加深（0.32.0） ---------------------------------------
    # 只在「命中太少（got < k）或覆盖率太低」时，用 query.expand_for_memory 的
    # 确定性改写**再检索一遍**，把首遍没出现过的条目填进空位。
    # 三条硬纪律：
    #   1. 主结果优先：不重排、不挤掉已有条目，只填空位（总量 ≤ 原 k）；
    #   2. 预算双上限：adaptive.max_add 与 budget 都不能突破；
    #   3. 只跑一次：内部参数 ``_deep=True`` 防递归。
    # 为什么不做全局 k=20：B 层实测 44 道 miss@10 里 k=20 只补中 2 道，全局放大
    # 只是每题多花钱；加深只在低覆盖这一类失败上按需支付。
    deepen_info: dict[str, Any] = {}
    deepen_top_cov = 0.0
    adaptive_cfg = store.cfg.get("retrieval.adaptive", {}) or {}
    if isinstance(adaptive_cfg, dict) and bool(adaptive_cfg.get("enabled", True)) and not _deep:
        threshold = float(adaptive_cfg.get("coverage", 0.45) or 0.0)
        got = len(payload)
        weak_items = got < k
        weak_coverage = cov_best < threshold
        if weak_items or weak_coverage:
            trigger = "few_items" if weak_items else "low_coverage"
            deepen_k = max(int(adaptive_cfg.get("k", 20) or 20), k)
            raw_max_add = adaptive_cfg.get("max_add", k)
            max_add = k if raw_max_add is None else max(0, int(raw_max_add))
            expanded = query_mod.expand_for_memory(query) or query
            evict_enabled = bool(adaptive_cfg.get("evict", True))
            if normalize(expanded) == q:
                # 没有可改写的词：不花第二次检索，元信息里如实标「没扩大」。
                deepen_info = {
                    "trigger": trigger,
                    "added": 0,
                    "expanded": False,
                    "skipped": "no_expansion",
                }
            elif got >= k and not evict_enabled:
                # 槽位已经满了、置换也关着：按旧契约只填空位，不做第二次检索。
                deepen_info = {
                    "trigger": trigger,
                    "added": 0,
                    "expanded": True,
                    "skipped": "no_slot",
                }
            else:
                deep = run_search(
                    store,
                    expanded,
                    k=deepen_k,
                    budget=budget,
                    mode=mode,
                    filters=filters,
                    client=client,
                    record=False,
                    _deep=True,
                    _decay_off=True,
                )
                deepen_top_cov = float((deep.get("uncertainty") or {}).get("coverage") or 0.0)
                seen_ids = {str(item.get("id")) for item in payload if item.get("id")}
                added_items: list[dict[str, Any]] = []
                for item in deep.get("items") or []:
                    if len(added_items) >= max_add:
                        break
                    item_id = str(item.get("id") or "")
                    if not item_id or item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                    added_items.append(item)
                # 槽位满了才允许置换：受害者只能是头部之后的弱尾巴，条数受 max_evict 卡住。
                # 只在「问句已经明说缺什么」这条通路上开，比通用补候选的 score 竞争更保守。
                if got >= k and evict_enabled:
                    evict_head = max(0, int(adaptive_cfg.get("evict_head", 3) or 0))
                    max_evict = max(0, int(adaptive_cfg.get("max_evict", 2) or 0))
                    victim_cov_max = float(adaptive_cfg.get("evict_cover", 0.5) or 0.0) * cov_best
                else:
                    evict_head = 0
                    max_evict = 0
                    victim_cov_max = None
                payload, payload_seq, payload_cov, spent, added, evicted = store._place_items(
                    added_items,
                    payload=payload,
                    payload_seq=payload_seq,
                    payload_cov=payload_cov,
                    k=k,
                    budget=int(budget),
                    spent=spent,
                    head=evict_head,
                    max_evict=max_evict,
                    victim_cov_max=victim_cov_max,
                    # 覆盖率用深捞这一遍的最佳值占位：既不冒充精确值，也不让后续
                    # "只换覆盖率 0 的尾巴" 把刚补进来的答案当成噪音换掉。
                    cov_value=deepen_top_cov,
                )
                if added:
                    placed = payload[-added:]
                    # 补 seq：后面的时效链/图补候选按 payload_seq 查重，0 会让它们判重失真。
                    id_marks = ",".join("?" for _ in placed)
                    seq_map = {
                        str(row["id"]): int(row["seq"])
                        for row in store.db.conn.execute(
                            f"SELECT id, seq FROM memory WHERE id IN ({id_marks})",
                            tuple(str(item.get("id")) for item in placed),
                        )
                    }
                    for index, item in enumerate(placed):
                        payload_seq[len(payload_seq) - added + index] = seq_map.get(
                            str(item.get("id")), 0
                        )
                    spent = sum(estimate_tokens(item["text"]) + 12 for item in payload)
                    _mark_via(payload[-added:], "deepen" if via_on else "")
                    _trace_stage(
                        budget_trace,
                        stage="deepen",
                        offered=len(added_items),
                        added=added,
                        evicted=evicted,
                        added_items=payload[-added:],
                    )
                deepen_info = {
                    "trigger": trigger,
                    "added": added,
                    "expanded": True,
                    "k": deepen_k,
                    "coverage": round(cov_best, 3),
                    **({"evicted": evicted} if evicted else {}),
                }

    graph_items: list[dict[str, Any]] = []
    if bool(graph_cfg.get("append", False)):
        graph_items, extra = store._graph_append(mass=mass_out, picked=picked, graph_cfg=graph_cfg)
        spread_info = {**spread_info, **extra}
    if graph_items:
        payload, payload_seq, payload_cov, spent, added, evicted = store._append_items(
            graph_items,
            payload=payload,
            payload_seq=payload_seq,
            payload_cov=payload_cov,
            k=k,
            budget=int(budget),
            spent=spent,
            policy=str(store.cfg.get("retrieval.graph.evict", "zero_cover") or "zero_cover"),
        )
        spread_info = {**spread_info, "added": added, "evicted": evicted}
        _mark_via(payload[-added:] if added else [], "graph" if via_on else "")
        _trace_stage(
            budget_trace,
            stage="graph",
            offered=len(graph_items),
            added=added,
            evicted=evicted,
            added_items=payload[-added:] if added else [],
        )

    # 向量通道（M13 / 0.13.0）：词法没把握时，用本地 bge 的余弦相似度补候选。
    # 三条纪律：只补不重排（前 head 名不动）、补进来按余弦和词法尾巴抢 k 个槽位、
    # 默认只在覆盖率低于阈值时跑（词法有把握时不添噪，也不烧这 4 ms）。
    vector_info: dict[str, Any] = {}
    vec_cfg = vector_mod.settings(store.cfg)
    if bool(vec_cfg.get("enabled", False)):
        # 第一次检索顺手补齐缺的向量（幂等、一次 NOT EXISTS 点查）。
        vector_mod.ensure_once(store.db, store.cfg)
        trigger = float(vec_cfg.get("trigger_coverage", 0.55) or 0.0)
        gated = str(vec_cfg.get("trigger", "coverage") or "coverage") == "coverage"
        if gated and cov_best >= trigger:
            vector_info = {"skipped": "coverage", "coverage": round(cov_best, 3)}
        else:
            vec_items, vector_info = store._vector_append(
                q=q,
                cov_by_seq=cov_by_seq,
                picked=picked,
                filters=filters,
                vec_cfg=vec_cfg,
            )
            if vec_items:
                payload, payload_seq, payload_cov, spent, added, evicted = store._append_items(
                    vec_items,
                    payload=payload,
                    payload_seq=payload_seq,
                    payload_cov=payload_cov,
                    k=k,
                    budget=int(budget),
                    spent=spent,
                    policy=str(vec_cfg.get("policy", "score") or "score"),
                    head=int(vec_cfg.get("head", 4) or 4),
                )
                vector_info = {**vector_info, "added": added, "evicted": evicted}
                _mark_via(payload[-added:] if added else [], "vector" if via_on else "")
                _trace_stage(
                    budget_trace,
                    stage="vector",
                    offered=len(vec_items),
                    added=added,
                    evicted=evicted,
                    added_items=payload[-added:] if added else [],
                )
    # 时效语义（M14）：旧说法是路标，不是答案。词法命中的旧说法已经沿
    # ``superseded_by`` 链到新版本（旧版本状态 superseded、不进候选池），
    # 这里顺着链把它补回来——「问原来那套后来怎么样了」时这才是答案。
    temporal_info: dict[str, Any] = {}
    if temporal_on and bool(temporal_cfg.get("chain", True)):
        chain_items, temporal_info = store._temporal_append(
            q=q,
            fused=fused,
            payload_seq=payload_seq,
            filters=filters,
            temporal_cfg=temporal_cfg,
            update_q=update_q,
        )
        if chain_items:
            payload, payload_seq, payload_cov, spent, added, evicted = store._append_items(
                chain_items,
                payload=payload,
                payload_seq=payload_seq,
                payload_cov=payload_cov,
                k=k,
                budget=int(budget),
                spent=spent,
                policy="score",
                head=int(temporal_cfg.get("chain_head", 4) or 0),
            )
            temporal_info = {**temporal_info, "added": added, "evicted": evicted}
            _mark_via(payload[-added:] if added else [], "temporal" if via_on else "")
            _trace_stage(
                budget_trace,
                stage="temporal",
                offered=len(chain_items),
                added=added,
                evicted=evicted,
                added_items=payload[-added:] if added else [],
            )

    # 实体桥补候选（M22）：桥到了、但词法没进前 k 的记忆，用「只换实词覆盖率 0
    # 的尾巴」的保守策略补进来。多跳题的答案与问句没有共同稀有词，词法尾巴里
    # 多是零覆盖噪声，换掉它们不会伤到真正命中的结果；补不进去就是净零，不硬挤。
    entity_append_info: dict[str, Any] = {}
    # 补候选默认关闭：A/B 实测（p01..p10 × 308 题）光靠通道进 RRF 就能捞回 5 道多跳、
    # 零 hit→miss；再补候选没有额外收益，却多一份「置换词法尾巴」的风险。留着开关。
    if entity_on and bool(ent_cfg.get("append", False)):
        cand = [
            (int(seq), float(score))
            for seq, score in (entity_bridge.get("scores") or {}).items()
            if int(seq) not in payload_seq
        ]
        cand.sort(key=lambda item: -item[1])
        cand = cand[: max(0, int(ent_cfg.get("append_max", 2) or 2))]
        if cand:
            try:
                ent_items = store._entity_append(cand, filters=filters)
            except Exception:  # noqa: BLE001 补候选失败不影响主结果
                ent_items = []
            if ent_items:
                payload, payload_seq, payload_cov, spent, added, evicted = store._append_items(
                    ent_items,
                    payload=payload,
                    payload_seq=payload_seq,
                    payload_cov=payload_cov,
                    k=k,
                    budget=int(budget),
                    spent=spent,
                    # 默认 score：桥到的多跳答案在词法尾巴上常常「有覆盖率」（只是撞得不对），
                    # 只换覆盖率 0 的尾巴换不动它；先保护前 head 名，再按分数竞争。
                    policy=str(ent_cfg.get("evict", "score") or "score"),
                    head=int(ent_cfg.get("head", 4) or 4),
                )
                entity_append_info = {"added": added, "evicted": evicted}
                _mark_via(payload[-added:] if added else [], "entity" if via_on else "")
                _trace_stage(
                    budget_trace,
                    stage="entity",
                    offered=len(ent_items),
                    added=added,
                    evicted=evicted,
                    added_items=payload[-added:] if added else [],
                )

    # 兜底彼此**互斥**：图/向量/时效链已经补进相关记忆时，原话兜底只会把它挤掉；
    # 都没给东西（冷启动/没有边/没模型）才回原话里捞。
    raw_items = store._raw_fallback(
        q=q,
        terms=terms,
        k=k,
        cov_best=cov_best,
        got=len(payload),
        have_graph=bool(spread_info.get("added"))
        or bool(vector_info.get("added"))
        or bool(temporal_info.get("added")),
    )
    raw_inserted = 0
    if raw_items:
        # 兜底也要守预算：主结果已经花掉一部分，剩下的才给原文
        remaining = max(0, int(budget) - int(spent))
        fitted: list[dict[str, Any]] = []
        for item in raw_items:
            cost = estimate_tokens(item["text"]) + 12
            if cost > remaining:
                continue
            remaining -= cost
            fitted.append(item)
        if fitted:
            old_len = len(payload)
            keep = max(1, k - len(fitted))
            payload = payload[:keep] + fitted
            # seq/覆盖率数组要和 payload 同步：原话没有 memory seq、也不参与词法覆盖率，
            # 但后面的保守置换要按位置挑「最弱的尾巴」，长度对不上会直接索引越界。
            payload_seq = payload_seq[:keep] + [0] * len(fitted)
            payload_cov = payload_cov[:keep] + [0.0] * len(fitted)
            raw_inserted = len(fitted)
            _mark_via(fitted, "raw" if via_on else "")
            _trace_stage(
                budget_trace,
                stage="raw",
                offered=len(raw_items),
                added=len(fitted),
                evicted=max(0, old_len - keep),
                added_items=fitted,
            )
    # 「我说过的话」通道（0.32.0）：原话兜底只在弱覆盖时开，而且和图/向量互斥；
    # 但「你上次答应我什么」问的不是覆盖率，是**我说过的原话**——它按设计永远不进
    # memory（facts 只从用户行派生），只能回 journal 里找。线索不命中就不查：
    # 宁可漏掉，也不拿助手的话去顶用户事实。
    assistant_info: dict[str, Any] = {}
    av_cfg = store.cfg.get("retrieval.assistant_voice", {}) or {}
    if isinstance(av_cfg, dict) and bool(av_cfg.get("enabled", True)):
        av_items = store._assistant_voice(
            q=q,
            terms=terms,
            limit=max(1, int(av_cfg.get("limit", 2) or 2)),
        )
        if av_items:
            # 原话兜底（raw_fallback）不按 role 过滤，同一条 assistant 原话可能已经进了
            # payload；这里按 id 去重，避免同一句话说两遍占预算。
            seen_ids = {str(item.get("id")) for item in payload if item.get("id")}
            fitted_items = [item for item in av_items if str(item.get("id")) not in seen_ids]
            # 置换只给「真和问句有共同实词」的原话开：线索正则放宽之后（0.33.0 补了
            # 你上回讲/你给自己/你之前…这些口语变体），最坏情况是线索命中但原话其实
            # 不相关；这时它只能占空位，不许挤掉用户的记忆。overlap≥1 ⇔ score>0.30。
            best_overlap_hit = bool(
                fitted_items and float(fitted_items[0].get("score") or 0.0) > 0.30
            )
            # 预算只够塞一条时，剩下的尾巴不该继续占着：问句已经明说是「我说过的话」，
            # 这时最弱的尾巴（探针里 P12 那几道只剩 13–21 token）比我的原话更没价值。
            # 置换仍然守纪律：头部 av_evict_head 条不动，最多换 av_max_evict 条。
            # 原话兜底已经插进来的条目不让：那是「派生事实没答上」时捞到的唯一证据，
            # 拿它给助手的话让位等于拆东墙补西墙——这时助手原话只占空位。
            # 「预留预算」（0.34.0）：问「我说过什么」时，600 token 里常常只剩几十，
            # 连一条 40 token 的原话都塞不下。允许为这类问句多花 `reserve` 个 token
            # （有上限、只在这条通路上），把「查得到」变成「放得进」。
            av_budget = int(budget) + max(0, int(av_cfg.get("reserve", 0) or 0))
            payload, payload_seq, payload_cov, spent, added, evicted = store._place_items(
                fitted_items,
                payload=payload,
                payload_seq=payload_seq,
                payload_cov=payload_cov,
                k=k,
                budget=av_budget,
                spent=spent,
                head=max(0, int(av_cfg.get("evict_head", 3) or 0)),
                max_evict=0
                if raw_inserted or not best_overlap_hit
                else max(0, int(av_cfg.get("max_evict", 2) or 0)),
            )
            if added:
                assistant_info = {
                    "added": added,
                    **({"evicted": evicted} if evicted else {}),
                }
                _mark_via(payload[-added:], "assistant" if via_on else "")
                _trace_stage(
                    budget_trace,
                    stage="assistant",
                    offered=len(fitted_items),
                    added=added,
                    evicted=evicted,
                    added_items=payload[-added:],
                )
    # 日记通道（M18）：「上周咱们都干嘛了」问的是**那几天的经过**，不是某一条事实。
    # 日记不参与候选池重排（它和记忆不是一个量纲），只在一种情况下出现：
    # 明确的时间窗 + 问经过（或词法明显没把握）→ 放最前面，它就是答案。
    # 其余情况一律不插：问事实的句子不能被日记顶掉，没时间窗的冷启动也不该倒一篇
    # 不相干的日记出来——那不是「帮你回忆」，那是噪音。
    episode_info: dict[str, Any] = {}
    ep_cfg = store.cfg.get("retrieval.episode", {}) or {}
    if isinstance(ep_cfg, dict) and bool(ep_cfg.get("enabled", True)):
        window = temporal_mod.range_window(q) if temporal_on else None
        head = False
        if window is not None:
            head = temporal_mod.asks_for_events(q) or cov_best < float(
                ep_cfg.get("gate_coverage", 0.55) or 0.0
            )
        if head:
            from datetime import timedelta

            journal_mod.ensure_episode_index(store.db, limit=64)
            since = None
            until = None
            if window is not None:
                if window.get("day"):
                    # 「11月3号那天发生了什么」：窗口就是那一天，两边都卡死。
                    since = str(window["day"])
                    until = str(window["day"])
                else:
                    since = (now() - timedelta(days=int(window["days"]))).date().isoformat()
            ep_items = journal_mod.search_episodes(
                store.db,
                query=q,
                since=since,
                until=until,
                subject=store.subject,
                limit=max(1, int(ep_cfg.get("max", 3) or 3)),
            )
            sparse_info: dict[str, Any] = {}
            # 稀疏自适应（M19）：写日记是会断的（忙、忘、没聊），窗口里空着不代表
            # 「那段时间不存在」。空窗时逐级放宽，并且**说清楚放宽到了哪**——
            # 答近的 + 说明缺口，比装作查不到有用，也比硬说「没发生什么」诚实。
            if not ep_items and window is not None and not window.get("day"):
                wider_days = max(int(window["days"]) * 4, 8)
                wider_since = (now() - timedelta(days=wider_days)).date().isoformat()
                ep_items = journal_mod.search_episodes(
                    store.db,
                    query=q,
                    since=wider_since,
                    subject=store.subject,
                    limit=max(1, int(ep_cfg.get("max", 3) or 3)),
                )
                if ep_items:
                    sparse_info = {
                        "sparse": True,
                        "widened_days": wider_days,
                        "note": f"「{window.get('label')}」这段时间没有日记，放宽到最近 {wider_days} 天才找到",
                    }
            if not ep_items:
                recent_items = journal_mod.recent(
                    store.db,
                    subject=store.subject,
                    days=365 * 3,
                    items=max(1, int(ep_cfg.get("sparse_recent", 2) or 2)),
                )
                if recent_items:
                    ep_items = [journal_mod.episode_item(row) for row in recent_items]
                    latest = str(ep_items[0].get("day") or "")
                    sparse_info = {
                        "sparse": True,
                        "latest": latest,
                        "note": f"这段时间没有日记，下面是最近的一篇（写在 {latest}）",
                    }
            if ep_items:
                # 日记放在最前面，预算不够时**让尾部的普通记忆让位**——
                # 它是这个问题要的答案，不能因为「600 token 已经被别的东西花光」就消失。
                # （第一版只用剩余预算，结果「11月3号那天发生了什么」一条日记都插不进来。）
                fitted_ep: list[dict[str, Any]] = []
                before_len = len(payload)
                for item in ep_items:
                    need = estimate_tokens(str(item.get("text") or "")) + 12
                    while payload and int(spent) + need > int(budget):
                        dropped = payload.pop()
                        if payload_seq:
                            payload_seq.pop()
                        spent = max(0, int(spent) - (estimate_tokens(str(dropped.get("text") or "")) + 12))
                    if int(spent) + need > int(budget):
                        break
                    fitted_ep.append(item)
                    spent = int(spent) + need
                if fitted_ep:
                    payload = fitted_ep + payload[: max(0, k - len(fitted_ep))]
                    payload_seq = [0] * len(fitted_ep) + payload_seq
                    episode_info = {
                        "items": len(fitted_ep),
                        "at": "head",
                        "window": (window or {}).get("label"),
                        **sparse_info,
                    }
                    used = sum(estimate_tokens(item["text"]) + 12 for item in payload)
                    _mark_via(payload[: len(fitted_ep)], "episode" if via_on else "")
                    _trace_stage(
                        budget_trace,
                        stage="episode",
                        offered=len(ep_items),
                        added=len(fitted_ep),
                        evicted=max(0, before_len + len(fitted_ep) - len(payload)),
                        added_items=payload[: len(fitted_ep)],
                    )

    # ---- 同主体枚举（P1-7，默认关）---------------------------------------
    # 问「各/分别/哪几/两类」时答案是多条并列事实，top-k 只带回最像的一条。
    # 用 query.plan 的 subject_terms 把同主体的其它事实取回来，**只填空位**：
    # 已有顺序一条不动（head=k、max_evict=0），装不下就丢——聚合是补，不是重排。
    #
    # **「只填空位」是硬语义，不是实现细节**：k 个槽位已经装满时（默认 k=8；bench 是
    # k=40，几乎总是满的），aggregate 一条都不添，也就是这条能力在 bench 口径下基本
    # inert。这是有意的——G2 已证明「置换弱尾巴」会挤掉本来对的答案，我们不擅自开；
    # 要让它参与置换，得另开开关并先证明不掉分（本波不做，不计分）。
    #
    # query.plan 由 branch-auditor 并行实现：还没落地/抛异常就优雅跳过（测试先绿）。
    aggregate_info: dict[str, Any] = {}
    if aggregate_on:
        plan = None
        plan_fn = getattr(query_mod, "plan", None)
        if callable(plan_fn):
            try:
                plan = plan_fn(q)
            except Exception:  # noqa: BLE001 查询计划是增强，挂了不能带崩检索
                plan = None
        subject_terms: list[str] = []
        if isinstance(plan, dict) and str(plan.get("kind") or "") == "aggregate":
            raw_terms = plan.get("subject_terms")
            if isinstance(raw_terms, (list, tuple)):
                subject_terms = [str(term) for term in raw_terms if str(term).strip()]
        if subject_terms:
            max_items = max(0, int(agg_cfg.get("max_items", 10) or 0))
            offered = (
                channels_mod.aggregate_append(
                    store, subject_terms=subject_terms, filters=filters, limit=max_items
                )
                if max_items
                else []
            )
            if offered:
                shown_ids = {str(item.get("id")) for item in payload if item.get("id")}
                offered = [item for item in offered if str(item.get("id")) not in shown_ids]
            if offered:
                payload, payload_seq, payload_cov, spent, added, evicted = store._place_items(
                    offered,
                    payload=payload,
                    payload_seq=payload_seq,
                    payload_cov=payload_cov,
                    k=k,
                    budget=int(budget),
                    spent=spent,
                    head=k,  # 已有条目全受保护 → 只填空位，不置换
                    max_evict=0,
                )
                if added:
                    _mark_via(payload[-added:], "aggregate" if via_on else "")
                    _trace_stage(
                        budget_trace,
                        stage="aggregate",
                        offered=len(offered),
                        added=added,
                        evicted=evicted,
                        added_items=payload[-added:],
                    )
                    aggregate_info = {"added": added, "terms": subject_terms[:4]}

    # 补候选之后重算一次注入量：budget_used 是给人和 benchmark 看的，不能停在补之前。
    if (
        vector_info.get("added")
        or spread_info.get("added")
        or temporal_info.get("added")
        or entity_append_info.get("added")
        or deepen_info.get("added")
        or assistant_info.get("added")
        or raw_inserted
        or aggregate_info.get("added")
    ):
        used = sum(estimate_tokens(item["text"]) + 12 for item in payload)
    # 软淡忘（task-40）：分数参与排序/选择，但**注入条数必须与关时一致**。
    # 排序变了，尾部条数会因 token 预算边界抖动（实测 ±1~2 条）；这里用同一检索的
    # 「关时版本」把条数对齐：多了截尾、少了从关时结果补回（只读一次，不写库）。
    # 候选池不变（上面各通道/图/实体补候选都用未改的候选），这里只修输出条数。
    if decay_on and payload:
        probe = run_search(
            store,
            query,
            k=k,
            budget=budget,
            mode=mode,
            filters=filters,
            client=client,
            record=False,
            _decay_off=True,
            must_cover=must_cover,
        )
        target_items = list(probe.get("items") or [])
        target = len(target_items)
        if target:
            if len(payload) > target:
                payload = payload[:target]
                if len(payload_seq) >= target:
                    payload_seq = payload_seq[:target]
                if len(payload_cov) >= target:
                    payload_cov = payload_cov[:target]
            elif len(payload) < target:
                shown_ids = {str(item.get("id")) for item in payload if item.get("id")}
                for extra in target_items:
                    if len(payload) >= target:
                        break
                    extra_id = str(extra.get("id") or "")
                    if extra_id and extra_id in shown_ids:
                        continue
                    payload.append(dict(extra))
                    payload_seq.append(-1)
                    payload_cov.append(0.0)
                    if extra_id:
                        shown_ids.add(extra_id)
            used = sum(estimate_tokens(item["text"]) + 12 for item in payload)

    # 原话通道（task-53，默认关）：正常 payload 打包/裁剪之后**只追加**。
    # 为什么放这里：原有条目顺序/数量必须不变（A/B 时 B 的上下文是 A 的超集），
    # 因此不能参与 RRF/排序；只从 journal_line 补「字面分 × 时间衰减」的原话，
    # 追加前去重（12 字窗口）并受 budget_share 字符上限与总 token 预算约束。
    if rawline_on:
        try:
            from . import rawline as rawline_mod

            rawline_candidates = rawline_mod.candidates(
                store, q, cutoff_ts=now().isoformat(), cfg=store.cfg
            )
            rawline_picked = rawline_mod.pick(
                payload,
                q,
                cfg=store.cfg,
                candidates=rawline_candidates,
                budget=int(budget),
            )
        except Exception:  # noqa: BLE001 原话通道是增强：挂了不能带崩检索
            rawline_picked = []
        if rawline_picked:
            used = sum(estimate_tokens(item["text"]) + 12 for item in payload)
            added_raw = 0
            for item in rawline_picked:
                cost = estimate_tokens(item["text"]) + 12
                if used + cost > int(budget):
                    break
                payload.append(item)
                payload_seq.append(-(3000 + added_raw))
                payload_cov.append(0.0)
                added_raw += 1
                used += cost

    # claim 命中回查原话（task-48）：claim 只当索引/路由，不跟 memory 抢主排序。
    # 规则：①只用剩余预算；②同 source_line 的 memory 已进 top-k 就不重复；
    # ③claim 条目总字符 ≤ 预算 25%；④不截 memory 条数。
    if claim_index_on and claim_hits and claim_index_mod is not None:
        used = sum(estimate_tokens(item["text"]) + 12 for item in payload)
        claim_char_cap = int(float(budget) * 0.25)
        claim_chars = 0
        shown_claim_ids = {
            str(item.get("claim_id")) for item in payload if item.get("claim_id")
        }
        added_claims = 0
        for claim in claim_hits:
            claim_id = str(claim.get("id") or "")
            if not claim_id or claim_id in shown_claim_ids:
                continue
            if claim_index_mod.source_covered(claim, payload, payload_memory_refs):
                continue
            item = claim_index_mod.payload_item(claim, use_original=True)
            text = str(item.get("text") or "")
            cost = estimate_tokens(text) + 12
            if claim_chars + len(text) > claim_char_cap:
                break
            if used + cost > int(budget):
                break
            payload.append(item)
            payload_seq.append(-(2000 + added_claims))
            payload_cov.append(0.0)
            used += cost
            claim_chars += len(text)
            added_claims += 1
            shown_claim_ids.add(claim_id)
        # boundary claim（task-45）：claim.index 打开时**无条件注入**，最多 boundary_limit 条，
    # 按与当前话题的 token 重叠排序；放在首遍头部（宁可挤掉一条普通记忆也不漏边界）。
    if claim_index_on:
        try:
            if claim_index_mod is None:
                from . import claim_index as claim_index_mod
            boundary_limit = max(
                0, int(_as_float(store.cfg.get("claim.inject.boundary_limit", 3), 3))
            )
            boundaries = (
                claim_index_mod.boundary_claims(
                    store.db, q, subject=store.subject, limit=boundary_limit
                )
                if boundary_limit
                else []
            )
        except Exception:  # noqa: BLE001
            boundaries = []
        if boundaries:
            shown_claims = {
                str(item.get("claim_id")) for item in payload if item.get("claim_id")
            }
            boundary_items: list[dict[str, Any]] = []
            boundary_seqs: list[int] = []
            for offset, claim in enumerate(boundaries):
                claim_id = str(claim.get("id") or "")
                if not claim_id or claim_id in shown_claims:
                    continue
                boundary_items.append(claim_index_mod.payload_item(claim))
                boundary_seqs.append(-(1000 + offset))
                shown_claims.add(claim_id)
            if boundary_items:
                # 只追加，不占 lexical 命中的位置（Lead 实测：放头部会把正常证据挤掉）。
                # 同一条预算线：预算不够时宁可少注入 boundary，也不删已有 lexical 条目。
                payload = list(payload) + boundary_items
                payload_seq = list(payload_seq) + boundary_seqs
                payload_cov = list(payload_cov) + [0.0] * len(boundary_items)
                payload, used = scoring.fit_budget(payload, int(budget))
                payload_seq = payload_seq[: len(payload)]
                payload_cov = payload_cov[: len(payload)]

    # 「不确定就说出来」（M18）：把「这次召回有多有把握」算成确定性信号，交给渲染层说人话。
    # 为什么不靠自觉：长对话里最先失效的就是自觉；数字先算出来，才有东西可说。
    doubt_cfg = store.cfg.get("retrieval.doubt", {}) or {}
    uncertainty: dict[str, Any] = {}
    if isinstance(doubt_cfg, dict) and bool(doubt_cfg.get("enabled", True)):
        first = payload[0] if payload else {}
        top_cov = float(cov_by_seq.get(payload_seq[0], 0.0)) if payload_seq else 0.0
        # 加深补进来的条目没有单独覆盖率（深捞只报最佳），首条就是它时用深捞覆盖率占位；
        # 这样「加深之后还是没把握」和「加深之后找到高覆盖答案」在 uncertainty 里分得开。
        if top_cov == 0.0 and deepen_info.get("added") and deepen_top_cov:
            top_cov = float(deepen_top_cov)
        # 分差只在**同一种东西**之间比：日记/原话的分数是另算的，混进来会永远报「咬得紧」。
        mem_scores = [
            float(item.get("score") or 0.0)
            for item in payload
            if str(item.get("source") or "") not in ("episode", "journal")
        ]
        margin = (mem_scores[0] - mem_scores[1]) if len(mem_scores) > 1 else 1.0
        reasons: list[str] = []
        drought_age = None
        if not payload:
            reasons.append("empty")
            # 记忆断档（M19 补）：一条都没召回时，分清楚「这事没记过」和
            # 「最近根本没在记」——后者更该被说出来，因为它能修。
            drought_days = int(store.cfg.get("retrieval.drought_days", 14) or 0)
            last = store.last_memory_at()
            if drought_days and last:
                age = days_between(last)
                if age is not None and age >= drought_days:
                    reasons.append("drought")
                    drought_age = int(age)
        else:
            # 只有「问经过 + 头一条就是日记」时覆盖率才不适用（日记没有这把尺子）；
            # 别的「头一条碰巧是日记/原话」不能免检摘要——问事实却只捞到一篇日记，
            # 恰恰是最该说「不确定」的时候。
            asks_events = temporal_mod.asks_for_events(q) if temporal_on else False
            is_overview = bool(episode_info) and asks_events and str(first.get("source") or "") == "episode"
            if not is_overview and top_cov < float(doubt_cfg.get("coverage", 0.35) or 0.0):
                reasons.append("coverage")
            if len(mem_scores) > 1 and margin < float(doubt_cfg.get("margin", 0.02) or 0.0):
                reasons.append("close")
        uncertainty = {
            "weak": bool(reasons),
            "reasons": reasons,
            "coverage": round(top_cov, 3),
            "margin": round(margin, 3),
            **({"drought_days": drought_age} if "drought" in reasons else {}),
        }
    # 统一带上 status：补候选那几路（图/向量/时效链/原话）各自造的条目没这个字段，
    # 而「候选 / 生效 / 休眠」正是上层判断可信度要看的。一次 IN 查询补齐。
    if payload:
        ids = [str(item.get("id")) for item in payload if item.get("id")]
        if ids:
            marks_s = ",".join("?" for _ in ids)
            status_map = {
                row["id"]: row["status"]
                for row in store.db.conn.execute(
                    f"SELECT id, status FROM memory WHERE id IN ({marks_s})", tuple(ids)
                )
            }
            for item in payload:
                if not item.get("status"):
                    item["status"] = status_map.get(str(item.get("id")))

    # 证据指针展开（P2-13，默认关）：只读 journal_line，给条目附 source_span，
    # 不改 item["text"]（正文属于记忆层，不能拿原文替换）。关时一次 SQL 都不发。
    if spans_on:
        _attach_source_spans(
            store,
            payload,
            max_chars=max(1, int(spans_cfg.get("max_chars", 120) or 120)),
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    entity_info: dict[str, Any] = {}
    if entity_on:
        entity_info = {
            "query": entity_bridge.get("query_entities") or [],
            "hop": entity_bridge.get("hop_entities") or [],
            "query_seqs": len(entity_bridge.get("query_seqs") or []),
            "hop_seqs": len(entity_bridge.get("hop_seqs") or []),
            **entity_append_info,
        }
        if entity_bridge.get("error"):
            entity_info["error"] = entity_bridge["error"]

    if record:
        store._record_search(
            query=q,
            mode=mode,
            channels=channels,
            payload=payload,
            used=used,
            latency_ms=latency_ms,
            client=client,
            uncertainty=uncertainty,
        )
    return {
        "items": payload,
        "total_candidates": len(fused),
        "budget_used": used,
        "latency_ms": latency_ms,
        # 时间路由（只增字段，不改原有键）：调用方/评测能看到这次到底有没有被收敛，
        # 以及实际生效的 k/budget——不然「为什么只回了 8 条」只能靠猜。
        "meta": {
            "time_router": {
                "routed": time_routed,
                "k": int(k),
                "budget": int(budget),
            },
            # ★ 召回契约可观测（2026-11-07）：请求 k、候选数、MMR 保护池与最终条数同屏，
            #   这样「要了 50 条只回 18 条」不再是无迹可循的静默降级。
            "retrieval": {
                "k": int(k),
                "candidates": int(candidate_count),
                "mmr_pool": int(mmr_pool),
                "pool_cap": int(pool_cap),
                "returned": len(payload),
            },
        },
        "channels": {name: len(ranks) for name, ranks in channels.items()},
        **({"budget_trace": budget_trace} if trace_on else {}),
        "spread": spread_info,
        **({"episode": episode_info} if episode_info else {}),
        **({"uncertainty": uncertainty} if uncertainty else {}),
        **({"vector": vector_info} if vector_info else {}),
        **({"entity": entity_info} if entity_info else {}),
        **({"deepen": deepen_info} if deepen_info else {}),
        **({"second_query": second_info} if second_info else {}),
        **({"assistant_voice": assistant_info} if assistant_info else {}),
        **({"temporal": {**temporal_info, **temporal_info_cap}} if (temporal_info or temporal_info_cap) else {}),
        **(
            {
                "debug": {
                    "candidates": sorted(debug_rows, key=lambda r: -r["score"])[:200],
                    "pool": len(rows),
                }
            }
            if debug_rows is not None
            else {}
        ),
    }
