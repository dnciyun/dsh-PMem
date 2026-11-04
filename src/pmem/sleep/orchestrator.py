"""睡眠编排器：8 阶段 DAG、checkpoint 续跑、dry-run、日快照、睡眠报告。

设计要点（docs/03 §9）：

- **顺序固定**：s1→s8，不并行。睡眠不是吞吐任务，是可解释的整理流程；
- **一阶段一事务**：阶段成功才写 checkpoint；崩溃后从最后成功阶段继续；
- **重跑幂等**：每个阶段自己保证重复执行不新增派生行；
- **续跑 = 一次跑完**：续跑时跳过已提交阶段，但会**重放 s1 的只读查询**把工作集
  （``work_ids`` / ``backlog``）补回内存——否则 s3 会对空工作集空转；
- **跨进程单飞**：阶段事件带 ``idem_key=f"{run_id}:{stage}"``（``event_log.idem_key`` 唯一）。
  后到者先查幂等键直接采纳；抢跑撞键则整个阶段事务回滚、采纳赢家 stats，不产生第二份副作用。
- **dry-run**：整轮放在一个事务里，最后 ROLLBACK，只写报告文件。

``s8 snapshot`` 是唯一不在事务里的阶段：``VACUUM INTO`` 不能跑在事务中，
所以它单独执行，再开一个小事务写 checkpoint。dry-run 直接跳过快照。
"""

from __future__ import annotations

import json
import sqlite3
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


def _stage_idem_key(run_id: str, name: str) -> str:
    """阶段事件的幂等键：``run_id:stage``（docs/03 §9，跨进程单飞靠它）。"""
    return f"{run_id}:{name}"


def _committed_stage(db: Database, run_id: str, name: str) -> dict[str, Any] | None:
    """同一 run 的该阶段是否已由别的进程提交？返回其 stats；没有返回 None。

    阶段事件和阶段写入在同一个事务里提交，所以"事件存在"等价于"这个阶段已经落盘"。
    """
    row = events.find_by_idem_key(db.conn, _stage_idem_key(run_id, name))
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    stats = payload.get("stats")
    return stats if isinstance(stats, dict) else {}


def _adopt_stage(checkpoint: dict[str, Any], name: str, stats: dict[str, Any]) -> dict[str, Any]:
    """把"别人已提交"的阶段记进内存 checkpoint（不写库，交给 _finish 统一落盘）。"""
    updated = dict(checkpoint)
    updated[name] = {"ok": True, "at": now_iso(), "stats": stats, "adopted": True}
    return updated


def _drop_loser_snapshot(
    db: Database, *, keep_stats: dict[str, Any], drop_stats: dict[str, Any]
) -> None:
    """并发 s8 撞键后的收尸：删掉本进程多插的快照行，只留先提交者的。

    ``VACUUM INTO`` 不能进事务（s8 在事务外），所以两个进程可能各插一行同 path 的
    ``snapshot``。阶段事件的幂等键决定了谁是赢家，输家自己那行要清掉。
    """
    keep_id = keep_stats.get("snapshot_id")
    drop_id = drop_stats.get("snapshot_id")
    if not keep_id or not drop_id or keep_id == drop_id:
        return
    with db.tx() as conn:
        conn.execute("DELETE FROM snapshot WHERE id=?", (drop_id,))


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
            run_id=run_id,
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
        # 幂等键即跨进程单飞护栏：同一 run 的同一阶段全局只能提交一次。
        idem_key=_stage_idem_key(run_id, name),
        run_id=run_id,
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
    # 收尾事件按"收尾状态"取幂等键：同一条 run 可以先 partial 收尾、续跑后再 ok 收尾
    # （顺序续跑是合法的多次收尾，两边状态不同 → 键不同）；而并发续跑的两个进程通常
    # 都以 ok 收尾、算出同一个键，后到者撞 UNIQUE 后放弃写状态——正好是单飞。
    # 键里不用"第几次收尾"的计数：读计数与插入不在同一快照里，后到者会算出 +1 而漏网。
    finish_key = f"{run_id}:finish:{status}"
    try:
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
                idem_key=finish_key,
                run_id=run_id,
            )
    except sqlite3.IntegrityError:
        if events.find_by_idem_key(db.conn, finish_key) is None:
            raise
        # 另一进程已用同一状态收尾：它的 status/checkpoint/stats 为准，不覆盖


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
    stats: dict[str, Any] | None = None

    def _replay_collect() -> str | None:
        """续跑时重放 s1 的只读查询，把工作集（work_ids/notes）补回内存。"""
        try:
            STAGE_FNS["s1_collect"](ctx)
            return None
        except Exception as exc:  # noqa: BLE001 工作集重建失败按阶段失败处理
            return f"s1_collect(replay): {type(exc).__name__}: {exc}"

    for name in STAGES:
        prior = checkpoint.get(name)
        if isinstance(prior, dict) and prior.get("ok"):
            # 续跑：s1 已被 checkpoint 跳过，但它的产物只在内存里。重放 s1（纯只读）
            # 把 work_ids/backlog 补回来，否则 s3 会对着空工作集空转——续跑 ≠ 一次跑完。
            # stats 仍沿用 checkpoint 里那次的，不改写已完成阶段的历史。
            if name == "s1_collect":
                replay_error = _replay_collect()
                if replay_error is not None:
                    error = replay_error
                    results.append(
                        {
                            "name": name,
                            "ok": False,
                            "skipped": False,
                            "ms": 0,
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
                    "skipped": True,
                    "ms": 0,
                    "stats": prior.get("stats") or {},
                }
            )
            continue

        # 跨进程单飞：同一 run 的同一阶段只要有人提交过（事件就在同一事务里），
        # 后到者直接采纳它的 stats，不重复执行。
        committed = _committed_stage(db, run_id, name)
        if committed is not None:
            if name == "s1_collect":
                replay_error = _replay_collect()
                if replay_error is not None:
                    error = replay_error
                    results.append(
                        {
                            "name": name,
                            "ok": False,
                            "skipped": False,
                            "ms": 0,
                            "stats": {},
                            "error": error,
                        }
                    )
                    status = "partial" if any(item.get("ok") for item in results) else "failed"
                    break
            checkpoint = _adopt_stage(checkpoint, name, committed)
            results.append(
                {
                    "name": name,
                    "ok": True,
                    "skipped": True,
                    "adopted": True,
                    "ms": 0,
                    "stats": committed,
                }
            )
            if stop_at == name:
                stopped = True
                status = "partial"
                break
            continue

        t0 = time.perf_counter()
        stats = None
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
        except sqlite3.IntegrityError:
            # 抢跑撞了幂等键：另一进程已提交该阶段；本进程的阶段事务整体回滚。
            # 采纳对方的 stats 继续，不把整轮判失败（这就是单飞护栏）。
            committed = _committed_stage(db, run_id, name)
            if committed is None:
                raise
            if name == "s8_snapshot" and isinstance(stats, dict):
                _drop_loser_snapshot(db, keep_stats=committed, drop_stats=stats)
            checkpoint = _adopt_stage(checkpoint, name, committed)
            results.append(
                {
                    "name": name,
                    "ok": True,
                    "skipped": True,
                    "adopted": True,
                    "ms": int((time.perf_counter() - t0) * 1000),
                    "stats": committed,
                }
            )
            if stop_at == name:
                stopped = True
                status = "partial"
                break
            continue
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
