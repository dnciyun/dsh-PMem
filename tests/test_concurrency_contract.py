"""并发契约（0.29.1，任务 A）：锁重试、去重、证据合并、feedback、entity.merge 原子性。

守的是审计里实测出来的几处并发缺陷：
- ``journal.append`` 遇到 ``database is locked`` 必须走 db 的重试（等，不丢）；
- 16 线程同文 ``store.add`` 最终只能有 1 行，且所有调用都成功；
- 并发合并证据后 ``evidence_count`` 与 ``evidence_sessions/tiers`` 一致；
- 并发 ``feedback`` 不丢 use_count/importance 增量；
- ``entity.merge`` 中途失败不留半合并状态。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import unittest

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.base.ids import ulid
from pmem.memory import journal
from pmem.memory.feedback import apply as apply_feedback
from pmem.memory.store import MemoryStore
from pmem.net import entities

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})


class _FlakyConn:
    """在真实连接外面套一层：指定语句前 N 次抛错（默认 database is locked）。"""

    def __init__(
        self,
        real: sqlite3.Connection,
        statement: str,
        fails: int,
        message: str = "database is locked",
    ) -> None:
        self._real = real
        self._statement = statement.strip().upper()
        self._fails = fails
        self._message = message

    def execute(self, sql: str, *args: object):
        if self._fails > 0 and sql.strip().upper().startswith(self._statement):
            self._fails -= 1
            raise sqlite3.OperationalError(self._message)
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class ConcurrencyContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("concurrency-contract")
        self.db_path = self.dir / "pmem.db"
        self.db = Database(self.db_path)
        self.db.BUSY_BASE_DELAY = 0.0
        self.db.init()

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    # -- A1：journal.append 的锁重试与索引留痕 -------------------------------
    def test_journal_append_retries_on_lock(self) -> None:
        real = self.db.conn
        self.db._conn = _FlakyConn(real, "BEGIN IMMEDIATE", 2)  # noqa: SLF001 故意的
        result = journal.append(self.db, "锁重试期间写进来的一句原话", session_id="s1")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["indexed"], result)
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM journal_line").fetchone()["c"], 1
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM journal_fts").fetchone()["c"], 1
        )

    def test_journal_append_fts_failure_keeps_line_and_is_visible(self) -> None:
        real = self.db.conn
        self.db._conn = _FlakyConn(  # noqa: SLF001
            real, "INSERT INTO JOURNAL_FTS", 1, message="malformed inverted index"
        )
        result = journal.append(self.db, "FTS 写失败也不能丢掉这句原话", session_id="s1")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["indexed"], result)
        self.assertIn("index_error", result)
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM journal_line").fetchone()["c"], 1
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM journal_fts").fetchone()["c"], 0
        )
        event = self.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE op='journal.index_failed'"
        ).fetchone()["c"]
        self.assertEqual(event, 1)
        stats = journal.stats(self.db)
        self.assertEqual(stats["unindexed"], 1)
        self.assertEqual(stats["fts_rows"], 0)

    # -- A2 + A3：同文并发去重 / 并发合并证据 -------------------------------
    def test_concurrent_same_text_merges_to_one_row(self) -> None:
        text = "同一条原文被 16 个线程同时写：最终只能有一行。"
        n = 16
        barrier = threading.Barrier(n)
        results: list[dict[str, object]] = []
        errors: list[str] = []

        def one(tid: int) -> None:
            db = Database(self.db_path)  # 每个线程独立连接（Database 线程不安全）
            store = MemoryStore(db, config=CFG, subject="self", actor=f"t{tid}")
            try:
                barrier.wait()
                results.append(
                    store.add(
                        text,
                        provenance="observed",
                        source_kind="contract",
                        session_id=f"session-{tid}",
                    )
                )
            except Exception as exc:  # noqa: BLE001 并发下失败要计数
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                db.close()

        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [], errors)
        self.assertEqual(len(results), n)
        rows = self.db.conn.execute(
            "SELECT id, evidence_count, extra_json FROM memory WHERE text_raw=?", (text,)
        ).fetchall()
        self.assertEqual(len(rows), 1, "并发同文写出了重复行")
        row = rows[0]
        extra = json.loads(row["extra_json"])
        self.assertEqual(int(row["evidence_count"]), n)
        self.assertEqual(int(extra["evidence_sessions"]), n)
        self.assertEqual(len(extra["evidence_tiers"]), n)

    def test_concurrent_feedback_keeps_increments(self) -> None:
        store = MemoryStore(self.db, config=CFG, subject="self", actor="seed")
        memory = store.add("并发反馈测试记忆", provenance="observed")
        base_importance = float(
            self.db.conn.execute(
                "SELECT importance FROM memory WHERE id=?", (memory["id"],)
            ).fetchone()["importance"]
        )
        n = 16
        barrier = threading.Barrier(n)
        errors: list[str] = []

        def one(tid: int) -> None:
            db = Database(self.db_path)
            try:
                barrier.wait()
                apply_feedback(db, memory["id"], "useful", actor=f"u{tid}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                db.close()

        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [], errors)
        row = self.db.conn.execute(
            "SELECT use_count, importance FROM memory WHERE id=?", (memory["id"],)
        ).fetchone()
        self.assertEqual(int(row["use_count"]), n)
        self.assertAlmostEqual(
            float(row["importance"]), min(1.0, base_importance + 0.03 * n), places=2
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"], n
        )

    # -- A4：entity.merge 原子性 -------------------------------------------
    def _seed_merge_entities(self) -> tuple[str, str, int]:
        mem = MemoryStore(self.db, config=CFG, subject="self", actor="seed").add(
            "合并测试原话", provenance="observed"
        )
        seq = int(
            self.db.conn.execute("SELECT seq FROM memory WHERE id=?", (mem["id"],)).fetchone()["seq"]
        )
        now = "2026-11-04T10:00:00.000Z"
        keep_id, drop_id = ulid(), ulid()
        for entity_id, name, norm in ((keep_id, "保留实体", "保留实体"), (drop_id, "被并实体", "被并实体")):
            self.db.conn.execute(
                "INSERT INTO entity(id, subject_id, type, name, name_norm, status, confidence, "
                "provenance, first_seen, last_seen, mention_count, extra_json) "
                "VALUES(?,?, 'person', ?, ?, 'active', 0.8, 'observed', ?, ?, 1, '{}')",
                (entity_id, "self", name, norm, now, now),
            )
        self.db.conn.execute(
            "INSERT INTO entity_alias(id, entity_id, alias, alias_norm, lang, source, confidence, created_at) "
            "VALUES(?,?,?,?,NULL,'template',0.8,?)",
            (ulid(), drop_id, "旧名", "旧名", now),
        )
        self.db.conn.execute(
            "INSERT INTO entity_mention(entity_id, memory_seq, span_start, span_end, surface, confidence, extractor) "
            "VALUES(?,?,0,4,'合并测试',0.8,'template')",
            (drop_id, seq),
        )
        return keep_id, drop_id, seq

    def test_entity_merge_rolls_back_on_midway_failure(self) -> None:
        keep_id, drop_id, seq = self._seed_merge_entities()
        real = self.db.conn
        self.db._conn = _FlakyConn(  # noqa: SLF001
            real, "UPDATE ENTITY SET STATUS='MERGED'", 1, message="disk I/O error"
        )
        with self.assertRaises(sqlite3.OperationalError):
            entities.merge(self.db, keep_id, drop_id)
        drop = self.db.conn.execute(
            "SELECT status FROM entity WHERE id=?", (drop_id,)
        ).fetchone()
        keep = self.db.conn.execute(
            "SELECT status FROM entity WHERE id=?", (keep_id,)
        ).fetchone()
        self.assertEqual(drop["status"], "active", "合并失败后旧实体不应已标记 merged")
        self.assertEqual(keep["status"], "active")
        # 别名/提及都没有被挪走（整段事务回滚）
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM entity_alias WHERE entity_id=?", (keep_id,)
            ).fetchone()["c"],
            0,
        )
        self.assertEqual(
            self.db.conn.execute(
                "SELECT entity_id FROM entity_mention WHERE memory_seq=?", (seq,)
            ).fetchone()["entity_id"],
            drop_id,
        )
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM event_log WHERE op='entity.merge'"
            ).fetchone()["c"],
            0,
        )

    def test_entity_merge_still_works(self) -> None:
        keep_id, drop_id, seq = self._seed_merge_entities()
        result = entities.merge(self.db, keep_id, drop_id)
        self.assertEqual(result["aliases"], 1)
        self.assertEqual(result["mentions"], 1)
        self.assertEqual(
            self.db.conn.execute("SELECT status FROM entity WHERE id=?", (drop_id,)).fetchone()["status"],
            "merged",
        )
        self.assertEqual(
            self.db.conn.execute(
                "SELECT entity_id FROM entity_mention WHERE memory_seq=?", (seq,)
            ).fetchone()["entity_id"],
            keep_id,
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM event_log WHERE op='entity.merge'").fetchone()["c"],
            1,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
