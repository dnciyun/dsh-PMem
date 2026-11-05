"""时间语义（02 §8）。

四条时间各司其职：

- ``created_at``  系统写入时刻（UTC，不可变）
- ``recorded_at`` 系统"认为它成立"的时刻（补录时可与 created_at 不同）
- ``valid_from/valid_to`` 现实世界里成立的时间段
- ``day_key``     本地日期（跨天判定用，避免凌晨写入被算到前一天）

存 UTC、显示本地。所有对外字符串都是 ISO-8601 带 ``Z``，精确到毫秒。
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"

# 测试/回放用的时间闸门（见 `docs/milestone-M9.9.md` §6）。
#
# 为什么必须是「模块里的一个变量」而不是 monkeypatch `clock.now`：
# 全项目有 30 多个模块 `from ..base.clock import now_iso`，绑定发生在 import 时。
# 换掉 `clock.now` 这个**名字**没用 —— 那些模块手里还是原来那个函数对象。
# 但函数体里的 `_NOW` 是运行时查的，所以只要把判断写进 `now()` 内部，
# 所有已经绑定的引用都会一起走模拟时间。
ENV_NOW = "PMEM_NOW"

_NOW: datetime | None = None
_ENV_CACHE: tuple[str, datetime] | None = None


def set_now(value: datetime | str | None) -> None:
    """把"现在"固定住；传 ``None`` 恢复真实时间。"""
    global _NOW, _ENV_CACHE
    if value is None:
        _NOW = None
        _ENV_CACHE = None
        return
    if isinstance(value, str):
        _NOW = parse_iso(value)
    else:
        _NOW = (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)


def reset_now() -> None:
    set_now(None)


def now() -> datetime:
    """当前 UTC 时间（带时区）。被 ``set_now`` 或 ``PMEM_NOW`` 接管时返回固定时刻。"""
    global _ENV_CACHE
    if _NOW is not None:
        return _NOW
    raw = os.environ.get(ENV_NOW, "").strip()
    if not raw:
        return datetime.now(UTC)
    if _ENV_CACHE is None or _ENV_CACHE[0] != raw:
        _ENV_CACHE = (raw, parse_iso(raw))
    return _ENV_CACHE[1]


def to_iso(dt: datetime) -> str:
    """UTC → ``2026-11-02T18:03:11.482Z``（毫秒）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime(ISO_FMT)[:-3] + "Z"


def now_iso() -> str:
    return to_iso(now())


def parse_iso(value: str) -> datetime:
    """解析 ``...Z`` 或其他 ISO-8601；无时区按 UTC。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def day_key(dt: datetime | None = None) -> str:
    """本地日期 ``YYYY-MM-DD``（系统时区）。"""
    if dt is None:
        dt = now().astimezone()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().date().isoformat()


def occurred_day(value: str | None, *, fallback: str | None = None) -> str | None:
    """ISO 时间 → 这件事**发生在哪一天**（本地日期）。

    为什么单独一个函数：``day_key`` 以前在写库那一刻取 ``now()``，于是「记忆属于
    哪一天」实际记的是**整理日**而不是**发生日**——日记在次日凌晨跑，派生出来的
    事实就整体晚一天；agent 面板和召回 payload 都拿这个字段当日期显示，用户看到的
    就是错日期（实测 B 层 6 个库 2118/2227 条偏 +1 天，本机真实库 166 条全偏 1–2 天）。

    派生事实的日期必须回到原料那一行的 ``ts``，所以这里只做一件事：把任意 ISO
    字符串折成本地日期。解析失败返回 ``fallback``（不抛：一条脏时间不该让整篇日记写不进去）。
    """
    if not value:
        return fallback
    try:
        return day_key(parse_iso(str(value)))
    except (TypeError, ValueError):
        return fallback


def days_between(earlier_iso: str, later_iso: str | None = None) -> float:
    """两时刻相差多少天（用于 recency / 衰减）。"""
    start = parse_iso(earlier_iso)
    end = parse_iso(later_iso) if later_iso else now()
    return max(0.0, (end - start).total_seconds() / 86400.0)


def parse_day(value: str) -> date:
    return date.fromisoformat(value)
