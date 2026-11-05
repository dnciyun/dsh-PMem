"""从备份恢复（维护窗口内，原子替换）。

为什么需要单独一个模块：``drill`` 只做「只读演练」，而真正恢复要动活库文件。
这件事有三个危险的默认值必须显式处理：

1. **必须开维护窗口**：daemon 正跑 sleep/backup 时换掉库文件，会让任务写进旧 inode
   （Windows 上则是直接替换失败）。所以默认要求 ``ops.maintenance`` 标记活跃，
   ``force=True`` 才跳过（手工救急用）。
2. **先备份当前库**：替换前用 ``VACUUM INTO`` 生成 ``<db>.pre-restore-<ts>``；
   恢复错了还能拿它回来。
3. **替换前清掉旧 sidecar**：SQLite 的 ``-wal/-shm`` 属于旧文件，留在原地会让
   新库被旧 WAL 覆盖写入（灾难性）。先关连接，再 ``os.replace``，再删旧 sidecar，
   最后只读重开验 integrity / 事件链 / 条数。

备份本身的校验（integrity / 事件链 / schema 版本不高于当前）在这里做一次，
失败直接拒绝，不触碰活库。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import events
from ..base import schema as schema_mod
from ..base.config import Config
from ..base.db import Database
from . import maintenance

SIDECARS = ("-wal", "-shm", "-journal")


def _version_tuple(value: str | None) -> tuple[int, ...]:
    parts = tuple(int(part) for part in re.findall(r"\d+", value or ""))
    return parts or (0,)


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _drop_sidecars(path: Path) -> list[str]:
    removed: list[str] = []
    for suffix in SIDECARS:
        side = path.with_name(path.name + suffix)
        try:
            if side.exists():
                side.unlink()
                removed.append(str(side))
        except OSError:  # 被占用：交给下一次打开时的 SQLite 自己处理
            continue
    return removed


def inspect_backup(backup_path: str | Path) -> dict[str, Any]:
    """只读检查备份：文件 / 完整性 / 外键 / 事件链 / schema 版本 / 条数。"""
    path = Path(backup_path)
    if not path.is_file():
        raise FileNotFoundError(f"备份文件不存在：{path}")
    conn = _ro_connect(path)
    try:
        integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = bool(integrity_row and integrity_row[0] == "ok")
        quick_row = conn.execute("PRAGMA quick_check").fetchone()
        quick_ok = bool(quick_row and quick_row[0] == "ok")
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        chain_ok, chain_detail = events.verify_chain(conn)
        schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        schema_version = str(schema_row["value"]) if schema_row else ""
        counts = {
            "memory": int(conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]),
            "event": int(conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]),
            "snapshot": int(conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"]),
        }
    finally:
        conn.close()
    return {
        "path": str(path),
        "size_bytes": int(path.stat().st_size),
        "integrity_ok": integrity_ok,
        "quick_check_ok": quick_ok,
        "foreign_key_violations": len(foreign),
        "chain_ok": chain_ok,
        "chain_detail": chain_detail,
        "schema_version": schema_version,
        "counts": counts,
    }


def _vacuum_copy(db: Database, target: Path) -> None:
    """把活库快照成 ``target``（VACUUM INTO，源库只读连接）。"""
    if target.exists():
        target.unlink()
    if db.path == ":memory:":
        db.conn.execute("VACUUM INTO ?", (str(target),))
        return
    source = sqlite3.connect(
        f"file:{Path(db.path).as_posix()}?mode=ro", uri=True, timeout=db.timeout
    )
    try:
        source.execute("VACUUM INTO ?", (str(target),))
    finally:
        source.close()


def _current_schema_version(db: Database, target: Path) -> str:
    if not target.exists():
        return schema_mod.SCHEMA_VERSION
    try:
        return str(db.meta("schema_version") or schema_mod.SCHEMA_VERSION)
    except sqlite3.DatabaseError:
        return schema_mod.SCHEMA_VERSION


def restore_backup(
    db_path: str | Path,
    backup_path: str | Path,
    *,
    force: bool = False,
    config: Config | None = None,
) -> dict[str, Any]:
    """用备份替换活库。成功返回报告；任何一步不满足条件都不动活库。

    参数：
    - ``db_path``：要恢复的活库路径（不存在时直接落备份）；
    - ``backup_path``：备份文件（``pmem-backup-*.db`` / ``pmem-YYYYMMDD.db`` 均可）；
    - ``force``：跳过维护窗口检查（默认要求 ``ops.maintenance`` 活跃）；
    - ``config``：用于定位维护标记文件的配置（与 daemon 一致）。

    返回 ``{ok, restored_from, db_path, pre_restore, replaced, checks,
    backup, current_schema_version, duration_ms}``。
    """
    cfg = config or Config()
    target = Path(db_path)
    source = Path(backup_path)
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []

    def fail(error: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error": error,
            "db_path": str(target),
            "restored_from": str(source),
            "checks": checks,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **extra,
        }

    if target.exists() and target.resolve() == source.resolve():
        return fail("备份文件与目标库是同一个文件")
    if not source.is_file():
        return fail(f"备份文件不存在：{source}")
    try:
        backup_info = inspect_backup(source)
    except Exception as exc:  # noqa: BLE001 坏备份/不可读
        return fail(f"备份不可读：{type(exc).__name__}: {exc}")
    checks.append({"name": "backup_integrity", "ok": backup_info["integrity_ok"], "detail": backup_info["path"]})
    checks.append(
        {
            "name": "backup_event_chain",
            "ok": backup_info["chain_ok"],
            "detail": backup_info["chain_detail"],
        }
    )
    if not backup_info["integrity_ok"]:
        return fail("备份完整性检查失败", backup=backup_info)
    if not backup_info["chain_ok"]:
        return fail("备份事件链校验失败", backup=backup_info)
    if backup_info["foreign_key_violations"]:
        return fail(f"备份外键检查失败（{backup_info['foreign_key_violations']} 处）", backup=backup_info)

    current_db = Database(str(target))
    pre_restore: str | None = None
    window: dict[str, Any] = {"active": False}
    try:
        current_schema = _current_schema_version(current_db, target)
        checks.append(
            {
                "name": "schema_not_newer",
                "ok": _version_tuple(backup_info["schema_version"]) <= _version_tuple(current_schema),
                "detail": f"backup {backup_info['schema_version']} vs current {current_schema}",
            }
        )
        if _version_tuple(backup_info["schema_version"]) > _version_tuple(current_schema):
            return fail(
                f"备份 schema {backup_info['schema_version']} 高于当前工具 {current_schema}，请先升级 PMem",
                backup=backup_info,
            )

        # 维护窗口：默认必须开着，force 才跳过。
        window = maintenance.active(current_db, config=cfg) if target.exists() else {"active": False}
        checks.append({"name": "maintenance_window", "ok": bool(window.get("active")) or force,
                       "detail": f"active={bool(window.get('active'))} force={bool(force)}"})
        if not window.get("active") and not force:
            return fail(
                "维护窗口未开启：恢复会替换库文件，请先 `pmem maintenance mark`（或传 force=True）",
                maintenance=window,
                backup=backup_info,
            )

        if target.exists():
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            pre_path = target.with_name(target.name + f".pre-restore-{stamp}")
            try:
                _vacuum_copy(current_db, pre_path)
                pre_restore = str(pre_path)
                checks.append({"name": "pre_restore_backup", "ok": True, "detail": pre_restore})
            except Exception as exc:  # noqa: BLE001 当前库都备份不了就不敢替换
                return fail(f"当前库预备份失败，已中止：{type(exc).__name__}: {exc}")
    finally:
        current_db.close()

    # Windows 上必须先关掉本进程里的连接再 os.replace；其它进程占着文件时
    # os.replace 会抛 PermissionError，此时原库文件没动，可以安全放弃。
    tmp = target.with_name(target.name + f".restore-{os.getpid()}.tmp")
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
        replaced = True
        checks.append({"name": "replaced", "ok": True, "detail": str(target)})
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return fail(
            f"替换库文件失败（请确认 DSH/MCP/daemon 已停止）：{type(exc).__name__}: {exc}",
            pre_restore=pre_restore,
        )
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

    # 旧库的 -wal/-shm 绝不能留给新库；先删再重开。
    dropped = _drop_sidecars(target)

    restored_counts: dict[str, int] = {}
    integrity_ok = False
    chain_ok = False
    chain_detail = ""
    try:
        reopened = Database(str(target), read_only=True)
        try:
            integrity_ok = bool(reopened.integrity_ok())
            chain_ok, chain_detail = events.verify_chain(reopened.conn)
            restored_counts = {
                "memory": int(reopened.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]),
                "event": int(reopened.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]),
            }
        finally:
            reopened.close()
    except Exception as exc:  # noqa: BLE001 恢复后打不开 = 必须报告
        return fail(
            f"恢复后重开校验失败：{type(exc).__name__}: {exc}",
            pre_restore=pre_restore,
            dropped_sidecars=dropped,
        )
    checks.append({"name": "restored_integrity", "ok": integrity_ok, "detail": "PRAGMA integrity_check"})
    checks.append({"name": "restored_event_chain", "ok": chain_ok, "detail": chain_detail})
    counts_match = restored_counts.get("memory") == backup_info["counts"]["memory"]
    checks.append(
        {
            "name": "restored_counts_match",
            "ok": counts_match,
            "detail": f"{restored_counts.get('memory')} vs {backup_info['counts']['memory']}",
        }
    )
    ok = bool(integrity_ok and chain_ok and counts_match)
    return {
        "ok": ok,
        "db_path": str(target),
        "restored_from": str(source),
        "pre_restore": pre_restore,
        "replaced": replaced,
        "backup": backup_info,
        "current_schema_version": current_schema,
        "restored_counts": restored_counts,
        "chain_detail": chain_detail,
        "dropped_sidecars": dropped,
        "maintenance": window,
        "forced": bool(force),
        "checks": checks,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


__all__ = ["restore_backup", "inspect_backup"]
