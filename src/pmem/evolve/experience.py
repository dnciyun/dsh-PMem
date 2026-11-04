"""经验复用（M5，docs/03 §11）。

经验卡不是"读过就算"——它要能被下一次情境匹配到、被复用、再被反馈校准。
没有向量的前提下，情境相似度用词法混合相似度替代 embedding；阈值降到 0.55
并写进边界说明，不假装它等价于 cos≥0.75。

反馈只改经验卡自身（confidence/status），不改原文，也不碰记忆事实层。
"""

from __future__ import annotations

from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.db import Database
from ..base.ids import ulid
from ..memory.dup import similarity

OUTCOME_SCORE = {"success": 1.0, "mixed": 0.6, "failure": 0.2, "unknown": 0.0}
SIGNALS = ("useful", "irrelevant", "wrong")


def _row(db: Database, experience_id: str) -> Any | None:
    return db.conn.execute("SELECT * FROM experience WHERE id=?", (experience_id,)).fetchone()


def match(
    db: Database,
    *,
    subject: str = "self",
    situation: str,
    limit: int = 3,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """按情境找可复用经验；不写库、不自动计复用。"""
    threshold = 0.55 if threshold is None else threshold
    rows = db.conn.execute(
        "SELECT * FROM experience WHERE subject_id=? AND status='active' "
        "ORDER BY reuse_count DESC, confidence DESC",
        (subject,),
    ).fetchall()
    scored: list[tuple[float, Any, float]] = []
    for row in rows:
        sim = similarity(situation, row["situation"])
        if sim < threshold:
            continue
        score = (
            0.5 * sim
            + 0.3 * OUTCOME_SCORE.get(row["outcome"], 0.0)
            + 0.2 * min(1.0, row["reuse_count"] / 5.0)
        )
        scored.append((round(score, 4), row, round(sim, 4)))
    scored.sort(key=lambda item: -item[0])
    return [
        {
            "id": row["id"],
            "situation": row["situation"],
            "action": row["action"],
            "outcome": row["outcome"],
            "lesson": row["lesson"],
            "confidence": row["confidence"],
            "reuse_count": row["reuse_count"],
            "similarity": sim,
            "score": score,
        }
        for score, row, sim in scored[:limit]
    ]


def reuse(
    db: Database,
    *,
    subject: str = "self",
    experience_id: str,
    note: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    """显式复用一次：reuse_count += 1，last_reused_at 更新。"""
    row = _row(db, experience_id)
    if row is None:
        raise KeyError(experience_id)
    if row["status"] != "active":
        raise ValueError(f"经验卡不是 active（当前 {row['status']}）")
    ts = now_iso()
    with db.tx() as conn:
        conn.execute(
            "UPDATE experience SET reuse_count=reuse_count+1, last_reused_at=?, status='active' "
            "WHERE id=?",
            (ts, experience_id),
        )
        events.append(
            conn,
            actor=actor,
            op="experience.reuse",
            target_tbl="experience",
            target_id=experience_id,
            subject_id=subject,
            payload={"note": note},
        )
    return {
        "id": experience_id,
        "reuse_count": int(row["reuse_count"]) + 1,
        "last_reused_at": ts,
    }


def feedback(
    db: Database,
    *,
    subject: str = "self",
    experience_id: str,
    signal: str,
    note: str | None = None,
    actor: str = "user",
) -> dict[str, Any]:
    """复用后的效果反馈：useful / irrelevant / wrong。"""
    if signal not in SIGNALS:
        raise ValueError(f"signal 必须是 {SIGNALS}")
    row = _row(db, experience_id)
    if row is None:
        raise KeyError(experience_id)
    confidence = float(row["confidence"])
    status = row["status"]
    if signal == "useful":
        confidence = min(0.95, confidence + 0.05)
        status = "active"
    elif signal == "irrelevant":
        confidence = round(confidence * 0.8, 4)
    else:  # wrong
        confidence = round(confidence * 0.6, 4)
        last = db.conn.execute(
            "SELECT signal FROM feedback WHERE target_tbl='experience' AND target_id=? "
            "ORDER BY ts DESC, rowid DESC LIMIT 2",
            (experience_id,),
        ).fetchall()
        if len(last) >= 1 and all(item["signal"] == "wrong" for item in last):
            status = "stale"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO feedback(id, ts, subject_id, target_tbl, target_id, signal, note, actor) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ulid(), now_iso(), subject, "experience", experience_id, signal, note, actor),
        )
        conn.execute(
            "UPDATE experience SET confidence=?, status=? WHERE id=?",
            (confidence, status, experience_id),
        )
        events.append(
            conn,
            actor=actor,
            op="experience.feedback",
            target_tbl="experience",
            target_id=experience_id,
            subject_id=subject,
            payload={"signal": signal, "confidence": confidence, "status": status, "note": note},
        )
    return {
        "id": experience_id,
        "signal": signal,
        "confidence": confidence,
        "status": status,
    }


def success_rate(db: Database, *, subject: str = "self") -> dict[str, Any]:
    """被复用且拿到反馈的经验里，最近一次反馈为 useful 的比例（exp_reuse_success）。"""
    rows = db.conn.execute(
        "SELECT id, reuse_count FROM experience WHERE subject_id=? AND reuse_count>0",
        (subject,),
    ).fetchall()
    evaluated = 0
    successes = 0
    for row in rows:
        last = db.conn.execute(
            "SELECT signal FROM feedback WHERE target_tbl='experience' AND target_id=? "
            "ORDER BY ts DESC, rowid DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if last is None:
            continue
        evaluated += 1
        if last["signal"] == "useful":
            successes += 1
    return {
        "reused": len(rows),
        "evaluated": evaluated,
        "successes": successes,
        "success_rate": round(successes / evaluated, 4) if evaluated else None,
    }


def stats(db: Database, *, subject: str = "self") -> dict[str, Any]:
    by_status = {
        row["status"]: row["c"]
        for row in db.conn.execute(
            "SELECT status, COUNT(*) c FROM experience WHERE subject_id=? GROUP BY status",
            (subject,),
        ).fetchall()
    }
    total = db.conn.execute(
        "SELECT COUNT(*) c FROM experience WHERE subject_id=?", (subject,)
    ).fetchone()["c"]
    out = {
        "total": total,
        "by_status": by_status,
        "reused": db.conn.execute(
            "SELECT COUNT(*) c FROM experience WHERE subject_id=? AND reuse_count>0", (subject,)
        ).fetchone()["c"],
    }
    out.update(success_rate(db, subject=subject))
    return out
