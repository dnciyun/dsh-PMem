"""周报（M7）：把质量审计按月/周落成可读的一页。

只写 ``reflection`` 与 ``metrics``，不碰事实层。同一 ``period`` 内容不变复用旧行，
内容变了追加新行（与 ``sleep/reflection.py`` 的日反思同一套幂等规则）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from ..base import events
from ..base.clock import now_iso, to_iso
from ..base.db import Database
from .audit import audit

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def _canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def week_key(day: date | None = None) -> str:
    iso = (day or date.today()).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_bounds(key: str) -> tuple[date, date]:
    match = _WEEK_RE.fullmatch(key or "")
    if not match:
        raise ValueError(f"周格式应为 YYYY-Www：{key!r}")
    year, week = int(match.group(1)), int(match.group(2))
    start = date.fromisocalendar(year, week, 1)
    return start, start + timedelta(days=6)


def _iso_at(day: date, hour: int = 0) -> str:
    return to_iso(datetime.combine(day, time(hour=hour)).astimezone())


def _scalar(db: Database, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = db.conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _activity(
    db: Database, *, subject: str, start_iso: str, end_iso: str
) -> dict[str, int]:
    return {
        "memories_created": _scalar(
            db,
            "SELECT COUNT(*) FROM memory WHERE subject_id=? AND created_at>=? AND created_at<? "
            "AND status<>'deleted'",
            (subject, start_iso, end_iso),
        ),
        "conflicts_opened": _scalar(
            db,
            "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND created_at>=? AND created_at<?",
            (subject, start_iso, end_iso),
        ),
        "conflicts_resolved": _scalar(
            db,
            "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='resolved' "
            "AND resolved_at>=? AND resolved_at<?",
            (subject, start_iso, end_iso),
        ),
        "traits_promoted": _scalar(
            db,
            "SELECT COUNT(*) FROM event_log WHERE subject_id=? AND op='trait.promote' "
            "AND ts>=? AND ts<?",
            (subject, start_iso, end_iso),
        ),
        "traits_demoted": _scalar(
            db,
            "SELECT COUNT(*) FROM event_log WHERE subject_id=? AND op='trait.demote' "
            "AND ts>=? AND ts<?",
            (subject, start_iso, end_iso),
        ),
        "experiences_created": _scalar(
            db,
            "SELECT COUNT(*) FROM experience WHERE subject_id=? AND created_at>=? AND created_at<?",
            (subject, start_iso, end_iso),
        ),
        "recalls": _scalar(
            db,
            "SELECT COUNT(*) FROM recall_log WHERE subject_id=? AND ts>=? AND ts<?",
            (subject, start_iso, end_iso),
        ),
        "feedback": _scalar(
            db,
            "SELECT COUNT(*) FROM feedback WHERE subject_id=? AND ts>=? AND ts<?",
            (subject, start_iso, end_iso),
        ),
        "sleep_runs": _scalar(
            db,
            "SELECT COUNT(*) FROM sleep_run WHERE subject_id=? AND started_at>=? AND started_at<?",
            (subject, start_iso, end_iso),
        ),
    }


def _quality_delta(db: Database, *, before_day: str, quality: float | None) -> float | None:
    if quality is None:
        return None
    row = db.conn.execute(
        "SELECT value FROM metrics WHERE name='reflect.quality' AND day_key < ? "
        "ORDER BY day_key DESC LIMIT 1",
        (before_day,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return round(quality - float(row[0]), 1)


def _summary(
    *,
    activity: dict[str, int],
    quality: float | None,
    delta: float | None,
    defects: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    open_conflicts: int,
    hypothesis: int,
) -> dict[str, str]:
    top = defects[0] if defects else None
    fix = suggestions[0] if suggestions else None
    return {
        "what_happened": (
            f"本周新增 {activity['memories_created']} 条记忆、{activity['sleep_runs']} 次睡眠、"
            f"{activity['recalls']} 次召回。"
        ),
        "what_changed": (
            f"质量分 {quality if quality is not None else '—'}"
            + (f"（较上次 {delta:+.1f}）" if delta is not None else "（无历史基线）")
            + f"；特质升级 {activity['traits_promoted']}、降级 {activity['traits_demoted']}。"
        ),
        "what_unresolved": f"{open_conflicts} 条冲突未裁决。",
        "what_learned": f"新增 {activity['experiences_created']} 张经验卡。",
        "what_uncertain": f"{hypothesis} 条假设特质尚未 established。",
        "what_to_watch": (
            f"最大缺陷 {top['code']}（{top['count']} 条，{top['severity']}）" if top else "没有检出缺陷。"
        ),
        "what_to_ask": (
            f"先处理 {fix['target']}？{fix['why']}（{fix['fix']}）" if fix else ""
        ),
    }


def compose(
    db: Database,
    *,
    subject: str = "self",
    key: str | None = None,
    scope_key: str | None = None,
    freshness_days: int = 90,
    recall_days: int = 30,
    due_days: int = 30,
    vector_enabled: bool = False,
) -> dict[str, Any]:
    """生成某一周的周报（不写库）。"""
    key = key or week_key()
    start, end = week_bounds(key)
    start_iso, end_iso = _iso_at(start), _iso_at(start + timedelta(days=7))
    end_day = end.isoformat()

    audit_data = audit(
        db,
        subject=subject,
        scope_key=scope_key,
        freshness_days=freshness_days,
        recall_days=recall_days,
        due_days=due_days,
        vector_enabled=vector_enabled,
    )
    activity = _activity(db, subject=subject, start_iso=start_iso, end_iso=end_iso)
    quality = audit_data["quality"]
    delta = _quality_delta(db, before_day=start.isoformat(), quality=quality)

    parts = {
        name: comp["value"] for name, comp in audit_data["components"].items()
    }
    metrics: dict[str, Any] = {
        **audit_data["metrics"],
        **activity,
        "quality": quality,
        "quality_delta": delta,
        "actionable_rate": audit_data["actionable_rate"],
    }
    for name, value in parts.items():
        if value is not None:
            metrics[f"part.{name}"] = value

    open_conflicts = _scalar(
        db, "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='open'", (subject,)
    )
    hypothesis = _scalar(
        db,
        "SELECT COUNT(*) FROM trait WHERE subject_id=? AND status='hypothesis'",
        (subject,),
    )
    questions = _summary(
        activity=activity,
        quality=quality,
        delta=delta,
        defects=audit_data["defects"],
        suggestions=audit_data["suggestions"],
        open_conflicts=open_conflicts,
        hypothesis=hypothesis,
    )

    return {
        "period": f"week:{key}",
        "week": key,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "questions": questions,
        "findings": audit_data["defects"],
        "hypotheses": [],
        "metrics": metrics,
        "suggestions": audit_data["suggestions"],
        "quality": quality,
        "components": audit_data["components"],
        "actionable_rate": audit_data["actionable_rate"],
    }


def _same(existing: Any, payload: dict[str, Any]) -> bool:
    try:
        return (
            json.loads(existing["questions_json"]) == payload["questions"]
            and json.loads(existing["findings_json"]) == payload["findings"]
            and json.loads(existing["hypotheses_json"]) == payload["hypotheses"]
            and json.loads(existing["metrics_json"]) == payload["metrics"]
            and json.loads(existing["suggestions_json"]) == payload["suggestions"]
        )
    except (json.JSONDecodeError, TypeError):
        return False


def write(db: Database, *, subject: str = "self", key: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """幂等写入周报；内容变化追加新行，不覆盖历史。"""
    payload = compose(db, subject=subject, key=key, **kwargs)
    period = payload["period"]
    existing = db.conn.execute(
        "SELECT * FROM reflection WHERE subject_id=? AND period=? ORDER BY created_at DESC LIMIT 1",
        (subject, period),
    ).fetchone()
    if existing is not None and _same(existing, payload):
        return {
            **payload,
            "id": existing["id"],
            "period": period,
            "created": False,
        }

    from ..base.ids import ulid

    reflection_id = ulid()
    end_day = payload["range"]["end"]
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO reflection(id, subject_id, period, questions_json, findings_json, "
            "hypotheses_json, metrics_json, suggestions_json, created_at, run_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                reflection_id,
                subject,
                period,
                _canonical(payload["questions"]),
                _canonical(payload["findings"]),
                _canonical(payload["hypotheses"]),
                _canonical(payload["metrics"]),
                _canonical(payload["suggestions"]),
                now_iso(),
                f"reflect:{payload['week']}",
            ),
        )
        conn.execute(
            "DELETE FROM metrics WHERE day_key=? AND name LIKE 'reflect.%'", (end_day,)
        )
        ts = now_iso()
        for name, value in payload["metrics"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                conn.execute(
                    "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
                    (ts, end_day, f"reflect.{name}", float(value), "{}"),
                )
        # 周报是派生视图（只写 reflection/metrics），但它是一次真实运行的产物：
        # 补一条事件，monthly/星图/审计才能回答「这周的反思跑过没有、结论几个」。
        events.append(
            conn,
            actor="system:reflect",
            op="reflect.report",
            target_tbl="reflection",
            target_id=reflection_id,
            subject_id=subject,
            payload={
                "period": period,
                "quality": payload.get("quality"),
                "defects": len(payload.get("findings") or []),
                "suggestions": len(payload.get("suggestions") or []),
            },
        )
    return {
        **payload,
        "id": reflection_id,
        "period": period,
        "created": True,
    }


def render(payload: dict[str, Any]) -> str:
    """周报的人读文本。"""
    lines: list[str] = []
    q = payload.get("quality")
    delta = payload.get("metrics", {}).get("quality_delta")
    lines.append(f"— 周报 {payload['period']}（{payload['range']['start']} → {payload['range']['end']}）")
    lines.append(
        f"  质量分 {q if q is not None else '—'}"
        + (f"（较上次 {delta:+.1f}）" if delta is not None else "")
        + f"  ·  可执行建议 {len(payload.get('suggestions') or [])} 条"
    )
    for key, text in (payload.get("questions") or {}).items():
        lines.append(f"  · {text}")
    defects = payload.get("findings") or []
    if defects:
        lines.append("缺陷：")
        for defect in defects[:6]:
            lines.append(
                f"  [{defect['severity']}] {defect['code']} {defect['count']} 条 — {defect['why']}"
            )
    return "\n".join(lines)
