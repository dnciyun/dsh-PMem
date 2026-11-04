"""睡眠内部的小工具：日期边界、稳定 JSON、派生行证据格式。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from ..base.clock import to_iso


def day_start(day_key: str) -> datetime:
    """本地日期 → 当天 00:00（带本地时区）。"""
    d = datetime.fromisoformat(day_key)
    return datetime(d.year, d.month, d.day).astimezone()


def day_end(day_key: str) -> datetime:
    return day_start(day_key) + timedelta(days=1)


def day_bounds_iso(day_key: str) -> tuple[str, str]:
    """本地日期 → [start, end) 的 UTC ISO 字符串。"""
    return (
        to_iso(day_start(day_key).astimezone(UTC)),
        to_iso(day_end(day_key).astimezone(UTC)),
    )


def yesterday_key() -> str:
    return (datetime.now().astimezone() - timedelta(days=1)).date().isoformat()


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence(memory_ids: Iterable[str], *, limit: int = 20) -> str:
    """证据指针：按 id 排序、去重，稳定 JSON（重跑比较不抖动）。"""
    seen: list[str] = []
    for mid in memory_ids:
        if mid and mid not in seen:
            seen.append(mid)
        if len(seen) >= limit:
            break
    return canonical([{"memory_id": mid} for mid in sorted(seen)])
