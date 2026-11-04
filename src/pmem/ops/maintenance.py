"""维护窗口闸（M9.9）：窗口内 daemon 只自愈、不执行任何任务。

为什么需要它：备份 / 迁移 / 手工修库时，后台调度器正好到期跑 sleep 或 backup，
会在你眼皮底下动库。窗口里「跳过」不是丢任务——``job.next_run_at`` 不动，
窗口结束后下一次 tick 自然补跑（补偿式调度本来就允许迟到，见 scheduler 模块头）。

三个动作：

- ``mark(db, config=..., reason=..., minutes=...)`` 写标记文件；
- ``active(db, config=...)`` 读标记（``until`` 已过 = 不活跃，文件留着无害）；
- ``clear(db, config=...)`` 删标记。

标记是**文件**而不是库里的表：维护窗口常常正发生在库不可写的时候（VACUUM /
迁移 / 锁排查），这时候往库里写一行反而会失败。文件放在备份目录的同级
（复用 ``backup._dir`` 的路径逻辑），默认文件名 ``maintenance.json``，
内容含 ``reason`` / ``started_at`` / ``until``（另附 ``minutes`` 便于排查）。

手动命令（``pmem backup`` / ``pmem sleep``）不走 ``scheduler.tick``，因此不受闸门影响。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..base.clock import now, parse_iso, to_iso
from ..base.config import Config
from ..base.db import Database
from . import backup

FILENAME = "maintenance.json"
DEFAULT_MINUTES = 30


def _file(db: Database, cfg: Config) -> Path:
    """标记文件路径：``runtime.maintenance_file`` 优先，否则备份目录的同级。"""
    explicit = str(cfg.get("runtime.maintenance_file", "") or "")
    if explicit:
        return Path(explicit)
    return backup._dir(db, cfg).parent / FILENAME


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _inactive(path: Path, *, expired: bool = False, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or {}
    return {
        "active": False,
        "expired": expired,
        "reason": str(data.get("reason") or ""),
        "started_at": str(data.get("started_at") or ""),
        "until": str(data.get("until") or ""),
        "remaining_seconds": 0,
        "path": str(path),
    }


def active(
    db: Database,
    *,
    config: Config | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """维护窗口是否活跃。``until`` 过期自动视为不活跃（不删文件，交给 clear/下一次 mark）。"""
    cfg = config or Config()
    path = _file(db, cfg)
    if not path.exists():
        return _inactive(path)
    data = _read(path)
    if not data:
        return _inactive(path)
    until_raw = str(data.get("until") or "")
    if not until_raw:
        return _inactive(path, data=data)
    try:
        until = parse_iso(until_raw)
    except ValueError:
        return _inactive(path, data=data)
    current = at or now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=until.tzinfo)
    remaining = int((until - current.astimezone(until.tzinfo)).total_seconds())
    if remaining <= 0:
        return _inactive(path, expired=True, data=data)
    return {
        "active": True,
        "expired": False,
        "reason": str(data.get("reason") or "maintenance"),
        "started_at": str(data.get("started_at") or ""),
        "until": to_iso(until),
        "remaining_seconds": remaining,
        "minutes": int(data.get("minutes") or 0),
        "path": str(path),
    }


def mark(
    db: Database,
    *,
    config: Config | None = None,
    reason: str = "manual maintenance",
    minutes: int = DEFAULT_MINUTES,
    at: datetime | None = None,
) -> dict[str, Any]:
    """写维护标记（原子替换，覆盖旧标记）。返回和 ``active()`` 同构的状态。"""
    cfg = config or Config()
    path = _file(db, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = at or now()
    if start.tzinfo is None:
        start = start.astimezone()
    span = max(1, int(minutes))
    payload = {
        "reason": str(reason or "manual maintenance"),
        "started_at": to_iso(start),
        "until": to_iso(start + timedelta(minutes=span)),
        "minutes": span,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return {
        "ok": True,
        "active": True,
        "expired": False,
        "reason": payload["reason"],
        "started_at": payload["started_at"],
        "until": payload["until"],
        "remaining_seconds": span * 60,
        "minutes": span,
        "path": str(path),
    }


def clear(db: Database, *, config: Config | None = None) -> dict[str, Any]:
    """删维护标记；本来就没有也算成功（幂等）。"""
    cfg = config or Config()
    path = _file(db, cfg)
    existed = path.exists()
    if existed:
        try:
            path.unlink()
        except OSError as exc:  # 权限/占用：把原因带回去，别假装清了
            return {"ok": False, "active": True, "cleared": False, "path": str(path), "error": str(exc)}
    return {
        "ok": True,
        "active": False,
        "expired": False,
        "reason": "",
        "started_at": "",
        "until": "",
        "remaining_seconds": 0,
        "cleared": existed,
        "path": str(path),
    }


__all__ = ["active", "mark", "clear", "FILENAME", "DEFAULT_MINUTES"]
