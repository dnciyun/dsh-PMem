"""日快照（s8）：``VACUUM INTO`` + sha256 + 完整性抽检。

为什么要单独一个文件：SQLite 的 WAL 让"直接复制 pmem.db"不可靠。
``VACUUM INTO`` 生成的是已提交状态的紧凑副本，可在库损坏时直接顶上。

快照也做幂等：同一天、同一路径、sha256 没变 → 不重复写文件、不重复加行。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..base.clock import now_iso
from ..base.db import Database
from ..base.ids import ulid


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path, *, verify: bool) -> tuple[bool, int, int]:
    """返回 (完整性是否 ok, 记忆条数, 事件序号)；事件序号用来判断库有没有前进。"""
    conn = sqlite3.connect(str(path))
    try:
        if verify:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                return False, 0, 0
        count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM event_log").fetchone()
        return True, int(count), int(seq_row[0])
    finally:
        conn.close()


def _live_fingerprint(db: Database) -> tuple[int, int]:
    row = db.conn.execute(
        "SELECT (SELECT COUNT(*) FROM memory) AS m, "
        "(SELECT COALESCE(MAX(seq), 0) FROM event_log) AS e"
    ).fetchone()
    return int(row["m"]), int(row["e"])


def create(
    db: Database,
    *,
    day_key: str,
    out_dir: str | Path,
    kind: str = "daily",
    verify: bool = True,
) -> dict[str, Any]:
    """创建（或复用）当天快照。返回快照元信息。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"pmem-{day_key.replace('-', '')}.db"

    existing = db.conn.execute(
        "SELECT * FROM snapshot WHERE path=? ORDER BY created_at DESC LIMIT 1",
        (str(target),),
    ).fetchone()
    prior = existing
    if target.exists() and existing is not None:
        digest = sha256_file(target)
        live_memories, live_seq = _live_fingerprint(db)
        ok, count, seq = _probe(target, verify=verify)
        if (
            ok
            and existing["sha256"] == digest
            and count == live_memories
            and seq == live_seq
        ):
            db.conn.execute(
                "UPDATE snapshot SET verified_at=?, memory_count=? WHERE id=?",
                (now_iso(), count, existing["id"]),
            )
            return {
                "snapshot_id": existing["id"],
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "sha256": digest,
                "memory_count": count,
                "verified": verify,
                "skipped": True,
                "kind": kind,
            }
        # 文件还在，但库已经前进（新记忆/新事件）或文件损坏 → 重建
        # 保留 prior：重建后更新同一行，不追加重复快照行

    temp = directory / f".pmem-{day_key}-{ulid()}.db"
    if temp.exists():
        temp.unlink()
    db.checkpoint()
    try:
        db.conn.execute("VACUUM INTO ?", (str(temp),))
        ok, count, _seq = _probe(temp, verify=verify)
        if not ok:
            raise RuntimeError("snapshot integrity check failed")
        digest = sha256_file(temp)
        size = temp.stat().st_size
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()

    snapshot_id = prior["id"] if prior is not None else ulid()
    ts = now_iso()
    schema_version = db.meta("schema_version") or ""
    if prior is not None:
        db.conn.execute(
            "UPDATE snapshot SET created_at=?, size_bytes=?, sha256=?, schema_version=?, "
            "memory_count=?, verified_at=?, kind=? WHERE id=?",
            (ts, size, digest, schema_version, count, ts if verify else None, kind, snapshot_id),
        )
    else:
        db.conn.execute(
            "INSERT INTO snapshot(id, created_at, path, size_bytes, sha256, schema_version, "
            "memory_count, verified_at, kind) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                ts,
                str(target),
                size,
                digest,
                schema_version,
                count,
                ts if verify else None,
                kind,
            ),
        )
    return {
        "snapshot_id": snapshot_id,
        "path": str(target),
        "size_bytes": size,
        "sha256": digest,
        "memory_count": count,
        "verified": verify,
        "skipped": False,
        "kind": kind,
    }
