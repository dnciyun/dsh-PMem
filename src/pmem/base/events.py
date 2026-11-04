"""事件日志：唯一允许"人人追加"的表，派生状态可由它重放（P3）。

两条不变量：

1. **只追加**：没有 UPDATE / DELETE，真删只写墓碑事件（payload 里只留 id 与原因）；
2. **哈希链**：``hash = sha256(prev_hash ‖ ts ‖ actor ‖ op ‖ target ‖ payload)``，
   ``pmem doctor --chain`` 可以查外部改动或被截断。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .clock import now_iso
from .ids import ulid


def _canonical(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(prev: str | None, ts: str, actor: str, op: str, target: str, payload: str) -> str:
    h = hashlib.sha256()
    h.update((prev or "").encode("utf-8"))
    h.update(b"\x1f")
    h.update(f"{ts}\x1e{actor}\x1e{op}\x1e{target}\x1e{payload}".encode())
    return h.hexdigest()


def last_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT hash FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else None


def append(
    conn: sqlite3.Connection,
    *,
    actor: str,
    op: str,
    target_tbl: str | None = None,
    target_id: str | None = None,
    subject_id: str | None = None,
    payload: dict[str, Any] | None = None,
    idem_key: str | None = None,
    run_id: str | None = None,
) -> str:
    """追加一条事件，返回事件 id。调用方负责事务。"""
    ts = now_iso()
    prev = last_hash(conn)
    body = _canonical(payload)
    target = f"{target_tbl or ''}:{target_id or ''}"
    digest = _digest(prev, ts, actor, op, target, body)
    event_id = ulid()
    conn.execute(
        "INSERT INTO event_log(id, ts, actor, op, target_tbl, target_id, subject_id, "
        "payload_json, idem_key, run_id, prev_hash, hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, ts, actor, op, target_tbl, target_id, subject_id, body, idem_key, run_id, prev, digest),
    )
    return event_id


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, str]:
    """逐条重算哈希；返回 (是否连续, 说明)。"""
    prev: str | None = None
    count = 0
    for row in conn.execute(
        "SELECT seq, ts, actor, op, target_tbl, target_id, payload_json, hash FROM event_log ORDER BY seq"
    ):
        target = f"{row['target_tbl'] or ''}:{row['target_id'] or ''}"
        expect = _digest(prev, row["ts"], row["actor"], row["op"], target, row["payload_json"])
        if expect != row["hash"]:
            return False, f"hash mismatch at seq={row['seq']}"
        prev = row["hash"]
        count += 1
    return True, f"{count} events verified"


# ---- 幂等 ----------------------------------------------------------------

def idem_lookup(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM idem WHERE key=?", (key,)).fetchone()


def idem_remember(conn: sqlite3.Connection, key: str, op: str, target_id: str, payload: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO idem(key, op, target_id, payload, created_at) VALUES(?,?,?,?,?)",
        (key, op, target_id, _canonical(payload), now_iso()),
    )
