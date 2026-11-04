"""SQLite 接入层：连接、PRAGMA、建库、事务。

工程约定（PLAN.md §4.2）：

- WAL + ``busy_timeout``，读写并发不打架；
- **所有写事务用 ``BEGIN IMMEDIATE``**，避免 SQLITE_BUSY 在升级锁时才爆发；
- 事务内不做 embedding 之类慢计算（先算完再进事务）；
- 单进程内一个连接复用，不做连接池（单写者纪律）。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import schema
from .clock import now_iso
from .ids import ulid

APPLICATION_ID = 0x504D454D  # 'PMEM'


class Database:
    """一个库文件 = 一个实例。线程不安全（单写者模型）。"""

    # 并发写重试（M21 补）：16 进程同时写同一个库时，SQLite 的 busy_timeout 会撞线，
    # 抛出 "database is locked"。这不是数据坏了，是锁等超时——所以要**重试**而不是把
    # 一次没抢到锁的写入丢掉。超高压测试抓到过一次真丢写（3200 条里丢 1 条）。
    BUSY_RETRIES = 8
    BUSY_BASE_DELAY = 0.02
    BUSY_MAX_DELAY = 0.5

    def __init__(self, path: str | Path, *, read_only: bool = False, timeout: float = 5.0) -> None:
        self.path = str(path)
        self.read_only = read_only
        self.timeout = timeout
        self._conn: sqlite3.Connection | None = None

    # -- 连接 --------------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._open()
        return self._conn

    def _open(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            uri = f"file:{Path(self.path).as_posix()}?mode=ro" if self.read_only else Path(self.path).as_posix()
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None)
        else:
            conn = sqlite3.connect(":memory:", timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if not self.read_only:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint = 1000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -32000")
        with contextlib.suppress(sqlite3.DatabaseError):
            conn.execute("PRAGMA mmap_size = 268435456")
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 建库 --------------------------------------------------------------
    def init(self, *, subject: str = "self", display: str = "我") -> None:
        """幂等建库：可反复调用。"""
        conn = self.conn
        self._migrate_entity_org(conn)
        self._migrate_last_surfaced(conn)
        for stmt in schema.DDL:
            conn.execute(stmt)
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        ts = now_iso()
        meta = {
            "schema_version": schema.SCHEMA_VERSION,
            "created_at": ts,
            "app": "PMemories",
            "codename": "PMem",
        }
        for key, value in meta.items():
            conn.execute(
                "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, ts),
            )
        conn.execute(
            "INSERT OR IGNORE INTO subject(id, kind, display, created_at) VALUES(?,?,?,?)",
            (subject, "self", display, ts),
        )
        for predicate, inverse, domain, range_, transitive, max_weight in schema.RELATION_TYPES:
            conn.execute(
                "INSERT OR IGNORE INTO relation_type(predicate, inverse, domain, range_, transitive, max_weight) "
                "VALUES(?,?,?,?,?,?)",
                (predicate, inverse, domain, range_, transitive, max_weight),
            )

    def _migrate_last_surfaced(self, conn: object) -> None:
        """1.4.0 → 1.5.0：记忆表补 ``last_surfaced_at``。

        为什么要单独一列：``last_used_at`` 以前被"浮现"和"召回"同时推进，
        于是「多久没用」这件事根本分不清——每轮自动注入一次卡，
        就等于每条记忆天天"被用"。

        拆开之后各归各位：

        - ``last_used_at``     真的被用到：显式召回 / 浮现回执 / 正面反馈；
        - ``last_surfaced_at`` 只是被浮现出来过：只推进轮换，不推进淡忘。

        迁移时把 ``last_used_at`` 回填进新列——那正是它历史上的含义。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory)")}  # type: ignore[attr-defined]
        if not cols or "last_surfaced_at" in cols:
            return
        conn.execute("ALTER TABLE memory ADD COLUMN last_surfaced_at TEXT")  # type: ignore[attr-defined]
        conn.execute("UPDATE memory SET last_surfaced_at = last_used_at")  # type: ignore[attr-defined]
        conn.execute(  # type: ignore[attr-defined]
            "CREATE INDEX IF NOT EXISTS idx_mem_last_surfaced ON memory(last_surfaced_at)"
        )

    def _migrate_entity_org(self, conn: object) -> None:
        """1.0.0 → 1.1.0：实体表补 ``org`` 类型。

        SQLite 改不了 CHECK 约束，只能重建。用 ``legacy_alter_table=ON``
        防止 RENAME 把 relation/entity_mention 的外键改指向旧表名；复制完
        再 DROP 旧表，外键仍指向新的 ``entity``。
        """
        import contextlib as _ctx

        row = conn.execute(  # type: ignore[attr-defined]
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity'"
        ).fetchone()
        if row is None or "'org'" in (row["sql"] or ""):
            return
        conn.execute("PRAGMA foreign_keys=OFF")  # type: ignore[attr-defined]
        conn.execute("PRAGMA legacy_alter_table=ON")  # type: ignore[attr-defined]
        try:
            conn.execute("BEGIN IMMEDIATE")  # type: ignore[attr-defined]
            conn.execute("ALTER TABLE entity RENAME TO entity_v10")  # type: ignore[attr-defined]
            conn.execute(schema.ENTITY_DDL)  # type: ignore[attr-defined]
            conn.execute(  # type: ignore[attr-defined]
                "INSERT INTO entity(seq,id,subject_id,type,name,name_norm,status,canonical_id,"
                "confidence,provenance,first_seen,last_seen,mention_count,extra_json) "
                "SELECT seq,id,subject_id,type,name,name_norm,status,canonical_id,"
                "confidence,provenance,first_seen,last_seen,mention_count,extra_json FROM entity_v10"
            )
            conn.execute("DROP TABLE entity_v10")  # type: ignore[attr-defined]
            for stmt in schema.ENTITY_INDEXES:
                conn.execute(stmt)  # type: ignore[attr-defined]
            conn.execute("COMMIT")  # type: ignore[attr-defined]
        except BaseException:
            with _ctx.suppress(Exception):
                conn.execute("ROLLBACK")  # type: ignore[attr-defined]
            raise
        finally:
            conn.execute("PRAGMA legacy_alter_table=OFF")  # type: ignore[attr-defined]
            conn.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    # -- 事务 --------------------------------------------------------------
    @staticmethod
    def _is_busy(error: BaseException) -> bool:
        message = str(error).lower()
        return "locked" in message or "busy" in message

    def _begin_with_retry(self, conn: sqlite3.Connection) -> None:
        import random
        import time

        last: BaseException | None = None
        for attempt in range(self.BUSY_RETRIES):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as error:
                if not self._is_busy(error):
                    raise
                last = error
                delay = min(self.BUSY_BASE_DELAY * (2**attempt), self.BUSY_MAX_DELAY)
                # 抖动：16 个进程同时醒来抢锁会再次撞车，错开一点
                time.sleep(delay + random.uniform(0, self.BUSY_BASE_DELAY))
        assert last is not None
        raise last

    def _commit_with_retry(self, conn: sqlite3.Connection) -> None:
        import random
        import time

        for attempt in range(self.BUSY_RETRIES):
            try:
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as error:
                if not self._is_busy(error):
                    raise
                delay = min(self.BUSY_BASE_DELAY * (2**attempt), self.BUSY_MAX_DELAY)
                time.sleep(delay + random.uniform(0, self.BUSY_BASE_DELAY))
        conn.execute("COMMIT")

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务。已在事务中则复用（不嵌套 BEGIN）。

        抢锁失败（SQLITE_BUSY / database is locked）会带抖动重试：
        并发写的正确语义是「等一会儿」，不是「丢这一条」。
        """
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        self._begin_with_retry(conn)
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("ROLLBACK")
            raise
        else:
            self._commit_with_retry(conn)

    # -- 元信息 ------------------------------------------------------------
    def meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )

    # -- 维护 --------------------------------------------------------------
    def new_id(self) -> str:
        return ulid()

    def checkpoint(self) -> None:
        with contextlib.suppress(sqlite3.DatabaseError):
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def integrity_ok(self) -> bool:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
