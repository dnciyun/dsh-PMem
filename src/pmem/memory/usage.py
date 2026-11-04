"""使用语义（M9.8b）：把「被用过」这件事从日志里重算一遍。

为什么要重算，而不是只从今天开始记：

``use_count`` / ``last_used_at`` 一直是**派生量**，可它们以前被"浮现"和"召回"
同时推进。插件装上之后每一轮都会浮现一次，于是每条记忆天天"被用"——
这个数字已经不是原来那个意思了，留着它，30 天日历门测出来的东西就没有意义。

好在原日志都还在，可以重放：

| 证据 | 表 | 算不算"用" |
| --- | --- | --- |
| 显式召回 | ``recall_log.returned_json`` 里出现过 | ✅ |
| 浮现回执 | ``event_log`` 里 ``op='surface.used'`` | ✅ |
| 正面反馈 | ``feedback`` 里 ``signal in ('useful','confirm')`` | ✅ |
| 只是被浮现 | ``surface_log`` | ❌ 只推进 ``last_surfaced_at`` |

这条和「派生可重放」是同一条原则：派生量算错了，就从原文重算，不要手改。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import events
from ..base.db import Database

# 正面反馈里，哪些等于"这条真的派上了用场"。
USED_SIGNALS = ("useful", "confirm")


def _bump(usage: dict[str, list], memory_id: str, stamp: str | None) -> None:
    slot = usage.setdefault(memory_id, [0, None])
    slot[0] += 1
    if stamp and (slot[1] is None or stamp > slot[1]):
        slot[1] = stamp


def replay(db: Database, *, subject: str = "self", dry_run: bool = False) -> dict[str, Any]:
    """从日志重算 ``use_count`` / ``last_used_at`` / ``last_surfaced_at``。

    只碰这三列——正文、等级、分组、可信度一律不动。
    """
    usage: dict[str, list] = {}

    # 1) 显式召回：returned_json 里出现一次就算一条。
    for row in db.conn.execute(
        "SELECT ts, returned_json FROM recall_log WHERE subject_id=? ORDER BY ts", (subject,)
    ):
        try:
            items = json.loads(row["returned_json"] or "[]")
        except ValueError:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                _bump(usage, str(item["id"]), row["ts"])

    # 2) 浮现回执：唯一能证明"浮现出来之后真的被用了"的信号。
    for row in db.conn.execute(
        "SELECT ts, target_id FROM event_log WHERE op='surface.used' AND target_tbl='memory' "
        "AND (subject_id IS NULL OR subject_id=?) ORDER BY ts",
        (subject,),
    ):
        _bump(usage, str(row["target_id"]), row["ts"])

    # 3) 正面反馈。
    marks = ",".join("?" for _ in USED_SIGNALS)
    for row in db.conn.execute(
        f"SELECT ts, target_id FROM feedback WHERE target_tbl='memory' "
        f"AND signal IN ({marks}) AND subject_id=? ORDER BY ts",
        (*USED_SIGNALS, subject),
    ):
        _bump(usage, str(row["target_id"]), row["ts"])

    # 4) 浮现时刻：只用于轮换和"最近活跃"，不算用。
    surfaced: dict[str, str] = {}
    for row in db.conn.execute(
        "SELECT target_id, MAX(ts) AS ts FROM surface_log WHERE target_tbl='memory' "
        "GROUP BY target_id"
    ):
        surfaced[str(row["target_id"])] = row["ts"]

    rows = db.conn.execute(
        "SELECT id, use_count, last_used_at, last_surfaced_at, created_at FROM memory "
        "WHERE subject_id=? AND status<>'deleted'",
        (subject,),
    ).fetchall()

    plan: list[tuple[str, int, str | None, str | None]] = []
    samples: list[dict[str, Any]] = []
    used_now = 0
    surfaced_now = 0
    for row in rows:
        memory_id = row["id"]
        count, last_used = usage.get(memory_id, [0, None])
        last_surfaced = surfaced.get(memory_id)
        if count:
            used_now += 1
        if last_surfaced:
            surfaced_now += 1
        if (
            int(row["use_count"] or 0) == count
            and (row["last_used_at"] or None) == last_used
            and (row["last_surfaced_at"] or None) == last_surfaced
        ):
            continue
        plan.append((memory_id, count, last_used, last_surfaced))
        if len(samples) < 5:
            samples.append(
                {
                    "id": memory_id,
                    "use_count": {"before": int(row["use_count"] or 0), "after": count},
                    "last_used_at": {"before": row["last_used_at"], "after": last_used},
                }
            )

    if plan and not dry_run:
        with db.tx() as conn:
            for memory_id, count, last_used, last_surfaced in plan:
                conn.execute(
                    "UPDATE memory SET use_count=?, last_used_at=?, last_surfaced_at=? WHERE id=?",
                    (count, last_used, last_surfaced, memory_id),
                )
            events.append(
                conn,
                actor="system:backfill",
                op="memory.usage_replay",
                target_tbl="memory",
                target_id=None,
                subject_id=subject,
                payload={
                    "memories": len(rows),
                    "changed": len(plan),
                    "used": used_now,
                    "surfaced": surfaced_now,
                },
            )

    return {
        "memories": len(rows),
        "changed": len(plan),
        "used": used_now,
        "surfaced": surfaced_now,
        "dry_run": bool(dry_run),
        "samples": samples,
        "sources": {
            "recall_log": db.conn.execute("SELECT COUNT(*) c FROM recall_log").fetchone()["c"],
            "receipts": db.conn.execute(
                "SELECT COUNT(*) c FROM event_log WHERE op='surface.used'"
            ).fetchone()["c"],
            "surface_log": db.conn.execute("SELECT COUNT(*) c FROM surface_log").fetchone()["c"],
        },
    }


__all__ = ["replay", "USED_SIGNALS"]
