"""事件日志：唯一允许"人人追加"的表，派生状态可由它重放（P3）。

三条不变量：

1. **只追加**：没有 UPDATE / DELETE，真删只写墓碑事件（payload 里只留 id 与原因）；
2. **哈希链**：``hash = sha256(prev_hash ‖ ts ‖ actor ‖ op ‖ target ‖ payload)``，
   ``pmem doctor --chain`` 可以查外部改动或被截断。但哈希链只能发现「改/删中段」，
   删掉**尾部**若干行后链仍然自洽；
3. **锚点**：``write_anchor`` 把 ``(count, last_seq, last_hash)`` 记进 ``schema_meta``，
   ``check_anchor`` 拿它当外部参照 —— 条数变少或锚点事件的 hash 变了就红，
   于是「砍尾巴」也能被发现。备份/快照时写锚点，doctor 的 I22 校验它。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .clock import now_iso
from .ids import ulid

# 锚点存在 schema_meta 里的键名。改动这里 = 破坏已有库的锚点，别动。
ANCHOR_COUNT_KEY = "event_anchor_count"
ANCHOR_SEQ_KEY = "event_anchor_seq"
ANCHOR_HASH_KEY = "event_anchor_hash"
ANCHOR_KEYS = (ANCHOR_COUNT_KEY, ANCHOR_SEQ_KEY, ANCHOR_HASH_KEY)


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


# ---- 链锚点（防尾部截断）-------------------------------------------------
#
# 哈希链能发现「改中段/删中段」：后继的 prev_hash 对不上。但删掉**尾部**后，
# 剩下的链仍然完全自洽 —— 单靠 verify_chain 永远发现不了审计日志被截断。
# 锚点就是外部参照：备份/快照时把当前 (count, last_seq, last_hash) 记进 schema_meta，
# 之后校验「条数没变少」+「锚点那条事件还在、hash 没变」。只能追加，不能砍、不能改。
# 键名用 schema_meta 而不是新表：零 schema 变更，老库开箱即用（无锚点 → 不检测）。


def write_anchor(conn: sqlite3.Connection) -> dict[str, Any]:
    """把当前事件链链尾写成锚点（``schema_meta`` 三行，幂等覆盖）。

    调用约定：调用方负责事务（``with db.tx() as conn: events.write_anchor(conn)``），
    备份/快照在拷完、校验完后调用（备份/快照模块负责接进来）。
    空链也会写（count=0、seq/hash 为空）—— 此时只能防「有→少」，防不了空链本身。
    """
    row = conn.execute("SELECT COUNT(*) c, MAX(seq) s FROM event_log").fetchone()
    count = int(row[0] or 0)
    last_seq = row[1]
    last_hash: str | None = None
    tail = conn.execute("SELECT hash FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
    if tail is not None and tail[0] is not None:
        last_hash = str(tail[0])
    ts = now_iso()
    values = {
        ANCHOR_COUNT_KEY: str(count),
        ANCHOR_SEQ_KEY: "" if last_seq is None else str(int(last_seq)),
        ANCHOR_HASH_KEY: last_hash or "",
    }
    for key, value in values.items():
        conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, ts),
        )
    return {
        "count": count,
        "last_seq": int(last_seq) if last_seq is not None else None,
        "last_hash": last_hash,
        "updated_at": ts,
    }


def check_anchor(conn: sqlite3.Connection) -> tuple[bool, str]:
    """校验链没有越过锚点（只允许追加）；返回 ``(ok, 说明)``。

    判据（三层，缺一层就漏一种攻击面）：

    1. 当前事件数 < 锚点条数 → 尾部被删（砍尾巴）→ 红；
    2. 锚点那条事件的 hash 找不到/对不上 → 中段或尾部被改/被删 → 红；
    3. 条数没变但链尾 hash 与锚点不同 → 尾部被改写 → 红。

    锚点不存在时返回 ``(True, "尚无…")``：没写过锚点的库照样能过体检，
    只是不宣称「截断可检测」（写锚点由备份/快照负责）。
    """
    rows = conn.execute(
        "SELECT key, value FROM schema_meta WHERE key IN (?,?,?)", ANCHOR_KEYS
    ).fetchall()
    meta: dict[str, str] = {str(row[0]): ("" if row[1] is None else str(row[1])) for row in rows}
    if ANCHOR_COUNT_KEY not in meta:
        return True, "尚无事件链锚点（备份/快照会写；写了之后才能检测尾部截断）"
    try:
        anchor_count = int(meta.get(ANCHOR_COUNT_KEY) or 0)
    except (TypeError, ValueError):
        return False, f"锚点损坏：{ANCHOR_COUNT_KEY}={meta.get(ANCHOR_COUNT_KEY)!r} 不是整数"
    anchor_seq_raw = meta.get(ANCHOR_SEQ_KEY) or ""
    anchor_hash = meta.get(ANCHOR_HASH_KEY) or ""

    row = conn.execute("SELECT COUNT(*) c, MAX(seq) s FROM event_log").fetchone()
    current_count = int(row[0] or 0)
    if current_count < anchor_count:
        return False, f"事件数少于锚点：{current_count} < {anchor_count}（尾部被截断）"
    if anchor_count > 0 and not anchor_hash:
        return False, "锚点缺 hash（锚点损坏，无法判断是否被改写）"
    if anchor_hash and anchor_seq_raw:
        try:
            anchor_seq = int(anchor_seq_raw)
        except (TypeError, ValueError):
            return False, f"锚点损坏：{ANCHOR_SEQ_KEY}={anchor_seq_raw!r} 不是整数"
        probe = conn.execute("SELECT hash FROM event_log WHERE seq=?", (anchor_seq,)).fetchone()
        if probe is None:
            return False, f"锚点事件 seq={anchor_seq} 不存在（被截断或被改写）"
        if str(probe[0]) != anchor_hash:
            return False, f"锚点事件 seq={anchor_seq} 的 hash 变了（事件被改写）"
    if current_count == anchor_count:
        tail = conn.execute("SELECT hash FROM event_log ORDER BY seq DESC LIMIT 1").fetchone()
        current_hash = str(tail[0]) if tail is not None and tail[0] is not None else ""
        if anchor_hash and current_hash != anchor_hash:
            return False, "链尾 hash 与锚点不一致（尾部被改写）"
    return True, f"锚点 OK：锚点 {anchor_count} 条 → 现在 {current_count} 条（只追加）"


# ---- 幂等 ----------------------------------------------------------------

def find_by_idem_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """按幂等键取**已提交**事件（``event_log.idem_key`` 唯一）。

    用途：睡眠阶段的跨进程单飞。阶段事件带 ``idem_key=f"{run_id}:{stage}"``，
    同一 run 的同一阶段全局只有一条；后到的进程据此判断"这个阶段已经有人提交了"
    ——先查再跳过，抢跑撞键则整段事务回滚，不产生第二份副作用。
    """
    return conn.execute("SELECT * FROM event_log WHERE idem_key=?", (key,)).fetchone()


def idem_lookup(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM idem WHERE key=?", (key,)).fetchone()


def idem_remember(conn: sqlite3.Connection, key: str, op: str, target_id: str, payload: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO idem(key, op, target_id, payload, created_at) VALUES(?,?,?,?,?)",
        (key, op, target_id, _canonical(payload), now_iso()),
    )
