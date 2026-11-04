"""M9 常驻调度器：把「睡眠 / 周报 / 备份 / 监控 / 月报」变成有状态的定时任务。

两种用法：

- ``pmem daemon``：前台长期运行，每 ``runtime.tick_seconds`` 检查一次；
- ``pmem daemon --once``：给 Windows 任务计划程序 / cron 调用，一次 tick 后退出。

调度语义是**补偿式**的：进程不在时不会丢任务——``job.next_run_at`` 留在过去，
下一次 tick 会立刻补跑；睡眠会通过 ``sleep.catchup`` 把漏掉且**有数据**的天补上。
这不是「保证 30 天不关机」，而是「关机再开也不会假装那几天没发生」。
"""

from __future__ import annotations

import calendar
import time
from datetime import UTC, date, datetime, time as dtime, timedelta
from typing import Any, Callable

from ..base.clock import to_iso
from ..base.config import Config
from ..base.db import Database
from . import jobs, maintenance

Runner = Callable[[Database, Config, str, str, dict[str, Any]], dict[str, Any]]


def _local(value: datetime | None = None) -> datetime:
    dt = value or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def _hhmm(value: str, fallback: str) -> tuple[int, int]:
    try:
        hour, minute = value.split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    hour, minute = fallback.split(":", 1)
    return int(hour), int(minute)


def _at(day: date, value: str, fallback: str) -> datetime:
    hour, minute = _hhmm(value, fallback)
    return datetime.combine(day, dtime(hour=hour, minute=minute)).astimezone()


def _month_day(year: int, month: int, wanted: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, max(1, min(wanted, last)))


def next_after(kind: str, now: datetime, config: Config) -> datetime:
    """严格晚于 ``now`` 的下一个计划时刻（本地时区，返回带时区 datetime）。"""
    local = _local(now)
    today = local.date()

    if kind == "sleep":
        candidate = _at(today, str(config.get("sleep.at", "03:30")), "03:30")
        return candidate if candidate > local else candidate + timedelta(days=1)

    if kind == "reflect_week":
        wanted = int(config.get("runtime.reflect_weekday", 1) or 1)
        wanted = max(1, min(wanted, 7))
        delta = (wanted - today.isoweekday()) % 7
        candidate = _at(today + timedelta(days=delta), str(config.get("runtime.reflect_at", "03:45")), "03:45")
        return candidate if candidate > local else candidate + timedelta(days=7)

    if kind == "backup":
        candidate = _at(today, str(config.get("runtime.backup_at", "04:00")), "04:00")
        return candidate if candidate > local else candidate + timedelta(days=1)

    if kind == "monitor":
        candidate = _at(today, str(config.get("runtime.monitor_at", "04:30")), "04:30")
        return candidate if candidate > local else candidate + timedelta(days=1)

    if kind == "monthly":
        wanted = int(config.get("runtime.monthly_day", 1) or 1)
        day = _month_day(today.year, today.month, wanted)
        candidate = _at(day, str(config.get("runtime.monthly_at", "04:15")), "04:15")
        if candidate <= local:
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
            day = _month_day(year, month, wanted)
            candidate = _at(day, str(config.get("runtime.monthly_at", "04:15")), "04:15")
        return candidate

    raise ValueError(f"未知任务类型：{kind}")


def ensure_jobs(db: Database, *, now: datetime | None = None, config: Config | None = None) -> list[str]:
    """登记缺失的常驻任务；已存在（含 dead）不覆盖。返回新建的 kind。"""
    cfg = config or Config()
    if not bool(cfg.get("runtime.enabled", True)):
        return []
    created: list[str] = []
    for kind in jobs.JOB_TYPES:
        if jobs.get(db, kind) is not None:
            continue
        due = next_after(kind, now or datetime.now(UTC), cfg)
        if jobs.ensure(db, kind=kind, next_run_at=due):
            created.append(kind)
    return created


def _previous_month(today: date) -> str:
    year = today.year - (1 if today.month == 1 else 0)
    month = 12 if today.month == 1 else today.month - 1
    return f"{year:04d}-{month:02d}"


