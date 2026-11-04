"""生命周期（M2/M3.6）：候选/活跃/休眠/归档的自动流转。

铁律：**只降级不删除**。dormant / archived 仍然可查、可恢复，只是不进默认召回。
真正的删除只有用户显式发起（`memory_delete --hard`）。

M3.6 起按**等级**决定淡忘速度：S 永不自动休眠，A 180 天，B 90 天，C 30 天，D 7 天。
没有等级的记忆沿用默认 90 天。等级是"我判断它多重要"，所以它直接决定
"多久不用就该沉下去"——这比所有记忆统一 90 天更符合直觉。
"""

from __future__ import annotations

from typing import Any

from ..base import events
from ..base.clock import days_between, now_iso
from ..base.db import Database

# 等级 → 多久不用转 dormant；None = 不自动休眠
GRADE_IDLE_DAYS: dict[str, int | None] = {
    "S": None,
    "A": 180,
    "B": 90,
    "C": 30,
    "D": 7,
}

# 等级 → 已休眠多久转 archived
GRADE_ARCHIVE_DAYS: dict[str, int | None] = {
    "S": None,
    "A": 365,
    "B": 180,
    "C": 90,
    "D": 30,
}


def _idle_days(row: Any) -> float:
    """距最后一次**真的被用到**（没被用过就算创建时间）多少天。

    M9.8b 起 ``last_used_at`` 只由召回 / 浮现回执 / 正面反馈推进；
    「被自动浮现出来看过一眼」不算——否则每轮注入一次卡就永远不会淡忘。
    """
    stamp = row["last_used_at"] or row["created_at"]
    return days_between(stamp)


def tick(
    db: Database,
    *,
    subject: str = "self",
    candidate_discard_days: int = 30,
    active_dormant_days: int = 90,
    dormant_archive_days: int = 180,
    grade_idle_days: dict[str, int | None] | None = None,
    grade_archive_days: dict[str, int | None] | None = None,
    protected_roots: tuple[str, ...] = ("我们",),
    dry_run: bool = False,
) -> dict[str, Any]:
    """跑一次流转。返回每条规则命中多少条。

    ``protected_roots`` 下的记忆（默认「我们」）永不自动下沉：
    淡忘只该作用于任务细节，不该作用于"我们"。
    """
    idle_rules = grade_idle_days or GRADE_IDLE_DAYS
    archive_rules = grade_archive_days or GRADE_ARCHIVE_DAYS
    rows = db.conn.execute(
        "SELECT id, status, pinned, use_count, created_at, last_used_at FROM memory "
        "WHERE subject_id=? AND status IN ('candidate','active','dormant')",
        (subject,),
    ).fetchall()
    grade_rows = db.conn.execute(
        "SELECT target_id, grade FROM target_grade WHERE target_tbl='memory'"
    ).fetchall()
    grades = {row["target_id"]: row["grade"] for row in grade_rows}
    protected: set[str] = set()
    if protected_roots:
        for gm in db.conn.execute(
            "SELECT gm.target_id, g.path FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
            "WHERE gm.target_tbl='memory' AND g.status='active'"
        ).fetchall():
            if gm["path"].split("/")[0] in protected_roots:
                protected.add(gm["target_id"])

    plan: list[tuple[str, str]] = []  # (id, new_status)
    by_grade: dict[str, int] = {}
    for row in rows:
        if row["pinned"]:
            continue
        grade = grades.get(row["id"])
        if grade == "S" or row["id"] in protected:
            continue  # 身份与「我们」永远不下沉
        idle = _idle_days(row)
        if row["status"] == "candidate":
            if row["use_count"] > 0:
                plan.append((row["id"], "active"))
            elif days_between(row["created_at"]) >= candidate_discard_days:
                plan.append((row["id"], "discarded"))
        elif row["status"] == "active":
            threshold = idle_rules.get(grade, active_dormant_days)
            if threshold is not None and idle >= threshold:
                plan.append((row["id"], "dormant"))
                by_grade[grade or "none"] = by_grade.get(grade or "none", 0) + 1
        elif row["status"] == "dormant":
            threshold = archive_rules.get(grade, dormant_archive_days)
            if threshold is not None and idle >= threshold:
                plan.append((row["id"], "archived"))

    counts: dict[str, int] = {}
    for _id, status in plan:
        counts[status] = counts.get(status, 0) + 1

    if plan and not dry_run:
        with db.tx() as conn:
            for memory_id, status in plan:
                conn.execute("UPDATE memory SET status=? WHERE id=?", (status, memory_id))
                events.append(
                    conn,
                    actor="system:lifecycle",
                    op="memory.status",
                    target_tbl="memory",
                    target_id=memory_id,
                    subject_id=subject,
                    payload={"to": status, "by": "tick", "grade": grades.get(memory_id)},
                )
    return {
        "moved": len(plan),
        "by_status": counts,
        "by_dormant_grade": by_grade,
        "dry_run": dry_run,
        "at": now_iso(),
    }
