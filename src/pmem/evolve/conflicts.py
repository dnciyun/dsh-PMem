"""冲突升级与保守裁决（M5）。

M2 的纪律是"只标记不裁决"。M5 只放开两种**不靠猜**的情况：

1. 一方已经被 ``superseded`` / ``archived`` / ``deleted`` —— 另一方胜出；
2. 一方明确 ``supersedes`` 另一方 —— 新版本胜出。

其余冲突不自动裁决，只按时间升级 severity，进入人工复核队列。
"证据多者胜/新者胜"需要标注验证，M5 不拿它当默认行为。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..base import events
from ..base.clock import now, to_iso
from ..base.db import Database
from ..memory import conflict as conflict_mod

LOSER_STATUS = ("superseded", "archived", "deleted")
WINNER_STATUS = ("active", "candidate", "contested")


def escalate(
    db: Database,
    *,
    subject: str = "self",
    days: int = 14,
    bump: float = 0.2,
) -> dict[str, Any]:
    """未决冲突超过 ``days`` 天 → severity 抬升（不改变 open/裁决状态）。"""
    cutoff = to_iso(now() - timedelta(days=days))
    rows = db.conn.execute(
        "SELECT id, severity FROM conflict WHERE subject_id=? AND status='open' "
        "AND created_at < ? AND severity < 1.0",
        (subject, cutoff),
    ).fetchall()
    if not rows:
        return {"escalated": 0, "ids": []}
    ids: list[str] = []
    with db.tx() as conn:
        for row in rows:
            severity = round(min(1.0, float(row["severity"]) + bump), 3)
            conn.execute("UPDATE conflict SET severity=? WHERE id=?", (severity, row["id"]))
            ids.append(row["id"])
        events.append(
            conn,
            actor="system:evolve",
            op="conflict.escalate",
            target_tbl="conflict",
            target_id=None,
            subject_id=subject,
            payload={"count": len(ids), "cutoff": cutoff, "bump": bump},
        )
    return {"escalated": len(ids), "ids": ids}


def auto_resolve(
    db: Database,
    *,
    subject: str = "self",
    enabled: bool = True,
) -> dict[str, Any]:
    """只裁决"一方已被替代/明确 supersede"的冲突；其余保持 open。"""
    if not enabled:
        return {"resolved": 0, "skipped": 0, "by_rule": {}}
    rows = db.conn.execute(
        "SELECT * FROM conflict WHERE subject_id=? AND status='open' ORDER BY created_at",
        (subject,),
    ).fetchall()
    resolved: list[dict[str, Any]] = []
    skipped = 0
    by_rule: dict[str, int] = {}
    for row in rows:
        left = db.conn.execute(
            "SELECT id, status, supersedes_id, superseded_by FROM memory WHERE id=?",
            (row["left_id"],),
        ).fetchone()
        right = db.conn.execute(
            "SELECT id, status, supersedes_id, superseded_by FROM memory WHERE id=?",
            (row["right_id"],),
        ).fetchone()
        if left is None or right is None:
            skipped += 1
            continue
        winner: str | None = None
        loser: str | None = None
        rule: str | None = None
        if left["status"] in LOSER_STATUS and right["status"] in WINNER_STATUS:
            winner, loser, rule = right["id"], left["id"], "superseded"
        elif right["status"] in LOSER_STATUS and left["status"] in WINNER_STATUS:
            winner, loser, rule = left["id"], right["id"], "superseded"
        elif left["supersedes_id"] == right["id"]:
            winner, loser, rule = left["id"], right["id"], "supersedes"
        elif right["supersedes_id"] == left["id"]:
            winner, loser, rule = right["id"], left["id"], "supersedes"
        if winner is None or loser is None or rule is None:
            skipped += 1
            continue
        decision = "prefer_left" if winner == row["left_id"] else "prefer_right"
        conflict_mod.resolve(
            db,
            row["id"],
            decision,
            note=f"rule:{rule}",
            actor="system:evolve",
        )
        resolved.append({"conflict_id": row["id"], "winner": winner, "loser": loser, "rule": rule})
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return {"resolved": len(resolved), "skipped": skipped, "by_rule": by_rule, "items": resolved}


def stats(db: Database, *, subject: str = "self") -> dict[str, Any]:
    open_rows = db.conn.execute(
        "SELECT COUNT(*) c, COALESCE(AVG(severity), 0) s FROM conflict "
        "WHERE subject_id=? AND status='open'",
        (subject,),
    ).fetchone()
    escalated = db.conn.execute(
        "SELECT COUNT(*) c FROM conflict WHERE subject_id=? AND status='open' AND severity>=0.8",
        (subject,),
    ).fetchone()["c"]
    resolved = db.conn.execute(
        "SELECT COUNT(*) c FROM conflict WHERE subject_id=? AND resolved_by='system:evolve'",
        (subject,),
    ).fetchone()["c"]
    return {
        "open": int(open_rows["c"]),
        "avg_severity": round(float(open_rows["s"] or 0.0), 3),
        "escalated": int(escalated),
        "auto_resolved": int(resolved),
    }
