"""Lexical channels: FTS5 token/term/trigram/like recall and global IDF weights.

Moved verbatim from memory/store.py (behaviour unchanged). Every function takes the
owning ``MemoryStore`` explicitly so this module never imports store.py (no cycle).
"""

from __future__ import annotations

import math
from typing import Any

from ..text import tokenize


def chan_tokens(
    store: Any,
    q: str,
    *,
    drop: frozenset[str] | set[str] | None = None,
) -> list[int]:
    seg = tokenize(q)
    if not seg:
        return []
    terms = [t for t in seg.split() if t.strip()]  # 长文档别把 OR 项堆到上千
    if drop:
        terms = [t for t in terms if t not in drop]
    return chan_terms(store, terms)


def chan_terms(store: Any, terms: list[str], *, limit: int = 50) -> list[int]:
    """按**整词**做 FTS 召回（不再分词）：给角色词/名字这种不能拆的词用。"""
    words = [t for t in terms if t and t.strip()][:64]
    if not words:
        return []
    expr = " OR ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in words)
    try:
        rows = store.db.conn.execute(
            "SELECT rowid AS seq FROM memory_fts_tok WHERE memory_fts_tok MATCH ? "
            "ORDER BY bm25(memory_fts_tok) LIMIT ?",
            (expr, int(limit)),
        ).fetchall()
    except Exception:
        return []
    return [r["seq"] for r in rows]


def chan_trigram(store: Any, q: str) -> list[int]:
    if len(q) < 3:
        return []
    expr = '"' + q.replace('"', '""') + '"'
    try:
        rows = store.db.conn.execute(
            "SELECT rowid AS seq FROM memory_fts_tri WHERE memory_fts_tri MATCH ? "
            "ORDER BY bm25(memory_fts_tri) LIMIT 50",
            (expr,),
        ).fetchall()
    except Exception:
        return []
    return [r["seq"] for r in rows]


def chan_like(store: Any, q: str) -> list[int]:
    if len(q) > 200:  # 文档级输入不做全表扫描
        return []
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = store.db.conn.execute(
        "SELECT seq FROM memory WHERE text_norm LIKE ? ESCAPE '\\' LIMIT 50",
        (f"%{escaped}%",),
    ).fetchall()
    return [r["seq"] for r in rows]


def term_weights(store: Any, terms: list[str]) -> dict[str, float]:
    """全库 IDF 词权：``log(1 + N/(1+df))``，df=0 的词直接不进表。

    为什么要查全库 df：覆盖率的问题是「问句里的词，这条记忆命中了几成」。
    候选集内的 df 是反向偏置的（虚词双字正是被选中的原因），全库 df 才是真稀罕度。
    成本：每个词一次 FTS 点查（``memory_fts_tok`` 就是现成的倒排），
    12 个词约 1 ms；带进程内缓存，重复问句近零成本。
    """
    if not terms:
        return {}
    total = memory_count(store)
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for term in terms[:24]:
        df = store._df_cache.get(term)
        if df is None:
            expr = '"' + term.replace('"', '""') + '"'
            try:
                row = store.db.conn.execute(
                    "SELECT COUNT(*) c FROM memory_fts_tok WHERE memory_fts_tok MATCH ?", (expr,)
                ).fetchone()
                df = int(row["c"] or 0)
            except Exception:  # noqa: BLE001 索引不可用 → 退回候选集内 df
                return {}
            if len(store._df_cache) > 4096:
                store._df_cache.clear()
            store._df_cache[term] = df
        if df <= 0:
            continue
        out[term] = math.log(1.0 + total / (1.0 + df))
    return out


def last_memory_at(store: Any) -> str | None:
    """最近一条活着的记忆写进来的时间（记忆断档判断用）。"""
    try:
        row = store.db.conn.execute(
            "SELECT MAX(created_at) AS ts FROM memory WHERE status<>'deleted'"
        ).fetchone()
        return str(row["ts"]) if row and row["ts"] else None
    except Exception:  # noqa: BLE001 老库/只读：当作没有
        return None


def memory_count(store: Any) -> int:
    if store._memory_total is None:
        row = store.db.conn.execute(
            "SELECT COUNT(*) c FROM memory WHERE status<>'deleted'"
        ).fetchone()
        store._memory_total = int(row["c"] or 0)
    return store._memory_total
