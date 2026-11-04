"""特质时间线与指标（M6）。

时间线 = 版本链（`prev_id`）+ 事件日志（`trait.promote/version/demote`）。
任何一次升降级都必须能回答：哪一版、为什么、证据几条、何时。
"""

from __future__ import annotations

import json
import statistics
from datetime import timedelta
from typing import Any

from ..base.clock import now, to_iso
from ..base.db import Database


def resolve(db: Database, *, subject: str, ref: str) -> Any | None:
    row = db.conn.execute(
        "SELECT * FROM trait WHERE id=? AND subject_id=?", (ref, subject)
    ).fetchone()
    if row is not None:
        return row
    return db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=? AND key=? ORDER BY version DESC LIMIT 1",
        (subject, ref),
    ).fetchone()


def versions(db: Database, *, subject: str, ref: str) -> list[dict[str, Any]]:
    row = resolve(db, subject=subject, ref=ref)
    if row is None:
        return []
    rows = db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=? AND scope=? AND type=? AND key=? "
        "ORDER BY version",
        (subject, row["scope"], row["type"], row["key"]),
    ).fetchall()
    return [
        {
            "id": item["id"],
            "version": item["version"],
            "status": item["status"],
            "strength": item["strength"],
            "confidence": item["confidence"],
            "evidence_days": item["evidence_days"],
            "evidence_count": len(json.loads(item["evidence_json"] or "[]")),
            "prev_id": item["prev_id"],
            "retired_reason": item["retired_reason"],
            "first_seen": item["first_seen"],
            "last_support_at": item["last_support_at"],
            "created_at": item["created_at"],
        }
        for item in rows
    ]


def timeline(db: Database, *, subject: str, ref: str, limit: int = 50) -> list[dict[str, Any]]:
    versions_data = versions(db, subject=subject, ref=ref)
    if not versions_data:
        return []
    ids = [item["id"] for item in versions_data]
    marks = ",".join("?" for _ in ids)
    rows = db.conn.execute(
        f"SELECT ts, op, target_id, payload_json FROM event_log "
        f"WHERE target_tbl='trait' AND target_id IN ({marks}) ORDER BY seq DESC LIMIT ?",
        (*ids, limit),
    ).fetchall()
    return [
        {
            "ts": row["ts"],
            "op": row["op"],
            "trait_id": row["target_id"],
            "payload": json.loads(row["payload_json"] or "{}"),
        }
        for row in rows
    ]


def list_traits(
    db: Database,
    *,
    subject: str = "self",
    status: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM trait WHERE subject_id=?"
    params: list[Any] = [subject]
    if status:
        sql += " AND status=?"
        params.append(status)
    if scope:
        sql += " AND scope=?"
        params.append(scope)
    sql += " ORDER BY CASE status WHEN 'established' THEN 0 WHEN 'hypothesis' THEN 1 ELSE 2 END, strength DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "id": row["id"],
            "scope": row["scope"],
            "type": row["type"],
            "key": row["key"],
            "value": json.loads(row["value_json"] or "{}"),
            "strength": row["strength"],
            "confidence": row["confidence"],
            "status": row["status"],
            "evidence_days": row["evidence_days"],
            "evidence_count": len(json.loads(row["evidence_json"] or "[]")),
            "version": row["version"],
            "updated_at": row["updated_at"],
            "retired_reason": row["retired_reason"],
        }
        for row in db.conn.execute(sql, params).fetchall()
    ]


def metrics(db: Database, *, subject: str = "self", churn_days: int = 30) -> dict[str, Any]:
    rows = db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=?", (subject,)
    ).fetchall()
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        by_type[row["type"]] = by_type.get(row["type"], 0) + 1
    active = [row for row in rows if row["status"] in ("hypothesis", "established")]
    established = [row for row in rows if row["status"] == "established"]
    traceable = [row for row in established if len(json.loads(row["evidence_json"] or "[]")) >= 3]
    active_traceable = [row for row in active if len(json.loads(row["evidence_json"] or "[]")) >= 3]
    cutoff = to_iso(now() - timedelta(days=churn_days))
    deprecated_recent = [row for row in rows if row["status"] == "deprecated" and row["updated_at"] >= cutoff]
    churn = len(deprecated_recent) / max(1, len(active) + len(deprecated_recent))
    evidence_counts = [len(json.loads(row["evidence_json"] or "[]")) for row in active]
    return {
        "total": len(rows),
        "active": len(active),
        "established": len(established),
        "by_status": by_status,
        "by_type": by_type,
        "trace_rate": round(len(traceable) / len(established), 4) if established else 1.0,
        "active_trace_rate": round(len(active_traceable) / len(active), 4) if active else 1.0,
        "churn": round(churn, 4),
        "churn_window_days": churn_days,
        "evidence_median": statistics.median(evidence_counts) if evidence_counts else 0,
        "no_derivation": db.conn.execute(
            "SELECT COUNT(*) c FROM trait WHERE subject_id=? AND (derivation IS NULL OR derivation='')",
            (subject,),
        ).fetchone()["c"],
    }


def render_versions(items: list[dict[str, Any]]) -> str:
    if not items:
        return "— 没有这个特质"
    lines = []
    for item in items:
        reason = f"  ({item['retired_reason']})" if item.get("retired_reason") else ""
        lines.append(
            f"v{item['version']}  {item['status']:<11} strength {item['strength']:.3f}  "
            f"conf {item['confidence']:.3f}  evidence {item['evidence_count']} 条/{item['evidence_days']} 天{reason}"
        )
    return "\n".join(lines)
