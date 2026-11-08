"""八个睡眠阶段（docs/03 §9）。

每个阶段只做一件事，输入输出都是可序列化的 stats；阶段之间不共享
内存里的对象，只共享 ``SleepContext``（当天的工作集与配置）。
这样"模拟崩溃后续跑"才成立：从 checkpoint 恢复时，编排器会**重放 s1 的
只读查询**重新算出同样的工作集（``orchestrator.run`` 里 checkpoint 命中
s1 时调用 ``s1_collect``），后面的阶段照常走。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base.config import Config
from ..base.db import Database
from ..memory import conflict, dup, groups, lifecycle
from ..net import relations
from ..trait import derive_trait
from . import experience, patterns, reflection, snapshot
from .common import day_bounds_iso


@dataclass
class SleepContext:
    db: Database
    subject: str
    day_key: str
    run_id: str
    config: Config
    trigger: str = "manual"
    dry_run: bool = False
    report_dir: Path | None = None
    backup_dir: Path | None = None
    work_ids: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(f"sleep.{key}", self.config.get(key, default))


def _scalar(db: Database, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = db.conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def s1_collect(ctx: SleepContext) -> dict[str, Any]:
    """拉当天新记忆 + 待处理候选，按重要度截断到配额。"""
    db = ctx.db
    total = _scalar(
        db,
        "SELECT COUNT(*) FROM memory WHERE subject_id=? AND day_key=? AND status<>'deleted'",
        (ctx.subject, ctx.day_key),
    )
    rows = db.conn.execute(
        "SELECT id, importance FROM memory WHERE subject_id=? AND day_key=? AND status<>'deleted' "
        "ORDER BY importance DESC, created_at ASC",
        (ctx.subject, ctx.day_key),
    ).fetchall()
    quota = int(ctx.cfg("max_new", 3000))
    work = [row["id"] for row in rows[:quota]]
    backlog = max(0, len(rows) - len(work))

    # 当天没被使用过的候选也顺带处理，避免它们永远卡在 candidate
    for row in db.conn.execute(
        "SELECT id FROM memory WHERE subject_id=? AND status='candidate' AND day_key<? "
        "ORDER BY importance DESC, created_at ASC LIMIT 200",
        (ctx.subject, ctx.day_key),
    ).fetchall():
        if row["id"] not in work:
            work.append(row["id"])

    ctx.work_ids = work
    ctx.notes["backlog"] = backlog
    start_iso, end_iso = day_bounds_iso(ctx.day_key)
    by_kind = {
        row["kind"]: row["c"]
        for row in db.conn.execute(
            "SELECT kind, COUNT(*) c FROM memory WHERE subject_id=? AND day_key=? "
            "AND status<>'deleted' GROUP BY kind",
            (ctx.subject, ctx.day_key),
        ).fetchall()
    }
    return {
        "day_key": ctx.day_key,
        "memories": len(rows),
        "work": len(work),
        "backlog": backlog,
        "by_kind": by_kind,
        "feedback": _scalar(
            db,
            "SELECT COUNT(*) FROM feedback WHERE subject_id=? AND ts>=? AND ts<?",
            (ctx.subject, start_iso, end_iso),
        ),
        "recalls": _scalar(
            db,
            "SELECT COUNT(*) FROM recall_log WHERE subject_id=? AND ts>=? AND ts<?",
            (ctx.subject, start_iso, end_iso),
        ),
    }


def s2_dedup(ctx: SleepContext) -> dict[str, Any]:
    """去重合并，并回收空分组。"""
    near = float(ctx.config.get("dedup.near", 0.92))
    stats = dup.consolidate(ctx.db, subject=ctx.subject, near=near)
    gc = groups.gc(ctx.db, subject=ctx.subject)
    return {
        "clusters": stats["groups"],
        "candidates": stats["candidates"],
        "merged": stats["merged"],
        "skipped": stats["skipped"],
        "groups_archived": gc["archived"],
    }


def s3_relations(ctx: SleepContext) -> dict[str, Any]:
    """实体/关系补抽 + 共现 + 传递闭包 + 关系休眠。"""
    db = ctx.db
    if ctx.work_ids:
        backfill = relations.backfill(db, subject=ctx.subject, memory_ids=ctx.work_ids)
    else:
        backfill = {
            "scanned": 0,
            "mentions": 0,
            "relations_created": 0,
            "relations_reinforced": 0,
        }
    co = relations.cooccurrence(
        db,
        subject=ctx.subject,
        min_count=int(ctx.config.get("net.pmi_min_count", 5)),
        min_pmi=float(ctx.config.get("net.pmi_min", 1.5)),
    )
    trans = relations.transitive_closure(
        db,
        subject=ctx.subject,
        max_new=int(ctx.config.get("net.transitive_max_new", 5000)),
    )
    dormant = relations.tick(
        db,
        subject=ctx.subject,
        days=int(ctx.config.get("net.rel_dormant_days", 90)),
        weight=float(ctx.config.get("net.rel_dormant_weight", 0.3)),
    )
    return {
        "scanned": backfill["scanned"],
        "mentions": backfill["mentions"],
        "relations_created": backfill["relations_created"],
        "relations_reinforced": backfill["relations_reinforced"],
        "cooccurrence": {"pairs": co["pairs"], "created": co["created"]},
        "transitive_created": trans["created"],
        "dormant": dormant["dormant"],
    }


def s4_conflicts(ctx: SleepContext) -> dict[str, Any]:
    stats = conflict.run(ctx.db, subject=ctx.subject)
    return {
        "found": stats["found"],
        "created": stats["created"],
        "skipped": stats["skipped"],
    }


def s5_patterns(ctx: SleepContext) -> dict[str, Any]:
    """规律 + 经验。经验只来自四段齐全的结构化记忆。"""
    mined = patterns.mine(
        ctx.db,
        subject=ctx.subject,
        day_key=ctx.day_key,
        run_id=ctx.run_id,
        window_days=int(ctx.cfg("window_days", 90)),
        stale_days=int(ctx.cfg("stale_days", 30)),
    )
    exp = experience.build(ctx.db, subject=ctx.subject, run_id=ctx.run_id)
    return {
        "patterns_created": mined["created"],
        "patterns_updated": mined["updated"],
        "patterns_stale": mined["stale"],
        "patterns_by_kind": mined["by_kind"],
        "experience_created": exp["created"],
        "experience_updated": exp["updated"],
        "experience_incomplete": exp["incomplete"],
        "experience_invalid": exp["invalid"],
    }


def s6_traits(ctx: SleepContext) -> dict[str, Any]:
    stats = derive_trait(
        ctx.db, subject=ctx.subject, day_key=ctx.day_key, run_id=ctx.run_id, config=ctx.config
    )
    return {
        "candidates": stats["candidates"],
        "created": stats["created"],
        "versioned": stats["versioned"],
        "demoted": stats["demoted"],
        "unchanged": stats["unchanged"],
        "updated_in_place": stats["updated_in_place"],
        "retired": stats["retired"],
        "capacity_retired": stats["capacity_retired"],
        "active": stats["active"],
        "established": stats["established"],
    }


def s7_reflect(ctx: SleepContext) -> dict[str, Any]:
    """生命周期（按等级淡忘）+ 七问反思。

    自动淡忘受 ``lifecycle.enabled``（默认 False）控制：关时这里**不写任何状态**，
    只在报告里标 ``lifecycle_skipped: True``；显式 ``memory_lifecycle`` 不受此键限制。
    """
    lifecycle_enabled = bool(ctx.config.get("lifecycle.enabled", False))
    if lifecycle_enabled:
        tick = lifecycle.tick(ctx.db, subject=ctx.subject)
        lifecycle_moved = tick["moved"]
        lifecycle_dry_run = tick["dry_run"]
    else:
        lifecycle_moved = 0
        lifecycle_dry_run = False
    reflection_stats = reflection.write(
        ctx.db,
        subject=ctx.subject,
        day_key=ctx.day_key,
        run_id=ctx.run_id,
        due_days=int(ctx.config.get("surface.due_days", 30)),
    )
    result = {
        "lifecycle_moved": lifecycle_moved,
        "lifecycle_dry_run": lifecycle_dry_run,
        "reflection_id": reflection_stats["id"],
        "reflection_created": reflection_stats["created"],
        "conflicts_open": reflection_stats["metrics"]["conflicts_open"],
        "memories": reflection_stats["metrics"]["memories"],
        "quality": reflection_stats.get("quality"),
        "defects": len(reflection_stats.get("defects") or []),
    }
    if not lifecycle_enabled:
        # 只在跳过的这一代加字段：打开时报告与 0.39.0 逐字节一致。
        result["lifecycle_skipped"] = True
    return result


def s8_snapshot(ctx: SleepContext) -> dict[str, Any]:
    if not bool(ctx.cfg("snapshot", True)):
        return {"skipped": True, "reason": "snapshot disabled"}
    if ctx.backup_dir is None:
        return {"skipped": True, "reason": "no backup dir"}
    data = snapshot.create(
        ctx.db,
        day_key=ctx.day_key,
        out_dir=ctx.backup_dir,
        verify=bool(ctx.cfg("verify_snapshot", True)),
    )
    return data


STAGE_FNS = {
    "s1_collect": s1_collect,
    "s2_dedup": s2_dedup,
    "s3_relations": s3_relations,
    "s4_conflicts": s4_conflicts,
    "s5_patterns": s5_patterns,
    "s6_traits": s6_traits,
    "s7_reflect": s7_reflect,
    "s8_snapshot": s8_snapshot,
}

STAGES = tuple(STAGE_FNS)
