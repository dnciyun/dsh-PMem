"""M9 月报：把一个月的事实、整理、质量、存储写成可回看的一页。

月报是派生视图，不写事实层；只追加 ``event_log`` 一条 ``ops.report.monthly``。
同一个月重复生成会覆盖同名文件（同名文件是最新派生结果），但事件会留下每次生成。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ..base import events
from ..base.clock import now_iso, to_iso
from ..base.config import Config
from ..base.db import Database


def _month_bounds(month: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", month)
    if not match:
        raise ValueError(f"月份格式应为 YYYY-MM：{month!r}")
    year, mon = int(match.group(1)), int(match.group(2))
    if not 1 <= mon <= 12:
        raise ValueError(f"月份超出范围：{month!r}")
    start = date(year, mon, 1)
    end = date(year + (1 if mon == 12 else 0), 1 if mon == 12 else mon + 1, 1)
    return start, end


def _iso(day: date) -> str:
    return to_iso(datetime.combine(day, time.min).astimezone())


def _previous_month(today: date | None = None) -> str:
    value = today or date.today()
    year = value.year - (1 if value.month == 1 else 0)
    month = 12 if value.month == 1 else value.month - 1
    return f"{year:04d}-{month:02d}"


def _scalar(db: Database, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = db.conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _rows(db: Database, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.conn.execute(sql, params).fetchall()]


def _report_dir(db: Database, cfg: Config) -> Path:
    if str(cfg.get("runtime.report_dir", "") or ""):
        return Path(str(cfg.get("runtime.report_dir")))
    base = Path.cwd() if db.path == ":memory:" else Path(db.path).resolve().parent
    return base / "reports"


def _db_size(db: Database) -> int:
    if db.path == ":memory:":
        return 0
    path = Path(db.path)
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def collect(
    db: Database,
    *,
    month: str | None = None,
    subject: str = "self",
) -> dict[str, Any]:
    key = month or _previous_month()
    start, end = _month_bounds(key)
    start_iso, end_iso = _iso(start), _iso(end)
    today = date.today()
    partial = start <= today < end

    created = _scalar(
        db,
        "SELECT COUNT(*) FROM memory WHERE subject_id=? AND created_at>=? AND created_at<? "
        "AND status<>'deleted'",
        (subject, start_iso, end_iso),
    )
    deleted = _scalar(
        db,
        "SELECT COUNT(*) FROM memory WHERE subject_id=? AND deleted_at>=? AND deleted_at<?",
        (subject, start_iso, end_iso),
    )
    active = _scalar(
        db,
        "SELECT COUNT(*) FROM memory WHERE subject_id=? AND status IN ('active','contested')",
        (subject,),
    )
    dormant = _scalar(
        db,
        "SELECT COUNT(*) FROM memory WHERE subject_id=? AND status='dormant'", (subject,)
    )
    total = _scalar(db, "SELECT COUNT(*) FROM memory WHERE subject_id=?", (subject,))

    by_kind = {
        row["kind"]: row["c"]
        for row in db.conn.execute(
            "SELECT kind, COUNT(*) c FROM memory WHERE subject_id=? GROUP BY kind", (subject,)
        )
    }
    by_grade = {
        row["grade"]: row["c"]
        for row in db.conn.execute(
            "SELECT t.grade, COUNT(*) c FROM memory m JOIN target_grade t "
            "ON t.target_tbl='memory' AND t.target_id=m.id WHERE m.subject_id=? GROUP BY t.grade",
            (subject,),
        )
    }
    top_groups = _rows(
        db,
        "SELECT g.path, COUNT(*) c FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
        "WHERE gm.target_tbl='memory' AND g.status='active' GROUP BY g.path ORDER BY c DESC LIMIT 8",
    )

    sleep_runs = _scalar(
        db,
        "SELECT COUNT(*) FROM sleep_run WHERE subject_id=? AND started_at>=? AND started_at<?",
        (subject, start_iso, end_iso),
    )
    sleep_ok = _scalar(
        db,
        "SELECT COUNT(*) FROM sleep_run WHERE subject_id=? AND started_at>=? AND started_at<? "
        "AND status='ok'",
        (subject, start_iso, end_iso),
    )
    sleep_last = db.conn.execute(
        "SELECT * FROM sleep_run WHERE subject_id=? ORDER BY started_at DESC LIMIT 1", (subject,)
    ).fetchone()

    quality_rows = _rows(
        db,
        "SELECT day_key, value FROM metrics WHERE name='reflect.quality' AND day_key>=? AND day_key<? "
        "ORDER BY day_key, id",
        (start.isoformat(), end.isoformat()),
    )
    quality_values = [float(row["value"]) for row in quality_rows]
    quality_first = quality_values[0] if quality_values else None
    quality_last = quality_values[-1] if quality_values else None
    quality_avg = round(sum(quality_values) / len(quality_values), 1) if quality_values else None

    eval_latest = db.conn.execute(
        "SELECT value, day_key FROM metrics WHERE name='eval.recall_at_5' "
        "AND day_key>=? AND day_key<? ORDER BY day_key DESC, id DESC LIMIT 1",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    recalls = _scalar(
        db,
        "SELECT COUNT(*) FROM recall_log WHERE subject_id=? AND ts>=? AND ts<?",
        (subject, start_iso, end_iso),
    )
    feedback = _scalar(
        db,
        "SELECT COUNT(*) FROM feedback WHERE subject_id=? AND ts>=? AND ts<?",
        (subject, start_iso, end_iso),
    )
    conflicts_open = _scalar(
        db, "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='open'", (subject,)
    )
    traits_active = _scalar(
        db, "SELECT COUNT(*) FROM trait WHERE subject_id=? AND status='established'", (subject,)
    )
    experiences = _scalar(
        db, "SELECT COUNT(*) FROM experience WHERE subject_id=? AND status='active'", (subject,)
    )
    backups = _rows(
        db,
        "SELECT kind, COUNT(*) c, MAX(created_at) last FROM snapshot "
        "WHERE created_at>=? AND created_at<? GROUP BY kind",
        (start_iso, end_iso),
    )
    backup_total = _scalar(
        db,
        "SELECT COUNT(*) FROM snapshot WHERE created_at>=? AND created_at<?",
        (start_iso, end_iso),
    )
    event_count = _scalar(
        db,
        "SELECT COUNT(*) FROM event_log WHERE ts>=? AND ts<?",
        (start_iso, end_iso),
    )
    size_row = db.conn.execute(
        "SELECT value FROM metrics WHERE name='ops.db_size_bytes' AND day_key<? "
        "ORDER BY day_key DESC, id DESC LIMIT 1",
        (end.isoformat(),),
    ).fetchone()
    size_before = int(size_row["value"]) if size_row else None
    size_now = _db_size(db)
    growth = None
    if size_before is not None:
        growth = round((size_now - size_before) / 1024 / 1024, 2)

    return {
        "month": key,
        "range": {"start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat()},
        "partial": partial,
        "days": max(1, (min(today, end - timedelta(days=1)) - start).days + 1)
        if partial
        else (end - start).days,
        "created": created,
        "deleted": deleted,
        "total": total,
        "active": active,
        "dormant": dormant,
        "by_kind": by_kind,
        "by_grade": by_grade,
        "top_groups": top_groups,
        "sleep_runs": sleep_runs,
        "sleep_ok": sleep_ok,
        "sleep_last": dict(sleep_last) if sleep_last else None,
        "quality": {
            "first": quality_first,
            "last": quality_last,
            "avg": quality_avg,
            "days": len(quality_values),
        },
        "quality_delta": round(quality_last - quality_first, 1)
        if quality_first is not None and quality_last is not None
        else None,
        "recall_at_5": float(eval_latest["value"]) if eval_latest else None,
        "recalls": recalls,
        "feedback": feedback,
        "conflicts_open": conflicts_open,
        "traits_active": traits_active,
        "experiences": experiences,
        "backup_total": backup_total,
        "backups": backups,
        "events": event_count,
        "db_size_bytes": size_now,
        "db_size_before": size_before,
        "db_growth_mb": growth,
    }


def render(data: dict[str, Any]) -> str:
    scope = "部分月" if data.get("partial") else "完整月"
    lines = [
        f"# PMem 月报 {data['month']}",
        "",
        f"> {data['range']['start']} → {data['range']['end']}（{scope}，{data.get('days', 0)} 天）",
        "> 记忆不是数字，下面这些只是给「有没有在好好相处」留一个可回看的坐标。",
        "",
        "## 一、这个月留下的",
        "",
        f"- 新增记忆 **{data['created']}** 条，删除 {data['deleted']} 条",
        f"- 现在共 **{data['total']}** 条：活跃 {data['active']} / 休眠 {data['dormant']}",
        f"- 类型：{_fmt_map(data.get('by_kind'))}",
        f"- 分级：{_fmt_map(data.get('by_grade'))}",
    ]
    groups = data.get("top_groups") or []
    if groups:
        lines.append("- 最大的几个分组：" + "、".join(f"`{g['path']}` {g['c']}" for g in groups[:6]))
    lines += [
        "",
        "## 二、整理与睡眠",
        "",
        f"- 睡眠 **{data['sleep_runs']}** 轮，成功 {data['sleep_ok']} 轮",
    ]
    last = data.get("sleep_last")
    if last:
        lines.append(f"- 最近一轮：{last.get('day_key')} · {last.get('status')} · {last.get('started_at')}")
    lines += [
        "",
        "## 三、质量与召回",
        "",
        f"- 质量分：月初 {_num(data['quality'].get('first'))} → 月末 {_num(data['quality'].get('last'))}"
        f"（均值 {_num(data['quality'].get('avg'))}，覆盖 {data['quality'].get('days')} 天）",
        f"- 质量变化：{_signed(data.get('quality_delta'))}",
        f"- recall@5：{_num3(data.get('recall_at_5'))}",
        f"- 召回 {data['recalls']} 次，反馈 {data['feedback']} 条，未决冲突 {data['conflicts_open']} 条",
        f"- 稳定特质 {data['traits_active']} 条，可用经验卡 {data['experiences']} 条",
        "",
        "## 四、存储与备份",
        "",
        f"- 事件 {data['events']} 条；库当前 {_mb(data['db_size_bytes'])}",
        f"- 备份/快照 {data['backup_total']} 份"
        + (f"；体积变化 {_signed(data.get('db_growth_mb'))} MB" if data.get("db_growth_mb") is not None else ""),
        "",
        "## 五、下个月先看什么",
        "",
    ]
    hints: list[str] = []
    if data["sleep_runs"] == 0 or data["sleep_ok"] < data["sleep_runs"]:
        hints.append("- 睡眠没有全部成功：先跑 `pmem monitor`，再看最近一份睡眠报告。")
    if data.get("quality_delta") is not None and data["quality_delta"] < 0:
        hints.append("- 质量分在下降：看 `pmem reflect --period day:...` 的缺陷码，不要先加功能。")
    if data.get("recall_at_5") is None:
        hints.append("- 还没有 recall@5 记录：跑一次 `pmem eval --record`，否则「没变差」无法证明。")
    if data["backup_total"] == 0:
        hints.append("- 本月没有备份：先跑 `pmem backup run`，再 `pmem backup drill`。")
    if not hints:
        hints.append("- 没有明显红灯；继续收集真实数据，别用记忆条数当成绩。")
    lines.extend(hints)
    lines += ["", "> 月报是派生视图，不是新事实；数字只用于发现退化，不用于衡量关系。"]
    return "\n".join(lines) + "\n"


def _fmt_map(value: dict[str, Any] | None) -> str:
    if not value:
        return "—"
    return " · ".join(f"{k} {v}" for k, v in sorted(value.items()))


def _num(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def _num3(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _signed(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.1f}"


def _mb(value: Any) -> str:
    return "—" if value is None else f"{float(value) / 1024 / 1024:.1f} MB"


def generate(
    db: Database,
    *,
    config: Config | None = None,
    subject: str = "self",
    month: str | None = None,
    out: str | Path | None = None,
) -> dict[str, Any]:
    cfg = config or Config()
    key = month or _previous_month()
    data = collect(db, month=key, subject=subject)
    rendered = render(data)
    directory = Path(out) if out else _report_dir(db, cfg)
    if directory.suffix.lower() == ".md":
        md_path = directory
        directory = md_path.parent
    else:
        md_path = directory / f"monthly-{key}.md"
    directory.mkdir(parents=True, exist_ok=True)
    md_path.write_text(rendered, encoding="utf-8")
    json_path = md_path.with_suffix(".json")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with db.tx() as conn:
        events.append(
            conn,
            actor="system:report",
            op="ops.report.monthly",
            target_tbl="month",
            target_id=key,
            subject_id=subject,
            payload={"path": str(md_path), "created": data["created"], "quality": data["quality"]},
        )
    return {
        "ok": True,
        "month": key,
        "path": str(md_path),
        "json_path": str(json_path),
        "rendered": rendered,
        "metrics": data,
        "generated_at": now_iso(),
    }


__all__ = ["collect", "render", "generate", "_previous_month"]
