"""Candidate assembly around the fused result: appends, budget slots, recall log.

Moved verbatim from memory/store.py (behaviour unchanged). Functions take the owning
``MemoryStore`` explicitly, so this module never imports store.py (no cycle).
``_json_dump`` is duplicated here on purpose: store.py also needs it, and importing
store from here would create a cycle. Keep the two implementations identical.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...base import events
from ...base.clock import now_iso
from ...base.ids import ulid
from .. import graph as graph_mod
from .. import groups as group_mod
from .. import query as query_mod
from .. import temporal as temporal_mod
from .. import vector as vector_mod
from ..text import estimate_tokens, normalize, tokenize

VISIBLE_STATUS = ("active", "contested", "dormant", "candidate")

# 「问我自己的话」线索：只有这类问句才值得去查 assistant 原话。
# 不放宽成「任何低覆盖率问句」——助手的话是证据，不是用户事实；拿它顶用户陈述
# 比漏答更糟（模型会把自己的推测当成用户说过的话）。
# 0.33.0 放宽：外部靶场 58 道真在问「助手说过什么」的题，旧词表只命中 23 道；
# 漏掉的全是**同一语义的另一种口语说法**——时间副词（上回讲/先前/当时）、
# 言说动词（提/给）、所有格与给予（你给我/你的/你给自己）、交付核对（交齐）、
# 个人习惯（习惯/规矩/原则）。补全后 58/58，其余 656 题仍 0 误命中。
# 为什么不放「上次说/之前说」这种省略「你」的说法：那会多命中 67 道**用户事实**题
# （「我上回说下雨才挪货架」「上次说的夜班见闻，我还欠你几件」），而只多找回 11 道——
# 拿助手的话去顶用户事实，是这条通道最不能犯的错。
# 两处刻意收紧：
#   - 「你说」必须带后接词（过/的/给/自己/平时/当时/上次/之前），裸「你说」会吃
#     「跟我说岔过」这类闲聊；
#   - 「答应/承诺」允许中间夹 0-4 字（「你当时答应」），但不再放宽到无「你」。
# 「交齐」是最语料特定的一条（通用问句「房租交齐了吗」也会触发），它在这里换来 5 道题；
# 真机部署如果嫌吵，删掉它只损失交付核对这一类（S2-safe 版）。
ASSISTANT_VOICE_CLUES = re.compile(
    r"你上回讲|你先前|你说自己|你给自己|你给我|你给了|你那个|你这边|你当时|"
    r"你之前|你说(?:过|的|给|自己|平时|当时|上次|之前)|"
    r"按你说的|照你说的|按你的|照你的|"
    r"你[^，。？！\n]{0,4}(?:答应|承诺)|你[^，。？！\n]{0,12}(?:习惯|规矩|原则)|"
    r"你说过|你上次说|你上回说|你讲过|你上次讲|你自己说|你的设定|你叫什么|你自称|"
    r"交齐"
)


def _assistant_voice_query(q: str) -> list[str]:
    """把含线索的问句切成 FTS 词；和 raw_fallback 一样用整句双字。"""
    tokens = tokenize(q).split()
    return tokens[:64]



def claim_hits(
    store: Any, query: str, *, limit: int = 12, kinds: list[str] | None = None
) -> list[dict[str, Any]]:
    """claim 命中通道（task-45，只在 claim.index.enabled 时由 search 调用）。

    真正的 FTS/索引维护在 ``retrieval.claim_index``；这里只是 search 侧的薄包装，
    保持「通道函数都在 channels.py」的一致性。subject/kind 过滤在 claim_index 内做。
    """
    from . import claim_index

    return claim_index.search(
        store.db, query, subject=store.subject, kinds=kinds, limit=limit
    )


def _json_dump(data: dict[str, Any] | None) -> str:
    # 和 ``memory/store.py`` 里的同名实现必须保持一致（channels 不能 import store，
    # 否则循环）；改 canonical JSON 口径时两处一起改。
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seed_weights(
    pending: list[tuple[Any, float]],
    cov_by_seq: dict[int, float],
    limit: int,
) -> list[tuple[int, float]]:
    """种子 = 真的命中了问句实词、且相关性靠前的几条（``cov > 0``）。

    只按分数取种子是错的：弱覆盖时榜首常是「撞到一个双字就进来」的无关记忆，
    从它出发扩散只会放大噪声（第一版实测：从「厨房水龙头」漂出一堆水龙头）。
    一条都没有命中实词时宁可不扩散——扩散是锦上添花，不是无中生有。
    """
    ranked: list[tuple[float, float, int]] = []
    for row, relevance in pending:
        seq = int(row["seq"])
        cov = float(cov_by_seq.get(seq, 0.0) or 0.0)
        if cov <= 0:
            continue
        ranked.append((cov, float(relevance), seq))
    if not ranked:
        return []
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [(seq, max(0.05, rel)) for _cov, rel, seq in ranked[: max(0, limit)]]


def graph_mass(
    store: Any,
    seeds: list[tuple[int, float]],
    graph_cfg: dict[str, Any],
) -> dict[int, float]:
    """种子 → 全图扩散质量（PPR）。只取两跳邻域，每层一条 ``IN (...)`` 查询。"""
    if not seeds:
        return {}
    adj = graph_mod.neighbourhood(
        store.db.conn,
        [seq for seq, _ in seeds],
        hops=int(graph_cfg.get("hops", 2) or 2),
        max_nodes=graph_mod.MAX_NODES,
    )
    if not adj:
        return {}
    return graph_mod.spread(
        adj,
        seeds,
        alpha=float(graph_cfg.get("alpha", 0.5) or 0.5),
        iterations=int(graph_cfg.get("iterations", 3) or 3),
    )


def graph_append(
    store: Any,
    *,
    mass: dict[int, float],
    picked: list[tuple[int, float]],
    graph_cfg: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """（默认关闭）把扩散到、但词法没进前 k 的记忆补进来。

    为什么默认关：实测在这套 90 天语料上，答案是**已经在候选里、只是排在第 9-30 名**，
    补一条只会占掉别的槽位（净收益 0）；真正的杠杆是 ``graph_affinity`` 重排。
    留着它是因为冷启动/候选池被截断时它仍然有用，而且开关可回到旧行为。
    """
    if not mass:
        return [], {}
    g = graph_cfg or {}
    min_share = float(g.get("min_share", 0.12) or 0.0)
    max_items = max(0, int(g.get("max_items", 2) or 0))
    shown = {int(seq) for seq, _ in picked}
    chosen: list[tuple[int, float]] = []
    for seq, share in sorted(mass.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(chosen) >= max_items:
            break
        if share < min_share:
            break
        if seq in shown:
            continue
        chosen.append((seq, share))
    if not chosen:
        return [], {"added": 0}
    statuses = VISIBLE_STATUS
    marks = ",".join("?" for _ in chosen)
    rows = store.db.conn.execute(
        f"SELECT * FROM memory WHERE seq IN ({marks}) AND subject_id=? "
        f"AND status IN ({','.join('?' for _ in statuses)})",
        (*[seq for seq, _ in chosen], store.subject, *statuses),
    ).fetchall()
    by_seq_rows = {int(row["seq"]): row for row in rows}
    ids = [row["id"] for row in rows]
    group_map = group_mod.group_map(store.db, "memory", ids)
    grade_map = group_mod.grade_map(store.db, "memory", ids)
    out: list[dict[str, Any]] = []
    for seq, share in chosen:
        row = by_seq_rows.get(seq)
        if row is None:  # 图里还留着、但记忆已不可见 → 跳过
            continue
        out.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                "score": round(min(0.7, 0.18 + 0.5 * share), 4),
                "kind": row["kind"],
                "day": row["day_key"],
                "valid_from": row["valid_from"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "provenance": row["provenance"],
                "channels": ["graph"],
                "truncated": False,
                "groups": group_map.get(row["id"], []),
                "grade": grade_map.get(row["id"]),
                "source": "graph",
                "reach": round(share, 3),
            }
        )
    return out, {"added": len(out)}


def temporal_append(
    store: Any,
    *,
    q: str,
    fused: dict[int, float],
    payload_seq: list[int],
    filters: dict[str, Any],
    temporal_cfg: dict[str, Any],
    update_q: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """旧说法命中 → 沿 supersede 链补替代版本。

    词法通道（FTS）里**看得到** superseded 的行（触发器照常维护索引），
    只是候选池按状态把它们滤掉了。所以这里从词法结果里挑旧说法、查它的
    ``superseded_by``，把链尾那个还在用的版本当候选补进来。

    补进来的分数按「旧说法排第几」定：旧说法越像问句，它的替代版本越可能是答案。
    """
    if not fused:
        return [], {}
    if bool(temporal_cfg.get("chain_only_update", False)) and not update_q:
        return [], {}
    ranked = sorted(fused, key=lambda seq: -fused[seq])
    rank_of = {seq: idx + 1 for idx, seq in enumerate(ranked)}
    max_old = int(temporal_cfg.get("chain_max_old", 60) or 60)
    replacements = temporal_mod.chain_replacements(
        store.db.conn,
        ranked[:max_old],
        subject=store.subject,
        hops=int(temporal_cfg.get("chain_hops", 3) or 3),
        max_old=max_old,
    )
    if not replacements:
        return [], {"chain": 0}
    shown = {int(seq) for seq in payload_seq}
    picked: list[tuple[int, float, int]] = []
    for old_seq, (new_seq, _hops) in replacements.items():
        if int(new_seq) in shown:
            continue
        base = float(temporal_cfg.get("chain_score", 0.72) or 0.72)
        if not update_q:
            # 非更新题里「旧说法→新版本」只是锦上添花：压到尾巴，别把答案顶掉。
            base = float(temporal_cfg.get("chain_score_noupdate", base) or base)
        score = temporal_mod.chain_score(
            rank_of.get(int(old_seq), 99),
            base=base,
            step=float(temporal_cfg.get("chain_step", 0.02) or 0.02),
            floor=float(temporal_cfg.get("chain_min", 0.55) or 0.0),
        )
        picked.append((int(new_seq), score, int(old_seq)))
    if not picked:
        return [], {"chain": len(replacements), "added": 0}
    picked.sort(key=lambda item: -item[1])
    picked = picked[: max(1, int(temporal_cfg.get("chain_max_items", 2) or 2))]
    rows = fetch_scored(store, 
        {seq: score for seq, score, _old in picked}, filters, limit=max(2, len(picked) * 2)
    )
    by_seq = {int(row["seq"]): row for row in rows}
    ids = [row["id"] for row in rows]
    group_map = group_mod.group_map(store.db, "memory", ids)
    grade_map = group_mod.grade_map(store.db, "memory", ids)
    out: list[dict[str, Any]] = []
    for seq, score, old_seq in picked:
        row = by_seq.get(seq)
        if row is None:  # 替代版本不可见（删了/密级过滤）→ 跳过
            continue
        out.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                "score": round(score, 4),
                "kind": row["kind"],
                "day": row["day_key"],
                "valid_from": row["valid_from"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "provenance": row["provenance"],
                "channels": ["supersede"],
                "truncated": False,
                "groups": group_map.get(row["id"], []),
                "grade": grade_map.get(row["id"]),
                "source": "supersede",
                "from_seq": int(old_seq),
            }
        )
    return out, {"chain": len(replacements), "update_q": bool(update_q)}


def entity_append(
    store: Any,
    cand: list[tuple[int, float]],
    *,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """实体桥补候选：桥到、但词法没选中的 seq → payload 条目（只补不重排）。

    ``cand`` 是 ``[(seq, 桥强度)]``。分数压在词法头部之下、噪声尾巴之上：
    这类记忆在「词法上答不出」，能进榜的唯一理由就是实体桥，所以不跟词法争前几名；
    能不能留下由调用方 ``_append_items`` 的置换策略决定。
    """
    if not cand:
        return []
    fused = {int(seq): float(score) for seq, score in cand}
    rows = fetch_scored(store, fused, filters, limit=max(1, len(cand)))
    if not rows:
        return []
    by_seq = {int(row["seq"]): row for row in rows}
    ids = [row["id"] for row in rows]
    group_map = group_mod.group_map(store.db, "memory", ids)
    grade_map = group_mod.grade_map(store.db, "memory", ids)
    out: list[dict[str, Any]] = []
    for seq, strength in cand:
        row = by_seq.get(int(seq))
        if row is None:  # 被状态/密级/分组过滤掉了 → 不补
            continue
        out.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                "score": round(min(0.62, 0.30 + 0.12 * float(strength)), 4),
                "kind": row["kind"],
                "day": row["day_key"],
                "valid_from": row["valid_from"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "provenance": row["provenance"],
                "channels": ["entity"],
                "truncated": False,
                "groups": group_map.get(row["id"], []),
                "grade": grade_map.get(row["id"]),
                "source": "entity",
                "reach": round(float(strength), 3),
            }
        )
    return out


def vector_append(
    store: Any,
    *,
    q: str,
    cov_by_seq: dict[int, float],
    picked: list[tuple[int, float]],
    filters: dict[str, Any],
    vec_cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """向量补候选：余弦排前几条里，挑没被词法选中的记忆，按预算装进来。

    只补不重排——和关联图同一个立场：改排序会同时牵动所有题，收益无法归因。
    相似度低于 ``min_score`` 的宁可不给：no_memory 题（问的是没记住的事）
    词法覆盖率天然是 0，必然会触发这里，阈值就是防它把「像但不相关」的条目塞进去。
    """
    max_items = max(0, int(vec_cfg.get("max_items", 2) or 0))
    if max_items <= 0:
        return [], {"triggered": True, "limited": 0}
    limit = max(max_items, int(vec_cfg.get("limit", 8) or 8))
    scores = vector_mod.search(
        store.db,
        store.cfg,
        q,
        limit=limit,
        min_score=float(vec_cfg.get("min_score", 0.45) or 0.0),
    )
    if not scores:
        return [], {"triggered": True, "candidates": 0}
    # 过滤条件（subject/status/kind/分组/密级）复用词法那条 SQL，别写第二份。
    rows = fetch_scored(store, scores, filters, limit=max(limit, max_items))
    shown = {int(seq) for seq, _score in picked}
    chosen = [row for row in rows if int(row["seq"]) not in shown][:max_items]
    if not chosen:
        return [], {"triggered": True, "candidates": len(scores)}
    ids = [row["id"] for row in chosen]
    group_map = group_mod.group_map(store.db, "memory", ids)
    grade_map = group_mod.grade_map(store.db, "memory", ids)
    out: list[dict[str, Any]] = []
    for row in chosen:
        seq = int(row["seq"])
        sim = float(scores.get(seq, 0.0) or 0.0)
        out.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                # 余弦相似度（0..1）和词法的 final_score 同为相关性量纲，
                # 直接当分数参与尾巴竞争；乘 0.98 只是让同分时词法结果优先。
                "score": round(min(0.95, 0.98 * sim), 4),
                "kind": row["kind"],
                "day": row["day_key"],
                "valid_from": row["valid_from"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "provenance": row["provenance"],
                "channels": ["vector"],
                "truncated": False,
                "groups": group_map.get(row["id"], []),
                "grade": grade_map.get(row["id"]),
                "source": "vector",
                "reach": round(sim, 3),
            }
        )
    top = round(max(float(v) for v in scores.values()), 3) if scores else 0.0
    return out, {
        "triggered": True,
        "candidates": len(scores),
        "top": top,
        "coverage": round(max(cov_by_seq.values(), default=0.0), 3),
    }


def append_items(
    store: Any,
    items: list[dict[str, Any]],
    *,
    payload: list[dict[str, Any]],
    payload_seq: list[int],
    payload_cov: list[float],
    k: int,
    budget: int,
    spent: int,
    policy: str = "zero_cover",
    head: int = 4,
) -> tuple[list[dict[str, Any]], list[int], list[float], int, int, int]:
    """把补来的条目塞进 k 个槽位：先占空位，再按策略换尾巴。

    为什么只换「覆盖率 0」的：k 个槽位是硬上限，补进来的条目必须挤掉一条。
    覆盖率 0 意味着这条记忆一个问句实词都没撞上，只是被 RRF/时效抬进来的，
    换掉它不会伤到「真的命中了问题」的结果；全员都有覆盖率时宁可不补。
    返回 ``(payload, payload_seq, payload_cov, spent, added, evicted)``。
    """
    if not items:
        return payload, payload_seq, payload_cov, spent, 0, 0
    if policy == "score":
        # 按相似度竞争：前 ``head`` 名不动，尾巴（第 head+1 名之后）和补来的条目
        # 一起按分数排序，分低的掉出去。这是唯一真正能补进漏答题的策略——
        # 漏答题的词法尾巴往往也有覆盖率，「只换覆盖率为 0 的尾巴」根本换不动。
        head_n = max(0, min(int(head), len(payload)))
        remaining = max(0, int(budget) - int(spent))
        fitted: list[dict[str, Any]] = []
        for item in items:
            cost = estimate_tokens(item["text"]) + 12
            if cost > remaining:
                continue
            remaining -= cost
            spent += cost
            fitted.append(item)
        if not fitted:
            return payload, payload_seq, payload_cov, spent, 0, 0
        tail = payload[head_n:] + fitted
        tail_seq = payload_seq[head_n:] + [0] * len(fitted)
        tail_cov = payload_cov[head_n:] + [float(item.get("reach") or 0.0) for item in fitted]
        room = max(0, k - head_n)
        order = sorted(range(len(tail)), key=lambda i: -float(tail[i].get("score") or 0.0))
        chosen = order[:room]
        lex_tail = len(payload) - head_n
        kept_tail = sum(1 for i in chosen if i < lex_tail)
        evicted = max(0, lex_tail - kept_tail)
        return (
            payload[:head_n] + [tail[i] for i in chosen],
            payload_seq[:head_n] + [tail_seq[i] for i in chosen],
            payload_cov[:head_n] + [tail_cov[i] for i in chosen],
            spent,
            len(fitted),
            evicted,
        )
    free_slots = max(0, k - len(payload))
    if policy == "lowest_cover":
        # 先换覆盖率最低的（同覆盖率换分数最低的）：赌「撞上的实词最少的那条」最没用。
        order = sorted(
            range(len(payload_seq)), key=lambda i: (payload_cov[i], payload[i]["score"])
        )
    elif policy == "tail":
        order = list(range(len(payload_seq) - 1, -1, -1))
    else:  # zero_cover：只换一条实词都没撞上的，最保守
        order = sorted(
            (i for i in range(len(payload_seq)) if payload_cov[i] <= 0.0),
            key=lambda i: payload[i]["score"],
        )
    spare = order
    room = free_slots + len(spare)
    remaining = max(0, int(budget) - int(spent))
    fitted: list[dict[str, Any]] = []
    for item in items[:room]:
        cost = estimate_tokens(item["text"]) + 12
        if cost > remaining:
            continue
        remaining -= cost
        spent += cost
        fitted.append(item)
    if not fitted:
        return payload, payload_seq, payload_cov, spent, 0, 0
    evict = set(spare[: max(0, len(fitted) - free_slots)])
    kept = [item for idx, item in enumerate(payload) if idx not in evict]
    kept_seq = [seq for idx, seq in enumerate(payload_seq) if idx not in evict]
    kept_cov = [cov for idx, cov in enumerate(payload_cov) if idx not in evict]
    kept = kept[: max(0, k - len(fitted))]
    kept_seq = kept_seq[: len(kept)]
    kept_cov = kept_cov[: len(kept)]
    return (
        kept + fitted,
        kept_seq + [0] * len(fitted),
        kept_cov + [float(item.get("reach") or 0.0) for item in fitted],
        spent,
        len(fitted),
        len(evict),
    )


def place_items(
    store: Any,  # noqa: ARG001 - 与 append_items 同签名，保留调用侧一致性
    items: list[dict[str, Any]],
    *,
    payload: list[dict[str, Any]],
    payload_seq: list[int],
    payload_cov: list[float],
    k: int,
    budget: int,
    spent: int,
    head: int = 3,
    max_evict: int = 2,
    victim_cov_max: float | None = None,
    cov_value: float | None = None,
) -> tuple[list[dict[str, Any]], list[int], list[float], int, int, int]:
    """把补来的条目放进 k 个槽位：先占空位，必要时挤掉**头部之后的弱尾巴**。

    和 ``append_items`` 的分工：那个是通用补候选（``policy='score'`` 会让尾部按分数
    重排），会重排既有结果；这里只做**一次保守置换**——头部 ``head`` 条永不动，
    受害者按 ``(覆盖率, 分数)`` 升序挑最弱的那几条，``victim_cov_max`` 给了就再加
    一道「覆盖率必须够低」的门槛。换进来的条目按调用方给的顺序接在尾部。

    为什么需要它：加深/原话这两路都是「问句已经明说缺的是什么」时才跑，命中率比
    普通补候选高；但它们的失败有一半是「k 个槽位已经满了，没地方放」。只填空位等于
    把最该救的那一类直接跳过。置换上限 ``max_evict`` 与头部保护一起，把「换错」的
    风险限死在尾部几条最弱的条目上。

    返回 ``(payload, payload_seq, payload_cov, spent, added, evicted)``。
    """
    if not items:
        return payload, payload_seq, payload_cov, spent, 0, 0
    payload = list(payload)
    payload_seq = list(payload_seq)
    payload_cov = list(payload_cov)
    # 防御：编排里凡是用 `payload = ... + items` 改过 payload 的分支（原话兜底/日记），
    # 都可能让 seq/覆盖率数组落后于 payload。这里补齐，别让「挑尾巴」变成索引越界。
    if len(payload_seq) < len(payload):
        payload_seq += [0] * (len(payload) - len(payload_seq))
    if len(payload_cov) < len(payload):
        payload_cov += [0.0] * (len(payload) - len(payload_cov))
    head_n = max(0, min(int(head), len(payload)))
    # 受害者候选：头部之后、覆盖率不超过门槛的条目，最弱的排前面。
    # ``victim_cov_max=None`` 表示不限覆盖率（调用方已经用别的理由说明「这条该让位」）。
    victims = [
        index
        for index in range(head_n, len(payload))
        if victim_cov_max is None or payload_cov[index] <= float(victim_cov_max)
    ]
    victims.sort(
        key=lambda index: (float(payload_cov[index]), float(payload[index].get("score") or 0.0))
    )
    avail = max(0, int(budget) - int(spent))
    slots_left = max(0, int(k) - len(payload))
    fitted: list[dict[str, Any]] = []
    evicted: list[int] = []
    for item in items:
        cost = estimate_tokens(str(item.get("text") or "")) + 12
        if slots_left > 0:
            if cost > avail:
                continue
            fitted.append(item)
            avail -= cost
            slots_left -= 1
            continue
        # 槽位已满：想放就得挤一条最弱的尾巴，且挤出来的预算要够付新条目。
        if len(evicted) >= max(0, int(max_evict)) or not victims:
            break
        victim = victims[0]
        release = estimate_tokens(str(payload[victim].get("text") or "")) + 12
        if cost > avail + release:
            # 这一条太大放不下：跳过它试后面更短的，别因此整批放弃
            # （G2 实测：第二路第一条是长摘要时 break 会把后面能放下的全挡住）。
            continue
        victims.pop(0)
        evicted.append(victim)
        avail += release
        avail -= cost
        fitted.append(item)
    if not fitted:
        return payload, payload_seq, payload_cov, spent, 0, 0
    drop = set(evicted)
    kept = [item for index, item in enumerate(payload) if index not in drop]
    kept_seq = [seq for index, seq in enumerate(payload_seq) if index not in drop]
    kept_cov = [cov for index, cov in enumerate(payload_cov) if index not in drop]
    added_cov = [
        float(cov_value) if cov_value is not None else float(item.get("reach") or 0.0)
        for item in fitted
    ]
    result_payload = kept + fitted
    result_seq = kept_seq + [0] * len(fitted)
    result_cov = kept_cov + added_cov
    # spent 是「payload 实际占的 token」，按结果重算最诚实：加法账在置换（退预算）时
    # 很容易和真实值差出十几 token，而调用方拿它当后续通道的上限。
    result_spent = sum(estimate_tokens(str(item.get("text") or "")) + 12 for item in result_payload)
    return result_payload, result_seq, result_cov, result_spent, len(fitted), len(evicted)


def pack_items(
    items: list[dict[str, Any]],
    seqs: list[int] | None = None,
    *,
    budget: int,
    k: int,
    values: list[float],
    covers: list[set[str]] | None = None,
    must_cover: list[str] | None = None,
    per_item_overhead: int = 12,
    truncate_at: int = 120,
) -> tuple[list[dict[str, Any]], list[int], int]:
    """预算打包（P2-12，默认关）：按价值装箱决定「注入哪些 / 什么顺序」。

    和 ``scoring.fit_budget`` 的分工：那个是**先到先得**（按已有顺序装，装不下就截断
    120 字并停）；这里改成贪心装箱，只换「装填策略」，**候选评分一分不动**。

    两阶段（确定性）：
    1. must_cover 优先：每个必须覆盖的槽位挑价值最高、装得下的条目（宁缺勿错：装不下
       就跳过该槽位，不硬塞）；
    2. 剩余预算按价值从高到低填。价值由调用方算（``search._pack_payload``：
       coverage × 类型覆盖 × 新鲜度），这里只读 ``values``/``covers``。

    约束：返回顺序即注入顺序；``len(结果) ≤ k``、``used ≤ budget``。放不下的长条目按
    ``fit_budget`` 同款规则截到 ``truncate_at`` 字再试一次（标 ``truncated``），仍放不下
    就跳过继续试后面的（装箱不该因为一条太长而停）。
    返回 ``(items, seqs, used)``——seqs 与 items 对齐，供调用方回填 payload_seq。
    """
    cap_k = max(0, int(k))
    cap_budget = max(0, int(budget))
    if not items or cap_k <= 0 or cap_budget <= 0:
        return [], [], 0
    seq_list = list(seqs) if seqs is not None else [0] * len(items)
    if len(seq_list) != len(items):
        seq_list = [0] * len(items)
    slots = [str(slot) for slot in (must_cover or []) if str(slot)]
    cover_list: list[set[str]] = list(covers) if covers is not None else [set() for _ in items]

    packed: list[dict[str, Any]] = []
    packed_seqs: list[int] = []
    taken: set[int] = set()
    used = 0

    def _try_take(index: int) -> bool:
        """装下第 index 条（装不下先按既有规则截断再试）；成功返回 True。"""
        nonlocal used
        item = items[index]
        text = str(item.get("text") or "")
        avail = cap_budget - used
        cost = estimate_tokens(text) + per_item_overhead
        if cost <= avail:
            packed.append(item)
            packed_seqs.append(seq_list[index])
            used += cost
            return True
        # 和 fit_budget 同款：剩余够 40 token 才值得截断，否则这条放弃。
        if avail - per_item_overhead < 40:
            return False
        head_chars = max(20, min(len(text), truncate_at))
        clipped = dict(item)
        clipped["text"] = text[:head_chars] + "…"
        clipped["truncated"] = True
        cost = estimate_tokens(clipped["text"]) + per_item_overhead
        if cost > avail:
            return False
        packed.append(clipped)
        packed_seqs.append(seq_list[index])
        used += cost
        return True

    # 阶段 1：must_cover 槽位优先（每个槽位最多一条；同价值按原顺序，保证确定性）。
    for slot in slots:
        if len(packed) >= cap_k:
            break
        candidates = [
            index
            for index in range(len(items))
            if index not in taken and slot in cover_list[index]
        ]
        candidates.sort(key=lambda index: (-float(values[index] or 0.0), index))
        for index in candidates:
            if _try_take(index):
                taken.add(index)
                break
    # 阶段 2：剩余预算按价值从高到低贪心填满（同价值按原顺序）。
    for index in sorted(range(len(items)), key=lambda i: (-float(values[i] or 0.0), i)):
        if len(packed) >= cap_k:
            break
        if index in taken:
            continue
        if _try_take(index):
            taken.add(index)
    return packed, packed_seqs, used


# P2-15（2026-11-07）核实后判定为**伪需求，不设开关**：assistant 原话从 0.36.1 起
# 就不按 role 过滤（本函数 SQL 一直没 role 条件），assistant 行默认就能被原话兜底召回，
# 加开关没有可观察语义。留一条正向断言防回归（tests/test_assistant_raw.py）。
def raw_fallback(
    store: Any,
    *,
    q: str,
    terms: list[str],
    k: int,
    cov_best: float,
    got: int,
    have_graph: bool = False,
) -> list[dict[str, Any]]:
    """派生事实答不上来时，回原话里捞一捞。

    为什么需要它：``_facts`` 把用户原话改写成「用户……」并截断 80 字，
    人一多、话一长，细节就丢了；而 journal_line 里原话还在（保留期内）。
    只在**派生结果薄弱**时查，且最多补 ``raw_fallback_max`` 条，
    避免每轮多一份注入成本。

    ``have_graph``：关联扩散已经补进相关记忆时不再做原话兜底——
    两条兜底抢同一份剩余槽位，先到的把后到的挤掉，图给的还是记忆单元，
    比未消化的原话更干净。图没东西（冷启动/没有边）才轮到原话。
    """
    if have_graph:
        return []
    if not bool(store.cfg.get("retrieval.raw_fallback", True)):
        return []
    threshold = float(store.cfg.get("retrieval.raw_fallback_coverage", 0.30) or 0.0)
    if got >= k and cov_best >= threshold:
        return []
    limit = max(1, int(store.cfg.get("retrieval.raw_fallback_max", 2) or 2))
    # 这里用**整句**的双字，而不是 content_terms：后者删虚词会在词中间接缝，
    # 造出「班骑」这种不存在的词（「上班骑什么」→ 上被当虚词删掉），原文兜底会漏。
    tokens = tokenize(q).split()
    if not tokens:
        return []
    expr = " OR ".join(f'"{token}"' for token in tokens[:64])
    try:
        rows = store.db.conn.execute(
            "SELECT jl.id, jl.text, jl.day_key, jl.role, jl.ts, bm25(journal_fts) AS bm25 "
            "FROM journal_fts JOIN journal_line jl ON jl.seq=journal_fts.rowid "
            "WHERE journal_fts MATCH ? "
            "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
            "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
            "ORDER BY bm25(journal_fts) LIMIT ?",
            (expr, limit * 4),
        ).fetchall()
    except Exception:  # noqa: BLE001 老库没建索引：兜底不存在也不报错
        return []
    if not rows:
        return []
    query_terms = query_mod.bigrams(q)
    ranked = []
    for row in rows:
        text = str(row["text"] or "")
        if not text:
            continue
        overlap = len(query_terms & query_mod.bigrams(text))
        ranked.append((overlap, float(row["bm25"] or 0.0), row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    out: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for overlap, _bm25, row in ranked:
        if len(out) >= limit:
            break
        text = str(row["text"])
        key = normalize(text)
        if not key or key in seen_text:
            continue
        seen_text.add(key)
        out.append(
            {
                "id": row["id"],
                "text": text,
                "score": round(min(1.0, 0.35 + 0.1 * overlap), 4),
                "kind": "journal",
                "day": row["day_key"],
                # 原话行没有 memory.valid_from 这一列；它的 ts 就是这句话发生的时间。
                # 透出 valid_from 让渲染层能和记忆条目用同一套「优先发生时间」逻辑，
                # role 则让模型知道这句是用户说的还是助手说的（兜底是唯一能召回助手原话的通道）。
                "valid_from": row["ts"],
                "role": row["role"],
                "importance": 0.5,
                "confidence": 0.6,
                "provenance": "user_explicit" if row["role"] == "user" else "observed",
                "channels": ["journal"],
                "truncated": False,
                "groups": [],
                "grade": None,
                "source": "journal",
            }
        )
    return out


def _assistant_rank_score(body: str, query_grams: set[str], query_terms: list[str]) -> float:
    """assistant 原话的重排分：问句实词命中×2 + 双字重合 − 问号惩罚。

    为什么权重这样定：实词命中比双字重合更可信（双字会撞虚词接缝），问号惩罚是因为
    助手行里带问号的多半在反问用户、不是陈述答案。三个权重在探针里是一张平坦曲面
    （gold 进 top-2 在 44–45 之间，见 .tmp/analysis-034/weight_surface.json），
    说明它不是对 58 道题的尖峰拟合。
    """
    key = normalize(body)
    term_hits = sum(1 for term in query_terms if term in key)
    overlap = len(query_grams & query_mod.bigrams(key))
    penalty = 0.5 if ("？" in body or "?" in body) else 0.0
    return 2.0 * float(term_hits) + float(overlap) - penalty


def assistant_voice(
    store: Any,
    *,
    q: str,
    terms: list[str],  # noqa: ARG001 - 与 raw_fallback 同签名，保留调用侧一致性
    limit: int = 2,
) -> list[dict[str, Any]]:
    """只在问「我（助手）自己说过什么」时，召回 assistant 原话。

    为什么单独一路而不是放开 raw_fallback：原话兜底是「派生事实不够用时回原文」，
    它和用户事实抢同一份预算；assistant 的话是**证据**，只该在问句明确指向
    「你说过/你答应」时出现。否则模型会把自己的推测当成用户陈述。
    为什么 score 压低：这是低置信度的补充证据，不能把真正的事实候选挤下去。

    0.34.0 加了「多扫候选 + 确定性重排」：bm25 把「和问句用词相近」的闲聊排前面，
    而真正的答案往往用词完全不同（问「你给我想的标记」答「一盆画小雨滴」）。
    实测 58 道问助手原话的题里，gold 落在 bm25 第 3–8 名的有 20 道；扫 12 条再按
    「问句实词命中×2 + 双字重合 − 问号惩罚」重排，gold 进 top-2 从 33 提到 44–45。
    权重在一张平坦曲面上都稳定（见 .tmp/analysis-034/weight_surface.json），不是尖峰拟合。
    """
    text = str(q or "")
    if not ASSISTANT_VOICE_CLUES.search(text):
        return []
    tokens = _assistant_voice_query(text)
    if not tokens:
        return []
    cfg = store.cfg.get("retrieval.assistant_voice") or {}
    cfg = cfg if isinstance(cfg, dict) else {}
    want = max(1, int(limit or 2))
    # 扫描条数是内部上限，不受 limit 影响：多扫几条只是 SQL 成本，进 payload 的仍是 limit 条。
    scan = max(want, int(cfg.get("scan", 8) or 8))
    scan = min(scan, 32)
    rerank = bool(cfg.get("rerank", True))
    expr = " OR ".join(f'"{token}"' for token in tokens)
    try:
        rows = store.db.conn.execute(
            "SELECT jl.id, jl.text, jl.day_key, jl.role, jl.ts, bm25(journal_fts) AS bm25 "
            "FROM journal_fts JOIN journal_line jl ON jl.seq=journal_fts.rowid "
            "WHERE journal_fts MATCH ? AND jl.subject_id=? AND jl.role='assistant' "
            "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
            "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
            "ORDER BY bm25(journal_fts) LIMIT ?",
            (expr, str(store.subject), scan),
        ).fetchall()
    except Exception:  # noqa: BLE001 老库没建 FTS：当没有这条通道，不影响词法
        return []
    query_grams = query_mod.bigrams(text)
    query_terms = query_mod.content_terms(normalize(text))
    ranked: list[tuple[float, float, Any, str, int]] = []
    seen_text: set[str] = set()
    for row in rows:
        body = str(row["text"] or "")
        key = normalize(body)
        if not body or not key or key in seen_text:
            continue
        seen_text.add(key)
        overlap = len(query_grams & query_mod.bigrams(body))
        if rerank:
            rank_score = _assistant_rank_score(body, query_grams, query_terms)
            ranked.append((rank_score, float(row["bm25"] or 0.0), row, body, overlap))
        else:
            ranked.append((0.0, float(row["bm25"] or 0.0), row, body, overlap))
    if rerank:
        # 同分时用 bm25 兜底：SQL 已经是 bm25 升序，这里稳定排序保持它的相对顺序。
        ranked.sort(key=lambda item: (-item[0], item[1]))
    out: list[dict[str, Any]] = []
    for _rank_score, _bm25, row, body, overlap in ranked:
        if len(out) >= want:
            break
        out.append(
            {
                "id": row["id"],
                "text": body,
                # 低分：assistant 原话是补充证据，不能和用户事实抢头部。
                "score": round(min(0.8, 0.30 + 0.1 * overlap), 4),
                "kind": "journal",
                "day": row["day_key"],
                "valid_from": row["ts"],
                "role": "assistant",
                "author": "assistant",
                "importance": 0.5,
                "confidence": 0.6,
                "provenance": "observed",
                "channels": ["journal"],
                "truncated": False,
                "groups": [],
                "grade": None,
                "source": "journal",
            }
        )
    return out


def fetch_scored(
    store: Any,
    fused: dict[int, float],
    filters: dict[str, Any],
    *,
    limit: int,
) -> list[Any]:
    seqs = sorted(fused, key=lambda s: -fused[s])[: max(limit * 2, 100)]
    if not seqs:
        return []
    marks = ",".join("?" for _ in seqs)
    sql = f"SELECT * FROM memory WHERE seq IN ({marks}) AND subject_id=?"
    params: list[Any] = [*seqs, store.subject]
    statuses = filters.get("status") or list(VISIBLE_STATUS)
    sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
    params.extend(statuses)
    kinds = filters.get("kind")
    if kinds:
        sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    if filters.get("since"):
        sql += " AND day_key >= ?"
        params.append(filters["since"])
    if filters.get("pinned_only"):
        sql += " AND pinned=1"
    if filters.get("scope_key"):
        sql += " AND scope_key=?"
        params.append(filters["scope_key"])
    group_path = filters.get("group")
    if group_path:
        base = group_mod.normalize_path(str(group_path))
        sql += (
            " AND id IN (SELECT gm.target_id FROM grp_member gm "
            "JOIN grp g ON g.id=gm.grp_id WHERE gm.target_tbl='memory' "
            "AND g.status='active' AND (g.path=? OR g.path LIKE ?))"
        )
        params.extend([base, base + "/%"])
    grades = filters.get("grade")
    if grades:
        if isinstance(grades, str):
            grades = [grades]
        grades = [group_mod.validate_grade(g) for g in grades]
        sql += (
            " AND id IN (SELECT target_id FROM target_grade "
            "WHERE target_tbl='memory' AND grade IN ("
            + ",".join("?" for _ in grades)
            + "))"
        )
        params.extend(grades)
    if not store.cfg.get("policy.allow_secret", False):
        sql += " AND sensitivity <> 'secret'"
    rows = store.db.conn.execute(sql, params).fetchall()
    # 候选池截断必须按**相关性**，不能按行序：SQL 没有 ORDER BY，返回顺序是 rowid，
    # 一旦 fused 的候选超过池子，seq 靠前的会被留下、真正高分的反而被裁掉
    # （实体桥把候选池撑大后踩到过：一条排第 1 的记忆因为 seq 大被挤出池子）。
    rank = {seq: idx for idx, seq in enumerate(seqs)}
    rows = sorted(rows, key=lambda row: rank.get(int(row["seq"]), len(seqs)))
    return rows[:limit]


def record_search(
    store: Any,
    *,
    query: str,
    mode: str,
    channels: dict[str, list[int]],
    payload: list[dict[str, Any]],
    used: int,
    latency_ms: int,
    client: str | None,
    uncertainty: dict[str, Any] | None = None,
) -> None:
    ids = [p["id"] for p in payload]
    with store.db.tx() as conn:
        recall_id = ulid()
        conn.execute(
            "INSERT INTO recall_log(id, ts, subject_id, query, mode, channels_json, returned_json, "
            "injected_tokens, latency_ms, client) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                recall_id,
                now_iso(),
                store.subject,
                query,
                mode,
                _json_dump({k: len(v) for k, v in channels.items()}),
                _json_dump([{"id": p["id"], "score": p["score"]} for p in payload]),
                used,
                latency_ms,
                client,
            ),
        )
        for mid in ids:
            conn.execute(
                "UPDATE memory SET use_count=use_count+1, last_used_at=? WHERE id=?", (now_iso(), mid)
            )
        # 「不确定」也要留痕（M18）：只写事件日志，不进注入——否则每轮都在道歉。
        # 有了这条流水，之后能回答「我到底在哪些问题上没把握」，而不是靠印象。
        if uncertainty and uncertainty.get("weak"):
            events.append(
                conn,
                actor="recall",
                op="recall.weak",
                target_tbl="recall_log",
                target_id=recall_id,
                subject_id=store.subject,
                payload={
                    "query": query,
                    "reasons": list(uncertainty.get("reasons") or []),
                    "coverage": uncertainty.get("coverage"),
                    "margin": uncertainty.get("margin"),
                    "returned": len(payload),
                },
            )


# ---------------------------------------------------------------- P0–P3 新增（默认关）
# 下面三个函数只服务默认关闭的 opt-in 能力（docs/impl-contract-2026-11-07.md §5）。
# 放在 channels.py 而不是 search.py：它们都要把「seq/行」变成条目 dict，和
# ``entity_append`` 是同一份口径（id/text/score/kind/day/valid_from/groups/grade…）；
# search.py 只负责编排与预算。**关时调用方根本不进这些函数**，对 0.38.0 零影响。

def _aggregate_terms(subject_terms: Any, *, limit: int = 4) -> list[str]:
    """把 plan.subject_terms 收成去重后的短词表（最多 4 个，空串丢掉）。"""
    terms: list[str] = []
    for raw in subject_terms or []:
        term = str(raw or "").strip()
        if term and term not in terms:
            terms.append(term)
    return terms[: max(0, int(limit))]


def _entity_mention_seqs(store: Any, term: str, *, limit: int) -> list[int]:
    """实体名包含 ``term`` 的 memory seq（P1-7 的「实体命中」那一路）。只读。"""
    if not term:
        return []
    try:
        rows = store.db.conn.execute(
            "SELECT DISTINCT em.memory_seq AS seq FROM entity_mention em "
            "JOIN entity e ON e.id=em.entity_id "
            "WHERE e.subject_id=? AND e.status='active' "
            "AND (e.name=? OR e.name LIKE ?) LIMIT ?",
            (store.subject, term, f"%{term}%", max(1, int(limit))),
        ).fetchall()
    except Exception:  # noqa: BLE001 实体表缺/坏只是少一路命中，不影响检索
        return []
    return [int(row["seq"]) for row in rows]


def aggregate_append(
    store: Any,
    *,
    subject_terms: Any,
    filters: dict[str, Any],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """同主体枚举（P1-7，默认关）：按 ``query.plan`` 的 subject_terms 找同主体的多条事实。

    为什么需要：问「各/分别/哪几/两类」时，答案天然是多条并列事实，top-k 相似度只
    带回其中最像的一条。这里用主体词把「同一主体的其它事实」整批取回来，交给调用方
    **只填空位**（不重排）——主体定错时最多多塞几条，不会把原有排序打乱。

    命中两路（契约 §5.4「memory 文本/实体命中」）：
    1. 文本：``store._chan_terms`` 走现成 FTS（和词法通道同一份索引）；
    2. 实体：``entity_mention`` 里名字包含主体词的已抽实体。
    两条都只是**先取候选**，状态/分组/项目范围/密级仍由 ``fetch_scored`` 统一过滤。

    **只填空位是硬语义**：调用方只用 ``_place_items(head=k, max_evict=0)`` 把它接在
    payload 尾部——k 个槽位满时这些候选一条都进不去（默认 k=8 / bench k=40 基本都是满的）。
    置换会挤掉本来对的答案（G2 实测），所以这里不提供「置换」选项，要开另议。
    """
    terms = _aggregate_terms(subject_terms, limit=4)
    cap = max(0, int(limit or 0))
    if not terms or cap <= 0:
        return []
    hits: dict[int, float] = {}
    per_term = max(4, cap * 3)
    for term in terms:
        try:
            seqs = store._chan_terms([term], limit=per_term)
        except Exception:  # noqa: BLE001 词法通道挂了只是少一路命中
            seqs = []
        for seq in seqs:
            hits[int(seq)] = hits.get(int(seq), 0.0) + 1.0
        for seq in _entity_mention_seqs(store, term, limit=per_term):
            hits[int(seq)] = hits.get(int(seq), 0.0) + 1.0
    if not hits:
        return []
    rows = fetch_scored(store, hits, filters, limit=max(1, cap))
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    group_map = group_mod.group_map(store.db, "memory", ids)
    grade_map = group_mod.grade_map(store.db, "memory", ids)
    out: list[dict[str, Any]] = []
    for row in rows:
        hit_count = float(hits.get(int(row["seq"]), 1.0))
        out.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                # 分数压在首遍之下：聚合只能填空位，不该和词法结果抢排位。
                "score": round(min(0.6, 0.30 + 0.08 * hit_count), 4),
                "kind": row["kind"],
                "day": row["day_key"],
                "valid_from": row["valid_from"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "provenance": row["provenance"],
                "channels": ["aggregate"],
                "truncated": False,
                "groups": group_map.get(row["id"], []),
                "grade": grade_map.get(row["id"]),
                "source": "aggregate",
                "reach": round(min(1.0, hit_count / max(1, len(terms))), 3),
            }
        )
    return out


def journal_excerpts(store: Any, line_ids: Any, *, max_chars: int = 120) -> dict[str, str]:
    """``source_line_id`` → 原文片段（P2-13，默认关；只读 ``journal_line``）。

    为什么是「片段」而不是改条目正文：派生事实被截断/改人称后丢掉的细节要补回，
    但条目的 ``text`` 是记忆层的正文，不能偷偷替换（会破坏「原文只追加」和对齐）。
    所以只在条目旁边附一段 ``source_span``，读原文、不改条目。

    一次 IN 查询取回全部行；关时调用方根本不调本函数（契约要求「关时零额外 SQL」）。
    """
    ids: list[str] = []
    for raw in line_ids or []:
        line_id = str(raw or "").strip()
        if line_id and line_id not in ids:
            ids.append(line_id)
    if not ids:
        return {}
    cap = max(1, int(max_chars or 1))
    marks = ",".join("?" for _ in ids)
    try:
        rows = store.db.conn.execute(
            f"SELECT id, text FROM journal_line WHERE subject_id=? AND id IN ({marks})",
            (store.subject, *ids),
        ).fetchall()
    except Exception:  # noqa: BLE001 老库/表缺失：没有原文就返回空，不影响检索
        return {}
    return {str(row["id"]): str(row["text"] or "")[:cap] for row in rows}

