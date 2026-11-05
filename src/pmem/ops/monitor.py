"""M9 监控：把「它还活着、没丢东西、没有变差」变成可判定的检查。

``collect`` 是只读快照；``record=True`` 时把关键数字写进 ``metrics``，供月报和
30 天趋势使用。CLI 的退出码：0 = ok/warn，1 = fail。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..base import events
from ..base.clock import now_iso, parse_iso
from ..base.config import Config
from ..base.db import Database
from . import jobs

SEVERITY = {"ok": 0, "info": 0, "warn": 1, "fail": 2}


def _add(checks: list[dict[str, Any]], name: str, status: str, detail: str, **extra: Any) -> None:
    checks.append({"name": name, "status": status, "detail": detail, **extra})


def _hours_since(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, (datetime.now(parse_iso(value).tzinfo) - parse_iso(value)).total_seconds() / 3600)
    except (ValueError, TypeError):
        return None


def _latest_metric(db: Database, name: str) -> Any | None:
    row = db.conn.execute(
        "SELECT value, day_key, ts FROM metrics WHERE name=? ORDER BY day_key DESC, id DESC LIMIT 1",
        (name,),
    ).fetchone()
    return row


def _recall_pair(db: Database) -> tuple[Any | None, Any | None]:
    rows = db.conn.execute(
        "SELECT value, day_key, ts FROM metrics WHERE name='eval.recall_at_5' "
        "ORDER BY day_key DESC, id DESC LIMIT 2"
    ).fetchall()
    if not rows:
        return None, None
    if len(rows) == 1:
        return rows[0], None
    return rows[0], rows[1]


def _db_size(db: Database) -> int:
    if db.path == ":memory:":
        return 0
    path = Path(db.path)
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def _wal_bytes(db: Database) -> int:
    if db.path == ":memory:":
        return 0
    path = Path(str(db.path) + "-wal")
    return int(path.stat().st_size) if path.exists() else 0


def _add_wal(
    checks: list[dict[str, Any]],
    metrics: dict[str, Any],
    db: Database,
    cfg: Config,
    *,
    record: bool = False,
) -> None:
    """WAL 体积：大 WAL 说明 checkpoint 被长读连接/持续写入拖住了，不代表数据坏。

    ``record=True``（daemon 的定时拍子）时，超过 warn 阈值就真的做一次
    ``Database.checkpoint()``；``record=False`` 的只读快照只报告 + 建议。
    """
    size = _wal_bytes(db)
    metrics["wal_bytes"] = size
    mb = size / 1024 / 1024
    warn_mb = float(cfg.get("runtime.wal_warn_mb", 64) or 64)
    fail_mb = float(cfg.get("runtime.wal_fail_mb", 512) or 512)
    hint = "建议 idle 时调用 Database.checkpoint()（wal_checkpoint(TRUNCATE)），并检查长读连接"
    if mb >= warn_mb and record:
        db.checkpoint()
        trimmed = _wal_bytes(db)
        metrics["wal_bytes_after_checkpoint"] = trimmed
        size = trimmed
        mb = size / 1024 / 1024
        hint = f"已执行 Database.checkpoint()，WAL 降到 {mb:.2f} MB"
    if mb >= fail_mb:
        _add(checks, "wal", "fail", f"WAL {mb:.1f} MB ≥ {fail_mb:.0f} MB；{hint}")
    elif mb >= warn_mb:
        _add(checks, "wal", "warn", f"WAL {mb:.1f} MB ≥ {warn_mb:.0f} MB；{hint}")
    else:
        _add(checks, "wal", "ok", f"WAL {mb:.2f} MB")


def _check_event_anchor(conn: Any) -> tuple[str, str, dict[str, Any]]:
    """调 W2 的 ``events.check_anchor`` 并归一成 (status, detail, extra)。

    锚点未接线时只报 info（不假装系统有锚点，也不把老库判坏）。
    """
    fn = getattr(events, "check_anchor", None)
    if fn is None:
        return "info", "events.check_anchor 未接线（等 W2 锚点）", {"wired": False}
    try:
        result = fn(conn)
    except Exception as exc:  # noqa: BLE001 检查失败不等于锚点错
        return "warn", f"check_anchor 失败：{type(exc).__name__}: {exc}", {"wired": True, "error": str(exc)}
    if isinstance(result, bool):
        return ("ok" if result else "warn"), ("锚点一致" if result else "锚点与事件链尾不一致"), {"wired": True, "ok": result}
    if isinstance(result, dict):
        ok = result.get("ok")
        if ok is None:
            ok = result.get("status") == "ok"
        detail = str(result.get("detail") or result.get("message") or result)
        return ("ok" if ok else "warn"), detail, {"wired": True, "ok": bool(ok), "anchor": result}
    if isinstance(result, (tuple, list)) and len(result) >= 2:
        ok = bool(result[0])
        return ("ok" if ok else "warn"), str(result[1]), {"wired": True, "ok": ok}
    return "ok", str(result), {"wired": True}


def collect(
    db: Database,
    *,
    config: Config | None = None,
    subject: str = "self",
    record: bool = False,
) -> dict[str, Any]:
    cfg = config or Config()
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    # 1. 库自身
    integrity = db.integrity_ok()
    _add(checks, "integrity", "ok" if integrity else "fail", "PRAGMA integrity_check")

    chain_ok, chain_detail = events.verify_chain(db.conn)
    _add(checks, "event_chain", "ok" if chain_ok else "fail", chain_detail)

    anchor_status, anchor_detail, anchor_extra = _check_event_anchor(db.conn)
    _add(checks, "event_anchor", anchor_status, anchor_detail, **anchor_extra)
    metrics["event_anchor_ok"] = 1.0 if anchor_status == "ok" else 0.0

    _add_wal(checks, metrics, db, cfg, record=record)

    from . import invariants

    inv = invariants.summary(invariants.check_all(db))
    _add(
        checks,
        "invariants",
        "ok" if inv["ok"] else "fail",
        f"{inv['passed']}/{inv['total']} 通过",
        failed=inv.get("failed", [])[:10],
    )

    # 2. 任务
    job_rows = jobs.list_jobs(db)
    dead = [item for item in job_rows if item["state"] == "dead"]
    _add(checks, "jobs", "fail" if dead else "ok", f"{len(job_rows)} 个任务，dead {len(dead)} 个",
         dead=[item["id"] for item in dead])
    metrics["jobs_total"] = len(job_rows)
    metrics["jobs_dead"] = len(dead)

    # 3. 睡眠
    sleep = db.conn.execute(
        "SELECT * FROM sleep_run WHERE subject_id=? ORDER BY started_at DESC LIMIT 1",
        (subject,),
    ).fetchone()
    memory_total = int(db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"])
    metrics["memory_total"] = memory_total
    if sleep is None:
        _add(checks, "sleep", "warn", "还没有睡眠记录")
        metrics["last_sleep_hours"] = None
    else:
        hours = _hours_since(sleep["finished_at"] or sleep["started_at"])
        metrics["last_sleep_hours"] = round(hours or 0, 2)
        stale = float(cfg.get("runtime.sleep_stale_hours", 36) or 36)
        if sleep["status"] != "ok":
            _add(checks, "sleep", "fail" if (hours or 0) > stale else "warn",
                 f"最近一轮 {sleep['status']}：{sleep['error'] or ''}")
        elif memory_total and (hours or 0) > stale:
            _add(checks, "sleep", "fail", f"最近一次成功睡眠在 {hours:.1f} 小时前")
        else:
            _add(checks, "sleep", "ok", f"最近成功睡眠在 {hours:.1f} 小时前")

    # 4. 备份
    snap = db.conn.execute(
        "SELECT * FROM snapshot ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if snap is None:
        _add(checks, "backup", "warn", "还没有备份/快照")
        metrics["last_backup_hours"] = None
    else:
        path = Path(snap["path"])
        hours = _hours_since(snap["verified_at"] or snap["created_at"])
        metrics["last_backup_hours"] = round(hours or 0, 2)
        stale_days = float(cfg.get("runtime.backup_stale_days", 8) or 8)
        if not path.exists():
            _add(checks, "backup", "fail", f"备份文件不存在：{path}")
        elif not snap["verified_at"]:
            _add(checks, "backup", "warn", "备份未校验")
        elif (hours or 0) > stale_days * 24:
            _add(checks, "backup", "fail", f"最近备份在 {hours / 24:.1f} 天前")
        else:
            _add(checks, "backup", "ok", f"最近备份在 {hours / 24:.1f} 天前")

    # 5. 体积与增长
    size = _db_size(db)
    metrics["db_size_bytes"] = size
    max_mb = float(cfg.get("runtime.max_db_mb", 2048) or 2048)
    if size > max_mb * 1024 * 1024:
        _add(checks, "db_size", "fail", f"{size / 1024 / 1024:.1f} MB > {max_mb:.0f} MB")
    else:
        _add(checks, "db_size", "ok", f"{size / 1024 / 1024:.1f} MB")
    today = datetime.now().astimezone().date()
    cutoff = (today - timedelta(days=30)).isoformat()
    old = db.conn.execute(
        "SELECT value, day_key FROM metrics WHERE name='ops.db_size_bytes' "
        "AND day_key>=? AND day_key<? ORDER BY day_key ASC, id ASC LIMIT 1",
        (cutoff, today.isoformat()),
    ).fetchone()
    growth_mb = None
    if old is not None and old["value"]:
        growth_mb = round((size - float(old["value"])) / 1024 / 1024, 2)
    metrics["growth_mb_30d"] = growth_mb
    budget = float(cfg.get("runtime.growth_mb_30d", 256) or 256)
    if growth_mb is not None and growth_mb > budget:
        _add(checks, "db_growth", "warn", f"30 天增长 {growth_mb:.1f} MB > {budget:.0f} MB")
    elif growth_mb is not None:
        _add(checks, "db_growth", "ok", f"30 天增长 {growth_mb:.1f} MB")
    else:
        _add(checks, "db_growth", "info", "还没有历史体积基线")

    # 6. 召回趋势（有 eval 记录才判）
    latest, previous = _recall_pair(db)
    metrics["recall_at_5"] = float(latest["value"]) if latest else None
    if latest is None:
        _add(checks, "recall", "info", "还没有 recall@5 记录")
    elif previous is None:
        _add(checks, "recall", "ok", f"recall@5 {float(latest['value']):.3f}（单次基线）")
    else:
        drop = float(previous["value"]) - float(latest["value"])
        tolerance = float(cfg.get("runtime.recall_drop_tolerance", 0.02) or 0.02)
        status = "fail" if drop > tolerance else "ok"
        _add(checks, "recall", status,
             f"recall@5 {float(latest['value']):.3f}（上次 {float(previous['value']):.3f}，"
             f"{drop:+.3f}）", drop=round(drop, 4))

    # 7. 质量与冲突
    quality = _latest_metric(db, "reflect.quality")
    metrics["quality"] = float(quality["value"]) if quality else None
    qmin = float(cfg.get("runtime.quality_min", 50) or 50)
    if quality is None:
        _add(checks, "quality", "info", "还没有质量分")
    elif float(quality["value"]) < qmin:
        _add(checks, "quality", "warn", f"质量分 {float(quality['value']):.1f} < {qmin:.0f}")
    else:
        _add(checks, "quality", "ok", f"质量分 {float(quality['value']):.1f}")

    open_conflicts = db.conn.execute(
        "SELECT COUNT(*) c FROM conflict WHERE subject_id=? AND status='open'", (subject,)
    ).fetchone()["c"]
    metrics["open_conflicts"] = int(open_conflicts)
    _add(checks, "conflicts", "warn" if open_conflicts else "ok", f"{open_conflicts} 条未决冲突")

    status = "ok"
    for item in checks:
        if SEVERITY.get(item["status"], 0) > SEVERITY.get(status, 0):
            status = item["status"]
    result = {
        "ok": status != "fail",
        "status": status,
        "checks": checks,
        "metrics": metrics,
        "events": int(db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]),
        "generated_at": now_iso(),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }

    if record:
        _record(db, result, subject=subject)
    return result


def _record(db: Database, result: dict[str, Any], *, subject: str) -> None:
    day = datetime.now().astimezone().date().isoformat()
    ts = now_iso()
    mapping = {
        "ops.memory_total": result["metrics"].get("memory_total"),
        "ops.db_size_bytes": result["metrics"].get("db_size_bytes"),
        "ops.wal_bytes": result["metrics"].get("wal_bytes"),
        "ops.jobs_dead": result["metrics"].get("jobs_dead"),
        "ops.quality": result["metrics"].get("quality"),
        "ops.recall_at_5": result["metrics"].get("recall_at_5"),
        "ops.open_conflicts": result["metrics"].get("open_conflicts"),
        "ops.health": 1.0 if result["status"] != "fail" else 0.0,
    }
    with db.tx() as conn:
        marks = ",".join("?" for _ in mapping)
        conn.execute(
            f"DELETE FROM metrics WHERE day_key=? AND name IN ({marks})", (day, *mapping)
        )
        for name, value in mapping.items():
            if value is None:
                continue
            conn.execute(
                "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
                (ts, day, name, float(value), "{}"),
            )
        exists = conn.execute(
            "SELECT 1 FROM event_log WHERE op='ops.monitor' AND target_id=? LIMIT 1",
            (day,),
        ).fetchone()
        if exists is None:
            events.append(
                conn,
                actor="system:monitor",
                op="ops.monitor",
                target_tbl="day",
                target_id=day,
                subject_id=subject,
                payload={"status": result["status"], "metrics": result["metrics"]},
            )


def render(data: dict[str, Any]) -> str:
    mark = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}.get(data["status"], "·")
    lines = [f"{mark} PMem 监控：{data['status']}  （{data['elapsed_ms']} ms）"]
    for item in data["checks"]:
        mark = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}.get(item["status"], "·")
        lines.append(f"  {mark} {item['name']:<14} {item['detail']}")
    metrics = data.get("metrics") or {}
    if metrics.get("quality") is not None:
        lines.append(f"  质量 {metrics['quality']:.1f} · recall@5 {metrics.get('recall_at_5') or '—'}")
    return "\n".join(lines)


__all__ = ["collect", "render"]
