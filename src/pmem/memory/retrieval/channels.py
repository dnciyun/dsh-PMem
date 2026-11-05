"""Candidate assembly around the fused result: appends, budget slots, recall log.

Moved verbatim from memory/store.py (behaviour unchanged). Functions take the owning
``MemoryStore`` explicitly, so this module never imports store.py (no cycle).
``_json_dump`` is duplicated here on purpose: store.py also needs it, and importing
store from here would create a cycle. Keep the two implementations identical.
"""

from __future__ import annotations

import json
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
