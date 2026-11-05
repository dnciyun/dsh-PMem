"""0.31.0 日期语义：``day_key`` 回填迁移 + I23 不变量。

契约（来自 Lead，不改语义）：
- ``clock.occurred_day(iso, fallback=None)`` = ``day_key(parse_iso(iso))``，解析失败返回 fallback；
- 派生事实（``memory.source_kind='journal'`` 且 ``valid_from`` 非空）的
  ``memory.day_key`` 必须等于 ``occurred_day(valid_from)``；
- 其它 source_kind 的 valid_from 可能表示「事实生效时间」，与 day_key 允许不同，I23 不查；
- 迁移幂等键写在 ``schema_meta.day_key_semantics='occurred-v1'``，有值就短路；
- 有实际改动时追加一条 ``memory.day_key_backfilled`` 事件（payload 带 updated 计数）。
"""

from __future__ import annotations

import json
import unittest

from pmem.base import events
from pmem.base.clock import occurred_day
from pmem.base.db import Database
from pmem.base.ids import ulid
from pmem.ops import invariants

from .support import drop_dir, temp_dir

NOW = "2026-05-04T02:10:00.000Z"
OLD_VALID_FROM = "2026-05-03T22:40:00+08:00"
EXPECTED_DAY = "2026-05-03"


class DayKeySemanticsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("day-key-semantics")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    # -- 造「老库」行（绕过 store，模拟 0.30.x 之前写出来的错误 day_key）------
    def _insert_memory(
        self,
        *,
        source_kind: str = "journal",
        valid_from: str | None = OLD_VALID_FROM,
        day_key: str = "2026-05-04",
        status: str = "active",
        deleted_at: str | None = None,
        text: str = "日期语义迁移测试行",
        memory_id: str | None = None,
    ) -> str:
        memory_id = memory_id or ulid()
        self.db.conn.execute(
            "INSERT INTO memory(id, subject_id, scope_key, kind, text_raw, text_norm, text_seg, "
            "text_sha256, lang, status, importance, confidence, provenance, evidence_count, "
            "evidence_div, source_kind, sensitivity, pinned, created_at, recorded_at, "
            "valid_from, day_key, use_count, decay_score, extra_json, deleted_at) "
            "VALUES(?, 'self', 'project:manual', 'episodic', ?, ?, '', ?, 'zh', ?, 0.5, 0.6, "
            "'observed', 1, 0.0, ?, 'personal', 0, ?, ?, ?, ?, 0, 1.0, '{}', ?)",
            (
                memory_id,
                text,
                text,
                ulid(),
                status,
                source_kind,
                NOW,
                NOW,
                valid_from,
                day_key,
                deleted_at,
            ),
        )
        return memory_id

    def _drop_migration_mark(self) -> None:
        """把 meta 删掉，模拟「0.31.0 之前的老库」。"""
        self.db.conn.execute("DELETE FROM schema_meta WHERE key='day_key_semantics'")

    def _row(self, memory_id: str):
        return self.db.conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()

    # -- 1. 回填：journal 行回到发生日，其它 source_kind 不动 -----------------
    def test_init_backfills_journal_rows_only(self) -> None:
        self._drop_migration_mark()
        journal_id = self._insert_memory(
            source_kind="journal", day_key="2026-05-04", text="日记派生出来的事实"
        )
        session_id = self._insert_memory(
            source_kind="dsh_session", day_key="2026-05-04", text="会话回填出来的事实"
        )

        self.db.init()  # 老库第一次被新版本打开 → 触发回填

        self.assertEqual(self._row(journal_id)["day_key"], EXPECTED_DAY)
        # 其它 source_kind 的 valid_from 可能是「生效时间」，必须原样保留
        self.assertEqual(self._row(session_id)["day_key"], "2026-05-04")

        meta = self.db.conn.execute(
            "SELECT value FROM schema_meta WHERE key='day_key_semantics'"
        ).fetchone()
        self.assertIsNotNone(meta)
        self.assertEqual(meta["value"], "occurred-v1")

        rows = self.db.conn.execute(
            "SELECT payload_json FROM event_log WHERE op='memory.day_key_backfilled'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["updated"], 1)
        self.assertEqual(payload["source_kind"], "journal")
        ok, detail = events.verify_chain(self.db.conn)
        self.assertTrue(ok, detail)

    def test_fresh_db_has_no_migration_event(self) -> None:
        """fresh 库没有可回填的行，不写事件（空 event_log 要能留给事件导入）。"""
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM event_log WHERE op='memory.day_key_backfilled'"
            ).fetchone()["c"],
            0,
        )

    # -- 2. 幂等：第二次 init 不再改、不再写事件 -----------------------------
    def test_migration_is_idempotent(self) -> None:
        self._drop_migration_mark()
        journal_id = self._insert_memory(day_key="2026-05-04")
        self.db.init()
        first = self.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE op='memory.day_key_backfilled'"
        ).fetchone()["c"]
        self.assertEqual(first, 1)

        # 手工再改错一次：meta 已存在 → 迁移应短路，不覆盖（这是「一次性」的定义）
        self.db.conn.execute(
            "UPDATE memory SET day_key='2026-05-04' WHERE id=?", (journal_id,)
        )
        self.db.init()
        self.db.init()
        self.assertEqual(self._row(journal_id)["day_key"], "2026-05-04")
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM event_log WHERE op='memory.day_key_backfilled'"
            ).fetchone()["c"],
            first,
        )
        meta = self.db.conn.execute(
            "SELECT value FROM schema_meta WHERE key='day_key_semantics'"
        ).fetchone()
        self.assertEqual(meta["value"], "occurred-v1")

    # -- 3. I23：只抓 journal 派生行的漂移，改回即绿 --------------------------
    def test_i23_flags_journal_drift(self) -> None:
        journal_id = self._insert_memory(
            source_kind="journal", valid_from=OLD_VALID_FROM, day_key=EXPECTED_DAY
        )
        checks = {item["id"]: item for item in invariants.check_all(self.db)}
        self.assertTrue(checks["I23"]["ok"], checks["I23"])

        self.db.conn.execute(
            "UPDATE memory SET day_key='2026-05-04' WHERE id=?", (journal_id,)
        )
        checks = {item["id"]: item for item in invariants.check_all(self.db)}
        self.assertFalse(checks["I23"]["ok"])
        self.assertIn("1 条派生事实的 day_key 不是发生日", checks["I23"]["detail"])

        self.db.conn.execute(
            "UPDATE memory SET day_key=? WHERE id=?", (EXPECTED_DAY, journal_id)
        )
        checks = {item["id"]: item for item in invariants.check_all(self.db)}
        self.assertTrue(checks["I23"]["ok"], checks["I23"])

    def test_i23_ignores_non_journal_and_deleted_rows(self) -> None:
        # 非 journal：valid_from 可代表「生效时间」，与 day_key 不同不是错
        self._insert_memory(
            source_kind="dsh_session", valid_from=OLD_VALID_FROM, day_key="2026-05-04"
        )
        # 已软删的 journal 行不在检查范围（status<>'deleted'）
        self._insert_memory(
            source_kind="journal",
            valid_from=OLD_VALID_FROM,
            day_key="2026-05-04",
            status="deleted",
            deleted_at=NOW,
        )
        checks = {item["id"]: item for item in invariants.check_all(self.db)}
        self.assertTrue(checks["I23"]["ok"], checks["I23"])

    # -- 4. 脏 valid_from 不能让迁移炸掉 --------------------------------------
    def test_migration_tolerates_unparseable_valid_from(self) -> None:
        self._drop_migration_mark()
        dirty_id = self._insert_memory(
            source_kind="journal", valid_from="not-a-date", day_key="2026-05-04"
        )
        self.db.init()  # 不应抛异常
        self.assertEqual(self._row(dirty_id)["day_key"], "2026-05-04")
        meta = self.db.conn.execute(
            "SELECT value FROM schema_meta WHERE key='day_key_semantics'"
        ).fetchone()
        self.assertIsNotNone(meta)

    # -- 5. 契约对齐：occurred_day 与回填值一致 -------------------------------
    def test_expected_day_matches_occurred_day_contract(self) -> None:
        self.assertEqual(occurred_day(OLD_VALID_FROM), EXPECTED_DAY)
        self.assertIsNone(occurred_day("not-a-date"))
        self.assertEqual(occurred_day("", fallback="2026-01-01"), "2026-01-01")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
