"""M9 备份与**恢复演练**。

备份不是「复制一个 .db 文件」：WAL 模式下直接复制可能拿到半截状态。这里用
SQLite 的在线备份 API（``Connection.backup``）生成一致副本，再做：

1. ``PRAGMA integrity_check``；
2. ``sha256``；
3. 写入 ``snapshot`` 表 + ``event_log``。

恢复演练会**真的把副本打开**，验事件哈希链、外键、记忆条数，再统计恢复耗时；
整个过程只读副本，不碰活库。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.config import Config
from ..base.db import Database
from ..base.ids import ulid


def _dir(db: Database, cfg: Config) -> Path:
    if str(cfg.get("runtime.backup_dir", "") or ""):
        return Path(str(cfg.get("runtime.backup_dir")))
    if str(cfg.get("sleep.backup_dir", "") or ""):
        return Path(str(cfg.get("sleep.backup_dir")))
    base = Path.cwd() if db.path == ":memory:" else Path(db.path).resolve().parent
    return base / "backups"


def _sidecars(path: Path) -> list[Path]:
    """SQLite 的 sidecar 文件名（-wal/-shm/-journal）；只读检查也会顺手建出来。"""
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _drop_sidecars(path: Path) -> None:
    """清掉某个备份文件旁边的 sidecar（校验时的只读连接会建 -shm/-wal）。

    不修就会变成「表行删了、目录里还留一堆 .tmp-shm」——轮转越勤垃圾越多。
    """
    for side in _sidecars(path):
        try:
            if side.exists():
                side.unlink()
        except OSError:  # 被占用就当没这个文件；下次轮转再清
            pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        memory = conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
        event = conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM memory WHERE status IN ('active','contested')"
        ).fetchone()["c"]
        return {"memory": int(memory), "event": int(event), "active": int(active)}
    finally:
        conn.close()


def _check_copy(path: Path) -> dict[str, Any]:
    """只读检查副本；返回每项检查与总数。"""
    checks: list[dict[str, Any]] = []
    counts: dict[str, Any] = {}
    chain_ok = False
    chain_detail = ""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity = bool(row and row[0] == "ok")
        checks.append({"name": "integrity_check", "ok": integrity, "detail": row[0] if row else "no result"})
        quick = conn.execute("PRAGMA quick_check").fetchone()
        checks.append({"name": "quick_check", "ok": bool(quick and quick[0] == "ok"), "detail": quick[0] if quick else ""})
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks.append({"name": "foreign_key_check", "ok": not foreign, "detail": f"{len(foreign)} violations"})
        try:
            chain_ok, chain_detail = events.verify_chain(conn)
        except sqlite3.DatabaseError as exc:
            chain_ok, chain_detail = False, str(exc)
        checks.append({"name": "event_chain", "ok": chain_ok, "detail": chain_detail})
        counts = {
            "memory": conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"],
            "event": conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"],
            "active": conn.execute(
                "SELECT COUNT(*) c FROM memory WHERE status IN ('active','contested')"
            ).fetchone()["c"],
            "reflection": conn.execute("SELECT COUNT(*) c FROM reflection").fetchone()["c"],
            "snapshot": conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"],
        }
    finally:
        conn.close()
    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "counts": counts,
        "chain_detail": chain_detail,
    }


def _record_metric(db: Database, name: str, value: float, day: str | None = None) -> None:
    db.conn.execute(
        "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
        (now_iso(), day or datetime.now().astimezone().date().isoformat(), name, float(value), "{}"),
    )


def run(
    db: Database,
    *,
    config: Config | None = None,
    subject: str = "self",
    kind: str = "manual",
    tag: str = "manual",
    keep: int | None = None,
) -> dict[str, Any]:
    """在线备份一份，校验并登记。``tag`` 只写进事件，便于区分手动/定时。"""
    cfg = config or Config()
    directory = _dir(db, cfg)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    target = directory / f"pmem-backup-{stamp}-{ulid()}.db"
    temp = directory / f".{target.name}.tmp"
    started = time.perf_counter()
    db.checkpoint()
    dst = sqlite3.connect(str(temp))
    try:
        db.conn.backup(dst)
    finally:
        dst.close()

    try:
        checked = _check_copy(temp)
        if not checked["ok"]:
            raise RuntimeError("backup verification failed: " + json.dumps(checked["checks"], ensure_ascii=False))
        digest = _sha256(temp)
        size = temp.stat().st_size
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
        _drop_sidecars(temp)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    snapshot_id = ulid()
    ts = now_iso()
    counts = checked["counts"]
    final_kind = kind if kind in ("daily", "monthly", "manual", "pre-migrate") else "manual"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO snapshot(id, created_at, path, size_bytes, sha256, schema_version, "
            "memory_count, verified_at, kind) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                ts,
                str(target),
                size,
                digest,
                db.meta("schema_version") or "",
                int(counts.get("memory", 0)),
                ts,
                final_kind,
            ),
        )
        events.append(
            conn,
            actor="system:backup",
            op="ops.backup",
            target_tbl="snapshot",
            target_id=snapshot_id,
            subject_id=subject,
            payload={
                "path": str(target),
                "sha256": digest,
                "size_bytes": size,
                "counts": counts,
                "tag": tag,
                "verified": True,
            },
        )
        _record_metric(db, "ops.backup.size_bytes", size)
        _record_metric(db, "ops.backup.duration_ms", elapsed_ms)
        _record_metric(db, "ops.backup.memories", int(counts.get("memory", 0)))

    pruned = prune(db, config=cfg, keep=keep)
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "path": str(target),
        "size_bytes": size,
        "sha256": digest,
        "memory_count": int(counts.get("memory", 0)),
        "counts": counts,
        "duration_ms": elapsed_ms,
        "verified": True,
        "checks": checked["checks"],
        "pruned": pruned,
        "kind": final_kind,
        "tag": tag,
    }


def _prune_kind(db: Database, kind: str, limit: int) -> list[str]:
    """保留某类快照最近的 ``limit`` 条（rowid 倒序），更早的文件与表行一起清。"""
    rows = db.conn.execute(
        "SELECT rowid AS rid, id, path FROM snapshot WHERE kind=? ORDER BY rowid DESC",
        (kind,),
    ).fetchall()
    return _prune_rows(db, rows, limit)


def _daily_pool(path: str) -> str:
    """日快照的两条生产线：调度器离线备份 ``pmem-backup-*`` / 睡眠的每日副本 ``pmem-YYYYMMDD``。

    两者同属 ``kind='daily'``（表上有 CHECK 约束，不轻易加新 kind），但混在一个池子里轮转
    会互相挤：一天各产一份，7 份就只有 3.5 天。所以按文件名前缀分池，各自保留 7 份。
    """
    return "sched" if Path(path).name.startswith("pmem-backup-") else "sleep"


def _prune_rows(db: Database, rows: list[Any], limit: int) -> list[str]:
    removed: list[str] = []
    for row in rows[limit:]:
        path = Path(row["path"])
        try:
            if path.exists():
                path.unlink()
            _drop_sidecars(path)  # drill/list 打开过的旧文件可能还留着 -wal/-shm
        except OSError:
            continue  # 文件删不掉就留着行，下次再试（别留下「行没了文件还在」）
        db.conn.execute("DELETE FROM snapshot WHERE id=?", (row["id"],))
        removed.append(str(path))
    return removed


def prune(db: Database, *, config: Config | None = None, keep: int | None = None) -> list[str]:
    """分级清理旧快照（按 rowid 倒序，文件 + 表行一起清）。

    - ``manual``：``runtime.backup_keep``（默认 14；``keep`` 参数只覆盖这一类）
    - ``daily``：``runtime.daily_keep``（默认 7），**按生产线分池各留 7 份**——
      调度器 04:00 的离线备份与睡眠的每日副本是两条独立的安全网，混算会各减半。
    - ``monthly`` / ``pre-migrate``：不删（月报与迁移前快照是人工节点，不该被自动轮转掉）

    为什么分级：手动备份是「我要留的证据」，日快照是「最近一周能回滚」，两者
    价值与体积诉求不同；原来只清理 manual，等于日快照只增不减。
    """
    cfg = config or Config()
    manual_keep = max(
        1, int(keep if keep is not None else cfg.get("runtime.backup_keep", 14) or 14)
    )
    daily_keep = max(1, int(cfg.get("runtime.daily_keep", 7) or 7))
    removed = _prune_kind(db, "manual", manual_keep)
    rows = db.conn.execute(
        "SELECT rowid AS rid, id, path FROM snapshot WHERE kind='daily' ORDER BY rowid DESC"
    ).fetchall()
    pools: dict[str, list[Any]] = {}
    for row in rows:
        pools.setdefault(_daily_pool(row["path"]), []).append(row)
    for pool_rows in pools.values():
        removed.extend(_prune_rows(db, pool_rows, daily_keep))
    return removed


def counts(db: Database) -> dict[str, int]:
    """按 kind 统计快照数量；``total`` 是合计。给 ``backup list/counts`` 看保留现状。"""
    rows = db.conn.execute("SELECT kind, COUNT(*) c FROM snapshot GROUP BY kind").fetchall()
    out: dict[str, int] = {row["kind"]: int(row["c"]) for row in rows}
    out["total"] = sum(out.values())
    return out


def _choose(db: Database, source: str | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    if source:
        path = Path(source)
        row = db.conn.execute(
            "SELECT * FROM snapshot WHERE path=? ORDER BY created_at DESC LIMIT 1", (str(path),)
        ).fetchone()
        return (dict(row) if row else None), (path if path.exists() else None)
    row = db.conn.execute(
        "SELECT * FROM snapshot WHERE kind='manual' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = db.conn.execute("SELECT * FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None, None
    path = Path(row["path"])
    return dict(row), (path if path.exists() else None)


def drill(
    db: Database,
    *,
    config: Config | None = None,
    subject: str = "self",
    source: str | None = None,
) -> dict[str, Any]:
    """恢复演练：校验最新备份，并在临时副本上真的打开、查数、测耗时。"""
    cfg = config or Config()
    row, path = _choose(db, source)
    if path is None:
        return {
            "ok": False,
            "error": "没有可用的备份文件",
            "source": source,
            "checks": [],
        }
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    digest = _sha256(path)
    size = path.stat().st_size
    checks.append({"name": "file_exists", "ok": True, "detail": str(path)})
    try:
        live_counts = _counts(path)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"备份无法读取：{exc}",
            "source": str(path),
            "checks": checks,
        }
    if row is not None:
        checks.append(
            {"name": "sha256_matches", "ok": row.get("sha256") == digest, "detail": digest[:16]}
        )
        checks.append(
            {
                "name": "size_matches",
                "ok": int(row.get("size_bytes") or 0) == size,
                "detail": f"{size} bytes",
            }
        )
        checks.append(
            {
                "name": "memory_count_matches",
                "ok": int(row.get("memory_count") or -1) == int(live_counts["memory"]),
                "detail": f"{live_counts['memory']} memories",
            }
        )
    else:
        checks.append({"name": "snapshot_row", "ok": False, "detail": "文件不在 snapshot 表里"})

    try:
        copy_check = _check_copy(path)
    except Exception as exc:
        checks.append({"name": "readable", "ok": False, "detail": str(exc)})
        copy_check = {"ok": False, "checks": [], "counts": live_counts, "chain_detail": ""}
    checks.extend(copy_check["checks"])

    restore_ok = False
    restore_ms = 0
    restored: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="pmem-drill-", dir=str(path.parent)) as tempdir:
        target = Path(tempdir) / "restored.db"
        t0 = time.perf_counter()
        shutil.copy2(path, target)
        try:
            restored_db = Database(str(target), read_only=True)
            try:
                restored = {
                    "memory": restored_db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"],
                    "event": restored_db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"],
                    "integrity_ok": restored_db.integrity_ok(),
                }
                restore_ok = bool(restored["integrity_ok"])
            finally:
                restored_db.close()
        except Exception as exc:  # pragma: no cover - 损坏备份
            checks.append({"name": "restore_open", "ok": False, "detail": str(exc)})
        restore_ms = int((time.perf_counter() - t0) * 1000)
    checks.append({"name": "restore_open", "ok": restore_ok, "detail": f"{restore_ms} ms"})
    if row is not None and restored:
        checks.append(
            {
                "name": "restored_counts_match",
                "ok": int(restored.get("memory", -1)) == int(row.get("memory_count") or -1),
                "detail": f"{restored.get('memory')} vs {row.get('memory_count')}",
            }
        )

    ok = all(item["ok"] for item in checks)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ts = now_iso()
    with db.tx() as conn:
        events.append(
            conn,
            actor="system:backup",
            op="ops.backup.drill",
            target_tbl="snapshot",
            target_id=str(row.get("id") if row else path.name),
            subject_id=subject,
            payload={
                "path": str(path),
                "ok": ok,
                "restore_ms": restore_ms,
                "checks": checks,
            },
        )
        _record_metric(db, "ops.backup.drill_ok", 1 if ok else 0)
        _record_metric(db, "ops.backup.restore_ms", restore_ms)
    return {
        "ok": ok,
        "source": str(path),
        "snapshot_id": row.get("id") if row else None,
        "size_bytes": size,
        "sha256": digest,
        "restore_ms": restore_ms,
        "elapsed_ms": elapsed_ms,
        "counts": copy_check["counts"],
        "restored": restored,
        "checks": checks,
    }


def list_backups(db: Database) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM snapshot ORDER BY created_at DESC, id DESC LIMIT 100"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        path = Path(row["path"])
        out.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "kind": row["kind"],
                "path": row["path"],
                "exists": path.exists(),
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
                "memory_count": row["memory_count"],
                "verified_at": row["verified_at"],
            }
        )
    return out


__all__ = ["run", "drill", "prune", "counts", "list_backups"]
