"""睡眠编排器：8 阶段 DAG、checkpoint 续跑、dry-run、日快照、睡眠报告。

设计要点（docs/03 §9）：

- **顺序固定**：s1→s8，不并行。睡眠不是吞吐任务，是可解释的整理流程；
- **一阶段一事务**：阶段成功才写 checkpoint；崩溃后从最后成功阶段继续；
- **重跑幂等**：每个阶段自己保证重复执行不新增派生行；
- **dry-run**：整轮放在一个事务里，最后 ROLLBACK，只写报告文件。

``s8 snapshot`` 是唯一不在事务里的阶段：``VACUUM INTO`` 不能跑在事务中，
所以它单独执行，再开一个小事务写 checkpoint。dry-run 直接跳过快照。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.config import Config
from ..base.db import Database
from ..base.ids import ulid
from .common import yesterday_key
from .stages import STAGE_FNS, STAGES, SleepContext


class _Rollback(Exception):
    """dry-run 的正常结束信号。"""


def _paths(db: Database, cfg: Config) -> tuple[Path, Path]:
    if db.path == ":memory:":
        base = Path.cwd()
    else:
        base = Path(db.path).resolve().parent
    report_dir = Path(cfg.get("sleep.report_dir") or (base / "sleep"))
    backup_dir = Path(cfg.get("sleep.backup_dir") or (base / "backups"))
    return report_dir, backup_dir


def _checkpoint(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["checkpoint_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resumable(db: Database, subject: str, day_key: str) -> Any | None:
    return db.conn.execute(
        "SELECT * FROM sleep_run WHERE subject_id=? AND day_key=? AND status IN ('running','partial') "
        "ORDER BY started_at DESC LIMIT 1",
        (subject, day_key),
    ).fetchone()


def _create_run(db: Database, *, subject: str, day_key: str, trigger: str) -> str:
    run_id = ulid()
    ts = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO sleep_run(id, subject_id, trigger, day_key, started_at, status, stage, "
            "checkpoint_json, stats_json) VALUES(?,?,?,?,?,'running','s1_collect','{}','{}')",
            (run_id, subject, trigger, day_key, ts),
        )
        events.append(
            conn,
            actor="system:sleep",
            op="sleep.run",
            target_tbl="sleep_run",
            target_id=run_id,
            subject_id=subject,
            payload={"day_key": day_key, "trigger": trigger},
        )
    return run_id


def _save_stage(
    conn: Any,
    *,
    run_id: str,
    subject: str,
    day_key: str,
    checkpoint: dict[str, Any],
    name: str,
    stats: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(checkpoint)
    updated[name] = {"ok": True, "at": now_iso(), "stats": stats}
    conn.execute(
        "UPDATE sleep_run SET stage=?, checkpoint_json=? WHERE id=?",
        (name, json.dumps(updated, ensure_ascii=False), run_id),
    )
    events.append(
        conn,
        actor="system:sleep",
        op="sleep.stage",
        target_tbl="sleep_run",
        target_id=run_id,
        subject_id=subject,
        payload={"day_key": day_key, "stage": name, "stats": stats},
    )
    return updated


def _write_report(
    ctx: SleepContext,
    *,
    results: list[dict[str, Any]],
    status: str,
    error: str | None,
    elapsed: float,
) -> str | None:
    if ctx.report_dir is None:
        return None
    directory = ctx.report_dir / "reports"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"sleep-{ctx.day_key}-{ctx.run_id}"
        md_path = directory / f"{stem}.md"
        json_path = directory / f"{stem}.json"
        lines = [
            f"# 睡眠报告 {ctx.day_key}",
            "",
            f"- run: `{ctx.run_id}`",
            f"- 触发: {ctx.trigger}",
            f"- 状态: **{status}**",
            f"- 用时: {elapsed:.3f}s",
            f"- 工作集: {len(ctx.work_ids)} 条（backlog {ctx.notes.get('backlog', 0)}）",
            "",
            "| 阶段 | 状态 | 耗时 | 结果 |",
            "|---|---|---|---|",
        ]
        for item in results:
            mark = "跳过" if item.get("skipped") else ("✓" if item.get("ok") else "✗")
            stats = item.get("stats") or {}
            brief = json.dumps(stats, ensure_ascii=False)[:240]
            lines.append(f"| {item['name']} | {mark} | {item.get('ms', 0)} ms | `{brief}` |")
        if error:
            lines += ["", "## 错误", "", "```", error, "```"]
        lines += ["", "> 这只是一份整理记录，不是新事实。"]
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        json_path.write_text(
            json.dumps(
                {
                    "day_key": ctx.day_key,
                    "run_id": ctx.run_id,
                    "trigger": ctx.trigger,
                    "status": status,
                    "elapsed_s": round(elapsed, 4),
                    "backlog": ctx.notes.get("backlog", 0),
                    "work": len(ctx.work_ids),
                    "stages": results,
                    "error": error,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return str(md_path)
    except OSError as exc:  # 报告写失败不该让整轮睡眠失败
        ctx.notes["report_error"] = str(exc)
        return None


def _finish(
    db: Database,
    *,
    run_id: str,
    subject: str,
    day_key: str,
    status: str,
    checkpoint: dict[str, Any],
    results: list[dict[str, Any]],
    error: str | None,
    elapsed: float,
    report_path: str | None,
    backlog: int,
) -> None:
    summary = {
        "stages": results,
        "backlog": backlog,
        "elapsed_s": round(elapsed, 4),
        "report_path": report_path,
    }
    with db.tx() as conn:
        conn.execute(
            "UPDATE sleep_run SET status=?, finished_at=?, checkpoint_json=?, stats_json=?, error=? "
            "WHERE id=?",
            (
                status,
                now_iso(),
                json.dumps(checkpoint, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                error,
                run_id,
            ),
        )
        events.append(
            conn,
            actor="system:sleep",
            op="sleep.finish",
            target_tbl="sleep_run",
            target_id=run_id,
            subject_id=subject,
            payload={"day_key": day_key, "status": status, "elapsed_s": round(elapsed, 4)},
        )


def _context(
    db: Database,
    *,
    cfg: Config,
    subject: str,
    day_key: str,
    run_id: str,
    trigger: str,
    dry_run: bool,
) -> SleepContext:
    report_dir, backup_dir = _paths(db, cfg)
    return SleepContext(
        db=db,
        subject=subject,
        day_key=day_key,
        run_id=run_id,
        config=cfg,
        trigger=trigger,
        dry_run=dry_run,
        report_dir=report_dir,
        backup_dir=backup_dir,
    )


def _dry_run(ctx: SleepContext, stop_at: str | None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    error: str | None = None
    status = "dry_run"
    started = time.perf_counter()
    try:
        with ctx.db.tx():
            for name in STAGES:
                if name == "s8_snapshot":
                    results.append(
                        {
                            "name": name,
                            "ok": True,
                            "skipped": True,
                            "ms": 0,
                            "stats": {"skipped": True, "reason": "dry_run"},
                        }
                    )
                    continue
                t0 = time.perf_counter()
                try:
                    stats = STAGE_FNS[name](ctx)
                except Exception as exc:  # 计算失败：回滚，报告里留错
                    results.append(
                        {
                            "name": name,
                            "ok": False,
                            "skipped": False,
                            "ms": int((time.perf_counter() - t0) * 1000),
                            "stats": {},
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    error = f"{name}: {type(exc).__name__}: {exc}"
                    raise
                results.append(
                    {
                        "name": name,
                        "ok": True,
                        "skipped": False,
                        "ms": int((time.perf_counter() - t0) * 1000),
                        "stats": stats,
                    }
                )
                if stop_at == name:
                    break
            raise _Rollback()
    except _Rollback:
        status = "dry_run"
    except Exception:  # 上面已经记过 error
        status = "failed"
    elapsed = time.perf_counter() - started
    report = _write_report(ctx, results=results, status=status, error=error, elapsed=elapsed)
    return {
        "run_id": ctx.run_id,
        "day_key": ctx.day_key,
        "status": status,
        "dry_run": True,
        "stages": results,
        "error": error,
        "report_path": report,
        "elapsed_s": round(elapsed, 4),
        "rolled_back": True,
    }


def run(
    db: Database,
    *,
    subject: str = "self",
    day_key: str | None = None,
    trigger: str = "manual",
    dry_run: bool = False,
    resume: bool = True,
    stop_at: str | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """跑一轮睡眠。``stop_at`` 用于演练/测试：在该阶段成功后停下，标记 partial。"""
    if stop_at is not None and stop_at not in STAGES:
        raise ValueError(f"unknown stage: {stop_at}")
    cfg = config or Config()
    day = day_key or yesterday_key()

    if dry_run:
        return _dry_run(
            _context(
                db,
                cfg=cfg,
                subject=subject,
                day_key=day,
                run_id=f"dry-{ulid()}",
                trigger=trigger,
                dry_run=True,
            ),
            stop_at,
        )

    existing = _resumable(db, subject, day) if resume else None
    if existing is not None:
        run_id = existing["id"]
        checkpoint = _checkpoint(existing)
        trigger = existing["trigger"] or trigger
    else:
        run_id = _create_run(db, subject=subject, day_key=day, trigger=trigger)
        checkpoint = {}

    ctx = _context(
        db,
        cfg=cfg,
        subject=subject,
        day_key=day,
        run_id=run_id,
        trigger=trigger,
        dry_run=False,
    )
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    error: str | None = None
    status = "ok"
    stopped = False

    for name in STAGES:
        prior = checkpoint.get(name)
        if isinstance(prior, dict) and prior.get("ok"):
            results.append(
                {
                    "name": name,
                    "ok": True,
                    "skipped": True,
                    "ms": 0,
                    "stats": prior.get("stats") or {},
                }
            )
            continue

        t0 = time.perf_counter()
        try:
            if name == "s8_snapshot":
                stats = STAGE_FNS[name](ctx)
                with db.tx() as conn:
                    checkpoint = _save_stage(
                        conn,
                        run_id=run_id,
                        subject=subject,
                        day_key=day,
                        checkpoint=checkpoint,
                        name=name,
                        stats=stats,
                    )
            else:
                with db.tx() as conn:
                    stats = STAGE_FNS[name](ctx)
                    checkpoint = _save_stage(
                        conn,
                        run_id=run_id,
                        subject=subject,
                        day_key=day,
                        checkpoint=checkpoint,
                        name=name,
                        stats=stats,
                    )
        except Exception as exc:
            error = f"{name}: {type(exc).__name__}: {exc}"
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "skipped": False,
                    "ms": int((time.perf_counter() - t0) * 1000),
                    "stats": {},
                    "error": error,
                }
            )
            status = "partial" if any(item.get("ok") for item in results) else "failed"
            break

        results.append(
            {
                "name": name,
                "ok": True,
                "skipped": False,
                "ms": int((time.perf_counter() - t0) * 1000),
                "stats": stats,
            }
        )
        if stop_at == name:
            stopped = True
            status = "partial"
            break

    if not error and not stopped:
        status = "ok"
    elapsed = time.perf_counter() - started
    report = _write_report(ctx, results=results, status=status, error=error, elapsed=elapsed)
    _finish(
        db,
        run_id=run_id,
        subject=subject,
        day_key=day,
        status=status,
        checkpoint=checkpoint,
        results=results,
        error=error,
        elapsed=elapsed,
        report_path=report,
        backlog=ctx.notes.get("backlog", 0),
    )
    return {
        "run_id": run_id,
        "day_key": day,
        "status": status,
        "dry_run": False,
        "resumed": existing is not None,
        "stages": results,
        "error": error,
        "report_path": report,
        "elapsed_s": round(elapsed, 4),
        "backlog": ctx.notes.get("backlog", 0),
    }


def status(
    db: Database,
    *,
    subject: str = "self",
    day_key: str | None = None,
) -> dict[str, Any]:
    """最近一次睡眠运行（可按天过滤）；含是否可续跑与报告路径。"""
    sql = "SELECT * FROM sleep_run WHERE subject_id=?"
    params: list[Any] = [subject]
    if day_key:
        sql += " AND day_key=?"
        params.append(day_key)
    sql += " ORDER BY started_at DESC LIMIT 1"
    row = db.conn.execute(sql, params).fetchone()
    if row is None:
        return {"run": None, "resumable": False, "stages": {}}
    try:
        stats = json.loads(row["stats_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        stats = {}
    return {
        "run": {
            "id": row["id"],
            "day_key": row["day_key"],
            "trigger": row["trigger"],
            "status": row["status"],
            "stage": row["stage"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
        },
        "resumable": row["status"] in ("running", "partial"),
        "stages": _checkpoint(row),
        "report_path": stats.get("report_path"),
        "elapsed_s": stats.get("elapsed_s"),
    }


def catchup(
    db: Database,
    *,
    subject: str = "self",
    days: int | None = None,
    trigger: str = "catchup",
    config: Config | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """补跑最近 ``days`` 天里漏掉的睡眠（只跑有数据或有未完成 run 的天）。"""
    cfg = config or Config()
    span = int(days if days is not None else cfg.get("sleep.backfill_days", 7))
    span = max(1, min(span, 365))
    today = date.today()
    covered = {
        row["day_key"]
        for row in db.conn.execute(
            "SELECT day_key FROM sleep_run WHERE subject_id=? AND status='ok'", (subject,)
        ).fetchall()
    }
    runs: list[dict[str, Any]] = []
    skipped = 0
    for offset in range(span, 0, -1):
        day = (today - timedelta(days=offset)).isoformat()
        if day in covered:
            skipped += 1
            continue
        has_memory = db.conn.execute(
            "SELECT 1 FROM memory WHERE subject_id=? AND day_key=? AND status<>'deleted' LIMIT 1",
            (subject, day),
        ).fetchone()
        has_episode = db.conn.execute(
            "SELECT 1 FROM episode WHERE subject_id=? AND day_key=? LIMIT 1", (subject, day)
        ).fetchone()
        if not has_memory and not has_episode and _resumable(db, subject, day) is None:
            skipped += 1
            continue
        runs.append(
            run(
                db,
                subject=subject,
                day_key=day,
                trigger=trigger,
                resume=True,
                config=cfg,
                dry_run=dry_run,
            )
        )
    failed = [item for item in runs if item["status"] not in ("ok", "dry_run")]
    return {
        "days": span,
        "ran": len(runs),
        "skipped": skipped,
        "failed": len(failed),
        "runs": runs,
        "status": "ok" if not failed else "partial",
    }


__all__ = ["run", "status", "catchup", "STAGES"]
