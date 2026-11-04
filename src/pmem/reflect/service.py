"""反思服务（M7）：日审计 / 周报 / 历史，统一给 CLI、MCP、SDK 用。

只读路径（``persist=False``）不写任何表；写入路径只落 ``reflection`` 与 ``metrics``。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Mapping

from ..base.db import Database
from ..base.errors import ValidationError
from . import report
from .audit import LABELS


def _windows(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = config or {}

    def get(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    return {
        "freshness_days": get("reflect.freshness_days", 90),
        "recall_days": get("reflect.recall_days", 30),
        "due_days": get("reflect.due_days", 30),
    }


def _filter_only(data: dict[str, Any], only: str | None, limit: int) -> dict[str, Any]:
    if not only:
        return data
    defects = [
        item for item in (data.get("defects") or data.get("findings") or [])
        if item.get("code") == only
    ]
    for item in defects:
        item["samples"] = (item.get("samples") or [])[:limit]
    data["defects"] = defects
    data["suggestions"] = [
        item for item in (data.get("suggestions") or []) if item.get("target") == only
    ]
    return data


def reflect(
    db: Database,
    *,
    subject: str = "self",
    period: str | None = None,
    persist: bool = False,
    only: str | None = None,
    limit: int = 20,
    scope_key: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成日审计或周报；``persist=True`` 时写入 ``reflection``。"""
    windows = _windows(config)
    period = period or f"day:{date.today().isoformat()}"
    try:
        if period.startswith("week:"):
            key = period.split(":", 1)[1]
            if persist:
                data = report.write(
                    db, subject=subject, key=key, scope_key=scope_key, vector_enabled=True, **windows
                )
            else:
                data = report.compose(
                    db, subject=subject, key=key, scope_key=scope_key, vector_enabled=True, **windows
                )
            data["persisted"] = bool(persist)
            data["kind"] = "week"
        else:
            day = period.split(":", 1)[1] if ":" in period else period
            from ..sleep import reflection as daily

            if persist:
                data = daily.write(
                    db,
                    subject=subject,
                    day_key=day,
                    run_id="reflect:manual",
                    due_days=windows["due_days"],
                    freshness_days=windows["freshness_days"],
                    recall_days=windows["recall_days"],
                )
            else:
                data = daily.compose(
                    db,
                    subject=subject,
                    day_key=day,
                    due_days=windows["due_days"],
                    freshness_days=windows["freshness_days"],
                    recall_days=windows["recall_days"],
                )
            data["period"] = f"day:{day}"
            data["persisted"] = bool(persist)
            data["kind"] = "day"
    except ValueError as exc:
        raise ValidationError(f"period 不合法：{period}", field="period") from exc

    data = _filter_only(data, only, limit)
    data["rendered"] = render(data)
    return data


def render(data: dict[str, Any]) -> str:
    if data.get("kind") == "week":
        return report.render(data)

    lines: list[str] = []
    quality = data.get("quality")
    lines.append(f"— 反思 {data['period']}  ·  质量分 {quality if quality is not None else '—'} / 100")
    components = data.get("components") or {}
    for name, comp in components.items():
        value = comp.get("value")
        shown = "—" if value is None else f"{value:.2f}"
        lines.append(
            f"  {name:<14} {shown}  (w {comp.get('weight', 0):.2f})  "
            f"{comp.get('good')}/{comp.get('total')}  {comp.get('detail') or LABELS.get(name, '')}"
        )
    defects = data.get("defects") or []
    if defects:
        lines.append("缺陷：")
        for defect in defects:
            lines.append(
                f"  [{defect['severity']}] {defect['code']:<22} {defect['count']:>4} 条 — {defect['why']}"
            )
            for sample in (defect.get("samples") or [])[:3]:
                if sample.get("text"):
                    lines.append(f"      · {sample['text']}")
    suggestions = data.get("suggestions") or []
    if suggestions:
        lines.append("建议：")
        for item in suggestions:
            effort = item.get("effort") or "low"
            why = item.get("why") or item.get("reason") or ""
            command = item.get("command") or ""
            fix = item.get("fix") or command
            lines.append(f"  · {item['action']}（{effort}）：{why}")
            if command:
                lines.append(f"      看：{command}   修：{fix}")
    if data.get("kind") == "day":
        lines.append("七问：")
        for key, text in (data.get("questions") or {}).items():
            lines.append(f"  {key}: {text}")
    return "\n".join(lines)


def history(db: Database, *, subject: str = "self", limit: int = 20) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT period, created_at, metrics_json FROM reflection WHERE subject_id=? "
        "ORDER BY created_at DESC LIMIT ?",
        (subject, limit),
    ).fetchall()
    out = []
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"])
        except (json.JSONDecodeError, TypeError):
            metrics = {}
        out.append(
            {
                "period": row["period"],
                "created_at": row["created_at"],
                "quality": metrics.get("quality"),
            }
        )
    return out
