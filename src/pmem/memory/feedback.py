"""反馈闭环（M2）：用户说"对/错/没用"，系统据此调整那条记忆。

**硬约束**：反馈只能改"元数据 + 状态"，不能把推测变成事实——
`provenance` 不可变（触发器钉死），`confidence` 再涨也封顶 0.95。
"""

from __future__ import annotations

from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.db import Database
from ..base.ids import ulid
from . import scoring

SIGNALS = ("useful", "irrelevant", "wrong", "confirm", "deny", "pin", "unpin")
CONF_CAP = 0.95


def apply(
    db: Database,
    memory_id: str,
    signal: str,
    *,
    note: str | None = None,
    actor: str = "user",
    subject: str = "self",
) -> dict[str, Any]:
    if signal not in SIGNALS:
        raise ValueError(f"unknown signal: {signal}")
    row = db.conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
    if row is None:
        raise KeyError(memory_id)

    importance = row["importance"]
    confidence = row["confidence"]
    pinned = row["pinned"]
    status = row["status"]
    use_count = row["use_count"]

    if signal == "useful":
        importance = min(1.0, importance + 0.03)
        use_count += 1
    elif signal == "irrelevant":
        importance = max(0.0, importance * 0.8)
    elif signal == "confirm":
        confidence = min(CONF_CAP, confidence + 0.08)
        if status in ("contested", "candidate"):
            status = "active"
    elif signal in ("wrong", "deny"):
        # 反证：可信度腰斩 + 隔离，等人工裁决；绝不自动删除
        confidence = round(confidence * scoring.CONF_DEMOTION, 4)
        status = "contested"
    elif signal == "pin":
        pinned = 1
        importance = 1.0
    elif signal == "unpin":
        pinned = 0

    extra_changed = signal in ("useful", "confirm")
    with db.tx() as conn:
        conn.execute(
            "UPDATE memory SET importance=?, confidence=?, pinned=?, status=?, use_count=?, "
            "last_used_at=CASE WHEN ? THEN ? ELSE last_used_at END WHERE id=?",
            (importance, confidence, pinned, status, use_count, 1 if extra_changed else 0, now_iso(), memory_id),
        )
        conn.execute(
            "INSERT INTO feedback(id, ts, subject_id, target_tbl, target_id, signal, note, actor) "
            "VALUES(?,?,?,'memory',?,?,?,?)",
            (ulid(), now_iso(), subject, memory_id, signal, note, actor),
        )
        events.append(
            conn,
            actor=actor,
            op="memory.feedback",
            target_tbl="memory",
            target_id=memory_id,
            subject_id=subject,
            payload={"signal": signal, "confidence": confidence, "status": status, "note": note},
        )
    return {
        "id": memory_id,
        "signal": signal,
        "importance": importance,
        "confidence": confidence,
        "status": status,
        "pinned": pinned,
    }


def recent(db: Database, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM feedback ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "target": r["target_id"],
            "signal": r["signal"],
            "note": r["note"],
            "actor": r["actor"],
        }
        for r in rows
    ]
