"""进化标签与复核（M5）。

M4 只会"生成候选"，M5 要回答"哪些候选值得留下"。人工复核是唯一的真值来源，
所以标签必须落库、可追溯、可改判（改判只追加新标签，旧标签保留为历史）。

- `evolve_label`：一次复核结论（correct / wrong / unsure）；
- `apply_labels`：把最新标签应用到 pattern/experience/trait 的状态；
- `precision`：已复核规律的准确率，给 M5 Gate 用；
- `review_queue`：还没被复核的候选，供 CLI / 人读。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.db import Database
from ..base.ids import ulid

TARGETS = ("pattern", "experience", "conflict", "trait")
VERDICTS = ("correct", "wrong", "unsure")


def _exists(db: Database, target_tbl: str, target_id: str) -> bool:
    return (
        db.conn.execute(f"SELECT 1 FROM {target_tbl} WHERE id=?", (target_id,)).fetchone()
        is not None
    )


def label(
    db: Database,
    *,
    target_tbl: str,
    target_id: str,
    verdict: str,
    note: str | None = None,
    actor: str = "user",
    subject: str = "self",
) -> dict[str, Any]:
    """追加一条复核标签。改判不改旧行，只追加新行。"""
    if target_tbl not in TARGETS:
        raise ValueError(f"target_tbl 必须是 {TARGETS}")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {VERDICTS}")
    if not _exists(db, target_tbl, target_id):
        raise KeyError(f"{target_tbl}:{target_id}")
    label_id = ulid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO evolve_label(id, subject_id, target_tbl, target_id, verdict, note, "
            "actor, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (label_id, subject, target_tbl, target_id, verdict, note, actor, now_iso()),
        )
        events.append(
            conn,
            actor=actor,
            op="evolve.label",
            target_tbl=target_tbl,
            target_id=target_id,
            subject_id=subject,
            payload={"verdict": verdict, "note": note},
        )
    return {
        "id": label_id,
        "target_tbl": target_tbl,
        "target_id": target_id,
        "verdict": verdict,
    }


def latest_labels(
    db: Database, *, subject: str = "self", target_tbl: str | None = None
) -> dict[tuple[str, str], Any]:
    """每个目标只取最新一条标签（按时间倒序，时间相同按 rowid 倒序）。"""
    sql = "SELECT * FROM evolve_label WHERE subject_id=?"
    params: list[Any] = [subject]
    if target_tbl:
        sql += " AND target_tbl=?"
        params.append(target_tbl)
    sql += " ORDER BY created_at DESC, rowid DESC"
    out: dict[tuple[str, str], Any] = {}
    for row in db.conn.execute(sql, params).fetchall():
        key = (row["target_tbl"], row["target_id"])
        if key not in out:
            out[key] = row
    return out


def apply_labels(db: Database, *, subject: str = "self") -> dict[str, Any]:
    """把最新标签应用到派生状态；只改状态，不删任何行。"""
    labels = latest_labels(db, subject=subject)
    promoted = rejected = deprecated = unchanged = 0
    with db.tx() as conn:
        for (target_tbl, target_id), row in labels.items():
            verdict = row["verdict"]
            if target_tbl == "pattern":
                current = conn.execute(
                    "SELECT status FROM pattern WHERE id=?", (target_id,)
                ).fetchone()
                if current is None:
                    continue
                if verdict == "wrong" and current["status"] != "rejected":
                    conn.execute("UPDATE pattern SET status='rejected' WHERE id=?", (target_id,))
                    rejected += 1
                elif verdict == "correct" and current["status"] != "confirmed":
                    conn.execute("UPDATE pattern SET status='confirmed' WHERE id=?", (target_id,))
                    promoted += 1
                else:
                    unchanged += 1
            elif target_tbl == "experience":
                current = conn.execute(
                    "SELECT status FROM experience WHERE id=?", (target_id,)
                ).fetchone()
                if current is None:
                    continue
                if verdict == "wrong" and current["status"] != "rejected":
                    conn.execute("UPDATE experience SET status='rejected' WHERE id=?", (target_id,))
                    rejected += 1
                elif verdict == "correct" and current["status"] != "active":
                    conn.execute("UPDATE experience SET status='active' WHERE id=?", (target_id,))
                    promoted += 1
                else:
                    unchanged += 1
            elif target_tbl == "trait":
                current = conn.execute(
                    "SELECT status FROM trait WHERE id=?", (target_id,)
                ).fetchone()
                if current is None:
                    continue
                if verdict == "wrong" and current["status"] in ("hypothesis", "established"):
                    conn.execute(
                        "UPDATE trait SET status='deprecated', retired_reason='user_review', "
                        "updated_at=? WHERE id=?",
                        (now_iso(), target_id),
                    )
                    deprecated += 1
                else:
                    unchanged += 1
            else:
                unchanged += 1  # conflict 的标签只用于审计/校准，不改状态
    return {
        "labels": len(labels),
        "promoted": promoted,
        "rejected": rejected,
        "deprecated": deprecated,
        "unchanged": unchanged,
    }


def precision(
    db: Database, *, subject: str = "self", target_tbl: str = "pattern"
) -> dict[str, Any]:
    """已复核目标的准确率（只算 correct/wrong，unsure 不计）。"""
    labels = latest_labels(db, subject=subject, target_tbl=target_tbl)
    correct = sum(1 for row in labels.values() if row["verdict"] == "correct")
    wrong = sum(1 for row in labels.values() if row["verdict"] == "wrong")
    unsure = sum(1 for row in labels.values() if row["verdict"] == "unsure")
    decided = correct + wrong
    return {
        "target_tbl": target_tbl,
        "labeled": len(labels),
        "correct": correct,
        "wrong": wrong,
        "unsure": unsure,
        "precision": round(correct / decided, 4) if decided else None,
    }


def _pattern_summary(row: Any) -> str:
    return f"{row['kind']} · {row['key']}（support {row['support']}，{row['status']}）"


def review_queue(
    db: Database,
    *,
    subject: str = "self",
    target_tbl: str = "pattern",
    limit: int = 30,
) -> list[dict[str, Any]]:
    """未复核的候选，按"影响大、证据多"排在前面。"""
    labels = latest_labels(db, subject=subject, target_tbl=target_tbl)
    items: list[dict[str, Any]] = []
    if target_tbl == "pattern":
        rows = db.conn.execute(
            "SELECT * FROM pattern WHERE subject_id=? AND status IN ('candidate','confirmed') "
            "ORDER BY support DESC, confidence DESC LIMIT ?",
            (subject, limit * 2),
        ).fetchall()
        for row in rows:
            if ("pattern", row["id"]) in labels:
                continue
            items.append(
                {
                    "target_tbl": "pattern",
                    "id": row["id"],
                    "summary": _pattern_summary(row),
                    "support": row["support"],
                    "confidence": row["confidence"],
                    "status": row["status"],
                    "label": None,
                }
            )
    elif target_tbl == "experience":
        rows = db.conn.execute(
            "SELECT * FROM experience WHERE subject_id=? AND status='active' "
            "ORDER BY reuse_count DESC, confidence DESC LIMIT ?",
            (subject, limit * 2),
        ).fetchall()
        for row in rows:
            if ("experience", row["id"]) in labels:
                continue
            items.append(
                {
                    "target_tbl": "experience",
                    "id": row["id"],
                    "summary": f"{row['situation']} → {row['action']}（{row['outcome']}）",
                    "reuse_count": row["reuse_count"],
                    "confidence": row["confidence"],
                    "status": row["status"],
                    "label": None,
                }
            )
    elif target_tbl == "conflict":
        rows = db.conn.execute(
            "SELECT * FROM conflict WHERE subject_id=? AND status='open' "
            "ORDER BY severity DESC, created_at LIMIT ?",
            (subject, limit * 2),
        ).fetchall()
        for row in rows:
            if ("conflict", row["id"]) in labels:
                continue
            items.append(
                {
                    "target_tbl": "conflict",
                    "id": row["id"],
                    "summary": f"{row['kind']} · {row['slot'] or '—'}（severity {row['severity']}）",
                    "severity": row["severity"],
                    "status": row["status"],
                    "label": None,
                }
            )
    else:
        raise ValueError("review_queue 只支持 pattern / experience / conflict")
    return items[:limit]


def stats(db: Database, *, subject: str = "self") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for target in ("pattern", "experience", "conflict", "trait"):
        out[target] = precision(db, subject=subject, target_tbl=target)
    out["labels"] = db.conn.execute(
        "SELECT COUNT(*) c FROM evolve_label WHERE subject_id=?", (subject,)
    ).fetchone()["c"]
    return out


def render_queue(items: list[dict[str, Any]]) -> str:
    """给 CLI 的人读清单。"""
    if not items:
        return "— 没有待复核的候选"
    lines = []
    for index, item in enumerate(items, 1):
        lines.append(f"{index:>2}. [{item['target_tbl']}] {item['summary']}")
        lines.append(f"    label: pmem evolve label {item['target_tbl']} {item['id']} correct|wrong")
    return "\n".join(lines)


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
