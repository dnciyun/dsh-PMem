"""浮现回执（M9.9）：这一轮的回答里，真的用到了哪几条浮现过的记忆。

判定不调模型，只找**逐字复用**：把记忆正文切成 N 字片段，逐片段扫回答；
命中、且这个片段**不在用户这句话里**，才算这条记忆被用上了。

为什么要排除用户那句话：浮现常常是因为用户提到了它，回答自然也会复述那几个字。
不排除的话，「答非所问地抄了题面」会被记成「用上了」——回执就变成了噪声。

这是**下限估计**：改写、转述、只引一个词都不算。宁可少记，也不记假的。
反过来说，`use_count` 涨了就是硬证据，`use_count` 不涨不代表一定没用。
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from ..base.clock import now
from . import surface as surface_mod

MIN_SPAN = 8
MAX_ANSWER = 20000
_WS = re.compile(r"\s+")


def _collapse(text: str | None) -> str:
    """空白压成一个空格：跨行的同一句话仍然是同一句话。"""
    return _WS.sub(" ", str(text or "")).strip()


def _shingles(text: str, size: int) -> set[str]:
    if len(text) < size:
        return set()
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def _first_hit(answer: str, grams: set[str], banned: set[str], size: int) -> tuple[str, int] | None:
    """按出现顺序找第一个能算「复用」的片段。"""
    limit = len(answer) - size + 1
    for i in range(max(0, limit)):
        gram = answer[i : i + size]
        if gram in grams and gram not in banned:
            return gram, i
    return None


def _surfaced(db: Any, session_id: str) -> dict[str, str]:
    """本会话浮现过、且还没回过执的记忆 → 正文。"""
    used = {
        row["target_id"]
        for row in db.conn.execute(
            "SELECT target_id FROM event_log WHERE op='surface.used' "
            "AND json_extract(payload_json,'$.session')=?",
            (session_id,),
        )
    }
    rows = db.conn.execute(
        "SELECT DISTINCT sl.target_id, m.text_raw, m.status FROM surface_log sl "
        "JOIN memory m ON m.id=sl.target_id "
        "WHERE sl.session_id=? AND sl.target_tbl='memory'",
        (session_id,),
    ).fetchall()
    return {
        row["target_id"]: row["text_raw"] or ""
        for row in rows
        if row["target_id"] not in used and row["status"] != "deleted"
    }


def judge(
    db: Any,
    *,
    session_id: str,
    answer: str,
    exclude: str = "",
    subject: str = "self",
    min_span: int = MIN_SPAN,
    commit: bool = True,
) -> dict[str, Any]:
    """判定并（默认）落回执。``exclude`` 一般传用户那句话。"""
    if not session_id:
        return {"ok": False, "matched": [], "reason": "session_id 不能为空"}
    text = _collapse(answer)[:MAX_ANSWER]
    candidates = _surfaced(db, session_id)
    if not text or not candidates:
        return {
            "ok": True,
            "session_id": session_id,
            "candidates": len(candidates),
            "matched": [],
            "unmatched": len(candidates),
            "reason": "没有回答正文" if not text else "本会话没有待判定的浮现",
        }

    banned = _shingles(_collapse(exclude), min_span)
    matched: list[dict[str, Any]] = []
    for memory_id, raw in candidates.items():
        gram = _first_hit(text, _shingles(_collapse(raw), min_span), banned, min_span)
        if gram is None:
            continue
        matched.append({"memory_id": memory_id, "span": gram[0], "at": gram[1]})

    written: list[dict[str, Any]] = []
    if commit:
        for hit in matched:
            result = surface_mod.mark_used(
                db,
                subject=subject,
                session_id=session_id,
                memory_id=hit["memory_id"],
                signal="receipt",
                note=f"逐字复用：{hit['span']}",
            )
            written.append({**hit, **result})

    return {
        "ok": True,
        "session_id": session_id,
        "candidates": len(candidates),
        "matched": written if commit else matched,
        "unmatched": len(candidates) - len(matched),
        "min_span": min_span,
    }


def token_stats(db: Any, *, days: int = 30) -> dict[str, Any]:
    """``token_per_turn`` 的分布：门是 p95 ≤800，均值 ≤600。"""
    # 用 Python 算截止日，不用 SQL 的 date('now')：后者绕开时间闸门，
    # 回放时会把「模拟的 30 天」看成本机真实时间。
    cutoff = (now() - timedelta(days=max(1, int(days)))).date().isoformat()
    rows = db.conn.execute(
        "SELECT value FROM metrics WHERE name='pmem.token_per_turn' "
        "AND day_key >= ? ORDER BY ts",
        (cutoff,),
    ).fetchall()
    values = sorted(float(row["value"]) for row in rows)
    if not values:
        return {"samples": 0, "days": days}

    def pick(q: float) -> float:
        idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return round(values[idx], 2)

    return {
        "samples": len(values),
        "days": days,
        "mean": round(sum(values) / len(values), 2),
        "p50": pick(0.5),
        "p95": pick(0.95),
        "max": round(values[-1], 2),
        "budget_mean": 600,
        "budget_p95": 800,
    }


__all__ = ["judge", "token_stats", "MIN_SPAN"]
