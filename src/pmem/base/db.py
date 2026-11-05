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
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
        self._migrate_event_log_run(conn)
        for stmt in schema.DDL:
            conn.execute(stmt)
        # 老库补建 0.30.0 新增的索引：DDL 里已有 CREATE INDEX IF NOT EXISTS，
        # 这里再显式跑一遍（幂等），保证「先有表、后加索引」的旧库开一次就能补上。
        self._migrate_indexes(conn)
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

    def _migrate_event_log_run(self, conn: object) -> None:
        """老库兜底：event_log 缺 ``run_id`` 列就先补列。

        ``run_id`` 是「一次运行改了什么」的因果链键，schema.DDL 里的
        ``idx_event_run`` 依赖它。0.24.3 引入 event_log 时列就一起在了，
        正常库不会走到这里；但早期开发库/手改库一旦缺列，直接建索引会让
        整个库打不开——先补列，再让 DDL 幂等建索引。
        """
        exists = conn.execute(  # type: ignore[attr-defined]
            "SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'"
        ).fetchone()
        if exists is None:
            return
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_log)")}  # type: ignore[attr-defined]
        if "run_id" not in cols:
            conn.execute("ALTER TABLE event_log ADD COLUMN run_id TEXT")  # type: ignore[attr-defined]

    # 0.30.0 新增、且要能补到老库上的索引（DDL 里也有同样语句，这里两个入口都幂等）。
    _EXTRA_INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_mem_episode ON memory(episode_id)",
        "CREATE INDEX IF NOT EXISTS idx_entity_bridge ON entity(subject_id, status, mention_count DESC)",
    )

    def _migrate_indexes(self, conn: object) -> None:
        """老库幂等补建新索引（缺表/缺列时静默跳过，下一次 init 再补）。"""
        for stmt in self._EXTRA_INDEXES:
            try:
                conn.execute(stmt)  # type: ignore[attr-defined]
            except sqlite3.DatabaseError:
                # 只读库 / 更老的库结构：索引是增强，不能因此让库打不开。
                continue

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

    def _reset_after_failed_tx(self, conn: sqlite3.Connection) -> None:
        """事务失败后收尸：连接不能停在「事务还开着」的不确定状态。

        为什么要有这一步：SQLite 的 COMMIT 撞 SQLITE_BUSY 时事务仍然活着。
        如果原样把连接留下，下一次 ``tx()`` 会看到 ``in_transaction=True``、
        把它当成嵌套事务复用，于是后续写入全挂在这个旧事务里、再也不会提交
        ——长驻进程（MCP / REST / scheduler）会「写成功但永不落盘」，还一直
        占着写锁挡住别的进程。

        所以：还有事务就先补一次 ``ROLLBACK``；ROLLBACK 也失败就把连接丢掉
        （``self._conn = None``，下次访问 ``conn`` 属性按 ``_open()`` 重建），
        把错误收敛成「这一轮失败」，而不是「从此静默丢写」。
        """
        try:
            if not conn.in_transaction:
                return
        except sqlite3.DatabaseError:  # 连接已经关了/不可用：没有事务状态可收
            return
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.close()
            self._conn = None

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
        # 重试耗尽：最后一次 COMMIT 还可能抛（BUSY 之外也可能是 I/O）。
        # 无论哪种，都不能把「事务还开着」的连接原样还回去（见 _reset_after_failed_tx）。
        try:
            conn.execute("COMMIT")
        except BaseException:
            self._reset_after_failed_tx(conn)
            raise

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务。已在事务中则复用（不嵌套 BEGIN）。

        抢锁失败（SQLITE_BUSY / database is locked）会带抖动重试：
        并发写的正确语义是「等一会儿」，不是「丢这一条」。

        失败（含 COMMIT 重试耗尽）时会把连接收回到干净状态，
        必要时丢弃重开，避免后续写入挂在一个永不提交的旧事务上。
        """
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        self._begin_with_retry(conn)
        try:
            yield conn
        except BaseException:
            self._reset_after_failed_tx(conn)
            raise
        else:
            try:
                self._commit_with_retry(conn)
            except BaseException:
                # 非 busy 的 COMMIT 失败不会走到 _commit_with_retry 的收尸分支，
                # 在这里再兜一次（helper 幂等：事务已清就什么都不做）。
                self._reset_after_failed_tx(conn)
                raise

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

    def vacuum(self, *, analyze: bool = True) -> dict[str, Any]:
        """物理擦除：checkpoint → ``VACUUM`` →（可选）``PRAGMA optimize`` → 再 checkpoint。

        为什么要 VACUUM 而不是只删行：SQLite 删行只是把页挂回 freelist，
        **已删内容的字节还留在文件里**。VACUUM 会把全库重写进新文件，
        freelist 里的旧字节随之消失——「真删」才算真删。

        前置条件与代价：
        - 只读连接直接拒绝（返回 ``ok=False``，不抛异常，方便 CLI 统一处理）；
        - VACUUM 需要独占写锁、耗时与库大小成正比，期间其它写入会被 busy_timeout
          挡在外面。调用方应开维护窗口（``ops.maintenance``）再跑；
        - 内存库（``:memory:``）也能 VACUUM，但没有文件大小可报（记 0）。

        返回 ``{ok, before_bytes, after_bytes, before_pages, after_pages,
        freelist_before, freelist_after, before_total_bytes, after_total_bytes,
        saved_bytes, wal_before_bytes, wal_after_bytes, analyze, duration_ms}``。
        ``before/after_total_bytes`` 把 ``-wal`` 一起算（VACUUM 会把 WAL 并回主文件，
        只看 db 文件大小会出现假增长）。
        """
        if self.read_only:
            return {"ok": False, "reason": "read-only", "error": "只读连接不能 VACUUM"}
        path = Path(self.path) if self.path != ":memory:" else None
        started = time.perf_counter()

        def file_size(candidate: Path | None) -> int:
            return int(candidate.stat().st_size) if candidate is not None and candidate.exists() else 0

        def wal_path(candidate: Path | None) -> Path | None:
            return Path(str(candidate) + "-wal") if candidate is not None else None

        before_bytes = file_size(path)
        wal_before = file_size(wal_path(path))
        before_pages = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_before = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        self.checkpoint()
        try:
            self.conn.execute("VACUUM")
            if analyze:
                self.conn.execute("PRAGMA optimize")
        except sqlite3.DatabaseError as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "before_bytes": before_bytes,
                "before_pages": before_pages,
                "freelist_before": freelist_before,
            }
        # WAL 模式下 VACUUM 的结果可能先落在 -wal 里：再 checkpoint 一次，
        # 主库文件才是重写后的紧凑版本（「物理擦除」才有可验证的意义）。
        self.checkpoint()
        after_bytes = file_size(path)
        wal_after = file_size(wal_path(path))
        after_pages = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_after = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        # WAL 里的数据 VACUUM 时会并回主文件：只比 db 文件大小会出现「4 KB → 569 KB」
        # 这种假增长，所以总量把 WAL 一起算，saved_bytes 用总量差。
        before_total = before_bytes + wal_before
        after_total = after_bytes + wal_after
        return {
            "ok": True,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "before_pages": before_pages,
            "after_pages": after_pages,
            "freelist_before": freelist_before,
            "freelist_after": freelist_after,
            "before_total_bytes": before_total,
            "after_total_bytes": after_total,
            "saved_bytes": max(0, before_total - after_total),
            "wal_before_bytes": wal_before,
            "wal_after_bytes": wal_after,
            "analyze": bool(analyze),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    def integrity_ok(self) -> bool:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")

    def integrity_detail(self) -> dict:
        """完整性检查的**可解释**版本：``ok`` 可能是 True / False / None。

        - True：通过；
        - False：真的坏了（附 errors 前几条）；
        - None：这次没查成（库被锁、文件不可读……附 error 文本）。
        为什么要分开：以前「查不了」也被当成「坏了」，别人看到 integrity_ok=false
        会以为记忆库损坏——其实是那一瞬间没读到。分辨清楚才修得对。
        """
        try:
            rows = self.conn.execute("PRAGMA integrity_check").fetchall()
        except Exception as exc:  # noqa: BLE001 读不到 ≠ 坏了
            return {"ok": None, "error": f"{type(exc).__name__}: {exc}"}
        messages = [str(row[0]) for row in rows]
        if messages == ["ok"]:
            return {"ok": True}
        return {"ok": False, "errors": messages[:5]}