def default_runner(
    db: Database,
    cfg: Config,
    subject: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """真实执行器；测试可注入假执行器验证 30 天不丢任务。"""
    from . import backup, monitor, monthly

    if kind == "sleep":
        from ..sleep import orchestrator

        days = int(cfg.get("sleep.backfill_days", 7) or 7)
        result = orchestrator.catchup(
            db,
            subject=subject,
            days=days,
            trigger="schedule",
            config=cfg,
        )
        return {
            "status": result.get("status"),
            "ran": result.get("ran", 0),
            "skipped": result.get("skipped", 0),
            "failed": result.get("failed", 0),
        }

    if kind == "reflect_week":
        from ..reflect import report

        previous = date.today() - timedelta(days=1)
        data = report.write(db, subject=subject, key=report.week_key(previous))
        return {"period": data.get("period"), "created": data.get("created"), "quality": data.get("quality")}

    if kind == "backup":
        # 定时任务 = 日快照：kind 必须是 daily，prune 才按 runtime.daily_keep 轮转；
        # 标成 manual 会让日快照既不受 7 天保留、又混进手动备份的 14 份名额里。
        result = backup.run(db, config=cfg, subject=subject, kind="daily", tag="scheduled")
        return {
            "ok": result.get("ok"),
            "snapshot_id": result.get("snapshot_id"),
            "kind": "daily",
            "size_bytes": result.get("size_bytes"),
            "memory_count": result.get("memory_count"),
            "pruned": len(result.get("pruned") or []),
        }

    if kind == "monthly":
        result = monthly.generate(
            db, config=cfg, subject=subject, month=_previous_month(date.today())
        )
        return {
            "ok": result.get("ok"),
            "month": result.get("month"),
            "path": result.get("path"),
            "memories_created": (result.get("metrics") or {}).get("memories_created"),
        }

    if kind == "monitor":
        result = monitor.collect(db, config=cfg, subject=subject, record=True)
        return {"ok": result.get("ok"), "status": result.get("status")}

    raise ValueError(f"未知任务类型：{kind}")


def tick(
    db: Database,
    *,
    now: datetime | None = None,
    config: Config | None = None,
    subject: str = "self",
    runner: Runner | None = None,
    max_jobs: int = 8,
) -> dict[str, Any]:
    """检查并执行所有到期任务；单次最多 ``max_jobs`` 条，防止一次卡太久。"""
    cfg = config or Config()
    run_at = now or datetime.now(UTC)
    created = ensure_jobs(db, now=run_at, config=cfg)
    recovered = jobs.recover_stale(
        db, now=run_at, stale_minutes=int(cfg.get("runtime.stale_minutes", 120) or 120)
    )
    # 维护窗口闸：只做上面两件「不动业务数据」的事（登记缺失任务 + 自愈 stale running），
    # 不执行任何 job、也不写日记。next_run_at 保持不动，窗口结束后下一次 tick 自动补跑。
    gate = maintenance.active(db, config=cfg, at=run_at)
    if gate.get("active"):
        pending = jobs.list_jobs(db)
        due_at = to_iso(run_at)
        skipped = [
            job["type"]
            for job in pending
            if job.get("state") == "pending" and str(job.get("next_run_at") or "") <= due_at
        ]
        return {
            "ok": True,
            "at": to_iso(run_at),
            "maintenance": True,
            "reason": gate.get("reason") or "",
            "until": gate.get("until") or "",
            "created": created,
            "recovered": recovered,
            "journal": {"ok": True, "skipped": "maintenance"},
            "ran": [],
            "skipped": skipped,
            "jobs": pending,
        }
    execute = runner or default_runner
    try:
        journal_note = journal_tick(db, cfg, subject)
    except Exception as exc:  # 日记是附加动作，不能影响调度
        journal_note = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ran: list[dict[str, Any]] = []
    while len(ran) < max_jobs:
        job = jobs.claim_next(db, run_at)
        if job is None:
            break
        started = time.perf_counter()
        try:
            result = execute(db, cfg, subject, job["type"], job.get("payload") or {})
        except Exception as exc:  # 任务失败不能拖垮调度器
            failed = jobs.fail(
                db,
                job,
                error=f"{type(exc).__name__}: {exc}",
                now=run_at,
                max_attempts=int(cfg.get("runtime.max_attempts", 3) or 3),
                retry_minutes=int(cfg.get("runtime.retry_minutes", 10) or 10),
            )
            ran.append(
                {
                    "kind": job["type"],
                    "ok": False,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                    "job": failed,
                }
            )
            continue
        due = next_after(job["type"], run_at + timedelta(seconds=1), cfg)
        jobs.complete(db, job, next_run_at=due, result=result)
        ran.append(
            {
                "kind": job["type"],
                "ok": True,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "next_run_at": to_iso(due),
                "result": result,
            }
        )
    pending = jobs.list_jobs(db)
    return {
        "ok": all(item.get("ok") for item in ran),
        "at": to_iso(run_at),
        "maintenance": False,
        "created": created,
        "recovered": recovered,
        "journal": journal_note,
        "ran": ran,
        "jobs": pending,
    }


def journal_tick(db: Database, cfg: Config, subject: str) -> dict[str, Any]:
    """每次 tick 顺带看一眼：该睡（空闲够久 / 跨天）就把当天的流水写成日记。

    这件事**不进任务队列**：它每 10 分钟只做一次两行 SQL 的判断，
    真到期了才写；塞进队列反而要维护 last_run 状态，还容易被 sleep 抢走。
    """
    from ..memory import journal

    if not bool(cfg.get("journal.enabled", True)):
        return {"ok": True, "skipped": "disabled"}
    idle = int(cfg.get("journal.idle_minutes", 45) or 45)
    verdict = journal.due(db, subject=subject, idle_minutes=idle)
    if not verdict.get("due"):
        return {"ok": True, "skipped": verdict.get("reason"), "due": verdict}
    try:
        from ..memory.store import MemoryStore

        store = MemoryStore(db, config=cfg, subject=subject)
        result = journal.digest(
            db, subject=subject, day_key=verdict.get("day_key"), store=store, config=cfg
        )
    except Exception as exc:  # 写日记失败不能拖垮调度器
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "due": verdict}
    kept = int(cfg.get("journal.keep_days", 30) or 30)
    result["pruned"] = journal.prune(db, subject=subject, keep_days=kept)
    return {"ok": True, "due": verdict, "digest": result}


def serve(
    db: Database,
    *,
    config: Config | None = None,
    subject: str = "self",
    interval: int | None = None,
    once: bool = False,
    runner: Runner | None = None,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """前台循环；``once=True`` 时只 tick 一次（给系统计划任务用）。"""
    cfg = config or Config()
    delay = max(5, int(interval or cfg.get("runtime.tick_seconds", 60) or 60))
    last: dict[str, Any] = {}
    while True:
        try:
            last = tick(db, config=cfg, subject=subject, runner=runner)
        except Exception as exc:  # 常驻进程不能因为一次锁冲突就整体退出
            last = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "ran": [],
                "created": [],
                "recovered": [],
                "jobs": [],
            }
        if on_tick:
            on_tick(last)
        if once:
            return last
        time.sleep(delay)


__all__ = ["next_after", "ensure_jobs", "tick", "serve", "default_runner"]
