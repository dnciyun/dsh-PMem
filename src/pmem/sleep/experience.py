"""经验提炼（s5，docs/03 §11）。

经验不是"总结聊天"，而是**结构化复盘**：只有写入时带了
``situation / action / outcome / lesson`` 四段的 ``kind='experience'`` 记忆
才会成为经验卡；缺任何一段都不生成，只计数。

这样避免睡眠引擎把流水账包装成"经验"——那是最容易自欺的一步。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.db import Database
from ..base.ids import ulid
from .common import canonical

OUTCOMES = {"success", "failure", "mixed"}
KEYS = ("situation", "action", "outcome", "lesson")


def _extra(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["extra_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def build(db: Database, *, subject: str = "self", run_id: str = "") -> dict[str, Any]:
    rows = db.conn.execute(
        "SELECT id, text_raw, extra_json, confidence, created_at FROM memory "
        "WHERE subject_id=? AND kind='experience' AND status<>'deleted' ORDER BY created_at",
        (subject,),
    ).fetchall()

    created: list[str] = []
    updated = 0
    incomplete = 0
    invalid = 0
    ts = now_iso()

    with db.tx() as conn:
        for row in rows:
            data = _extra(row)
            situation = str(data.get("situation") or "").strip()
            action = str(data.get("action") or "").strip()
            outcome = str(data.get("outcome") or "").strip().lower()
            lesson = str(data.get("lesson") or "").strip()
            if not situation or not action:
                incomplete += 1
                continue
            if outcome not in OUTCOMES or not lesson:
                invalid += 1
                continue

            existing = conn.execute(
                "SELECT * FROM experience WHERE subject_id=? AND situation=? AND action=? "
                "AND status IN ('active','stale')",
                (subject, situation, action),
            ).fetchone()

            if existing is None:
                exp_id = ulid()
                context = {
                    key: value
                    for key, value in data.items()
                    if key not in KEYS
                }
                conn.execute(
                    "INSERT INTO experience(id, subject_id, situation, action, outcome, lesson, "
                    "context_json, evidence_json, reuse_count, confidence, status, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,0,?,'active',?)",
                    (
                        exp_id,
                        subject,
                        situation,
                        action,
                        outcome,
                        lesson,
                        canonical(context),
                        canonical([{"memory_id": row["id"]}]),
                        min(0.95, float(row["confidence"])),
                        ts,
                    ),
                )
                created.append(exp_id)
                events.append(
                    conn,
                    actor="system:sleep",
                    op="experience.build",
                    target_tbl="experience",
                    target_id=exp_id,
                    subject_id=subject,
                    payload={"outcome": outcome, "run_id": run_id},
                )
                continue

            try:
                evidence = json.loads(existing["evidence_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                evidence = []
            ids = {
                str(item.get("memory_id"))
                for item in evidence
                if isinstance(item, dict) and item.get("memory_id")
            }
            if row["id"] in ids:
                continue
            ids.add(row["id"])
            payload = canonical([{"memory_id": mid} for mid in sorted(ids)])
            conn.execute(
                "UPDATE experience SET evidence_json=? WHERE id=?",
                (payload, existing["id"]),
            )
            updated += 1

    return {
        "scanned": len(rows),
        "created": len(created),
        "updated": updated,
        "incomplete": incomplete,
        "invalid": invalid,
        "ids": created,
    }
