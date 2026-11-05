"""大睡一场：把已有的记忆按天从头睡一遍（默认全部，可限定日期范围）。

一夜一夜地补：每天先消化当天的流水（有模型就交给「鲸眠」会话，没有就用模板 digest），
再跑固定的 8 阶段巩固。单天失败不拖垮整轮——出错的记下来，接着睡下一天。

这不是「重置」：原文只追加、日记按天补齐，重复跑是幂等的（已消化的天不会再写一篇）。
用法见 ``pmem sleep big --help``。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..base.errors import PmemError
from ..memory import journal
from ..sleep import orchestrator, whale


def days_with_lines(db: Any, subject: str) -> list[str]:
    """有原文（journal_line）的所有日期，升序。"""
    rows = db.conn.execute(
        "SELECT DISTINCT day_key FROM journal_line WHERE subject_id=? AND day_key IS NOT NULL "
        "ORDER BY day_key",
        (subject,),
    ).fetchall()
    return [str(row["day_key"]) for row in rows]


def pending_count(db: Any, subject: str, day_key: str) -> int:
    row = db.conn.execute(
        "SELECT COUNT(*) c FROM journal_line jl WHERE jl.subject_id=? AND jl.day_key=? "
        "AND jl.episode_id IS NULL AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
        "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id)",
        (subject, day_key),
    ).fetchone()
    return int(row["c"] or 0) if row is not None else 0


def select_days(
    db: Any,
    subject: str,
    *,
    start: str | None = None,
    end: str | None = None,
    skip_digested: bool = False,
    limit: int = 0,
) -> list[str]:
    days = days_with_lines(db, subject)
    if start:
        days = [day for day in days if day >= start]
    if end:
        days = [day for day in days if day <= end]
    if skip_digested:
        days = [day for day in days if pending_count(db, subject, day) > 0]
    if limit > 0:
        days = days[:limit]
    return days


def _diary_brief(result: dict[str, Any], *, mode: str | None = None) -> dict[str, Any]:
    """统一日记结果的展示口径：facts 一律是条数，别把列表混进进度行。"""
    return {
        "ok": bool(result.get("ok")),
        "mode": result.get("mode") or mode,
        "episode_id": result.get("episode_id"),
        "facts": len(result.get("facts") or []),
        "skipped": result.get("skipped"),
        "reason": result.get("reason"),
    }


def run(
    db: Any,
    *,
    subject: str,
    store: Any,
    app: Any,
    config: Any,
    model: Any = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 0,
    dry_run: bool = False,
    use_whale: bool = True,
    skip_digested: bool = False,
    on_day: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """按天跑「消化 + 8 阶段」；返回逐天结果与汇总。"""
    days = select_days(db, subject, start=start, end=end, skip_digested=skip_digested, limit=limit)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    usage_total = 0
    whale_days = 0
    template_days = 0
    skipped_days = 0
    failed_days = 0

    wake_text = ""
    if use_whale and model is not None:
        wake = (app.wake(budget=300) or {}).get("data") or {}
        wake_text = str(wake.get("live") or wake.get("text") or "")

    for day in days:
        item: dict[str, Any] = {"day": day, "diary": None, "sleep": None, "error": ""}
        pending = pending_count(db, subject, day)
        try:
            if skip_digested and pending == 0:
                item["diary"] = {"ok": True, "skipped": "digested"}
                item["sleep"] = {"ok": True, "skipped": "digested"}
                skipped_days += 1
                results.append(item)
                if on_day:
                    on_day(item)
                continue
            if pending == 0:
                item["diary"] = {"ok": True, "skipped": "no_lines"}
            elif use_whale and model is not None:
                try:
                    outcome = whale.run(
                        db,
                        subject=subject,
                        day_key=day,
                        store=store,
                        app=app,
                        config=config,
                        model=model,
                        dry_run=dry_run,
                        wake_text=wake_text,
                        recent_episodes=whale.recent_episodes(db, subject=subject, day_key=day, limit=3),
                    )
                    usage_total += int(outcome.get("usage_total") or 0)
                    item["diary"] = {
                        "ok": bool(outcome.get("ok")),
                        "mode": "whale",
                        "verdict": outcome.get("verdict"),
                        "episode_id": (outcome.get("diary") or {}).get("episode_id"),
                        "facts": len((outcome.get("diary") or {}).get("facts") or []),
                        "transcript": outcome.get("transcript"),
                    }
                    whale_days += 1
                    if not outcome.get("ok"):
                        raise RuntimeError(outcome.get("reason") or outcome.get("verdict") or "鲸眠没写成")
                except Exception as exc:  # noqa: BLE001 鲸眠失败退回模板，不能让这一天没日记
                    item["whale_error"] = f"{type(exc).__name__}: {exc}"
                    item["diary"] = _diary_brief(journal.digest(
                        db, subject=subject, day_key=day, store=store, config=config, dry_run=dry_run
                    ))
                    template_days += 1
            else:
                item["diary"] = _diary_brief(journal.digest(
                    db, subject=subject, day_key=day, store=store, config=config, dry_run=dry_run
                ))
                template_days += 1
            item["sleep"] = orchestrator.run(
                db, subject=subject, day_key=day, dry_run=dry_run, config=config
            )
        except Exception as exc:  # noqa: BLE001 单天失败不拖垮整轮
            item["error"] = f"{type(exc).__name__}: {exc}"
            failed_days += 1
        results.append(item)
        if on_day:
            on_day(item)

    elapsed = round(time.perf_counter() - started, 1)
    return {
        "ok": failed_days == 0,
        "days": len(results),
        "range": {"start": start, "end": end},
        "mode": "whale" if (use_whale and model is not None) else "template",
        "whale_days": whale_days,
        "template_days": template_days,
        "skipped_days": skipped_days,
        "failed_days": failed_days,
        "tokens": usage_total,
        "elapsed_s": elapsed,
        "dry_run": dry_run,
        "results": results,
    }


def validate_range(start: str | None, end: str | None) -> None:
    """日期格式检查；错了就抛 PmemError，别让 CLI 跑到一半才炸。"""
    from datetime import date

    for name, value in (("start", start), ("end", end)):
        if not value:
            continue
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise PmemError(f"{name} 不是 YYYY-MM-DD：{value}", code="E_VALIDATION", field=name) from exc
    if start and end and start > end:
        raise PmemError(f"start({start}) 晚于 end({end})", code="E_VALIDATION", field="start")
