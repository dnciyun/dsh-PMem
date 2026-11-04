"""1.7.0：原文兜底索引（journal_fts）的写入、清理、补历史与检索。

为什么要有这一层：日记派生事实会被截断（80 字）和改人称（我→用户），
原话里丢掉的细节只能回原文里捞。它只做兜底：派生结果薄弱时才查。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal


class TestJournalRawIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.app = PMem.open(self.db_path, config=Config())
        self.app.db.init()

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _fts_count(self) -> int:
        row = self.app.db.conn.execute("SELECT COUNT(*) AS c FROM journal_fts").fetchone()
        return int(row["c"])

    def _line_count(self) -> int:
        row = self.app.db.conn.execute("SELECT COUNT(*) AS c FROM journal_line").fetchone()
        return int(row["c"])

    def test_append_writes_index(self) -> None:
        result = journal.append(self.app.db, "用户昨天在成都出差，回来以后开始骑车上班")
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("indexed"))
        self.assertEqual(self._fts_count(), 1)
        row = self.app.db.conn.execute("SELECT text_seg FROM journal_fts").fetchone()
        self.assertIn("骑车", row["text_seg"])

    def test_raw_fallback_recovers_line_when_no_facts(self) -> None:
        journal.append(self.app.db, "用户昨天在成都出差，回来以后开始骑车上班")
        result = self.app.search("我后来上班骑什么去", k=3)
        items = result["data"]["items"]
        self.assertTrue(items, "派生层为空时必须回原文兜底")
        self.assertTrue(any(item["kind"] == "journal" for item in items))
        self.assertIn("骑车", "".join(item["text"] for item in items))
        self.assertTrue(any(item.get("source") == "journal" for item in items))

    def test_raw_fallback_respects_switch(self) -> None:
        app = PMem.open(self.db_path, config=Config({"retrieval": {"raw_fallback": False}}))
        try:
            journal.append(app.db, "用户昨天在成都出差，回来以后开始骑车上班")
            items = app.search("我后来上班骑什么去", k=3)["data"]["items"]
            self.assertFalse(any(item["kind"] == "journal" for item in items))
        finally:
            app.close()

    def test_prune_removes_index_rows(self) -> None:
        self.app.db.conn.execute(
            "INSERT INTO episode(id, subject_id, channel, started_at, ended_at, day_key, title, "
            "summary, memory_count, derivation, extra_json) "
            "VALUES('ep-1','self','journal','2026-09-01T10:00:00','2026-09-01T10:00:00',"
            "'2026-09-01','旧的一天','旧的一天',0,'{}','{}')"
        )
        for text in ("用户昨天在成都出差", "用户今天在杭州加班"):
            journal.append(self.app.db, text, ts="2026-09-01T10:00:00")
        self.app.db.conn.execute("UPDATE journal_line SET episode_id='ep-1'")
        before = self._fts_count()
        removed = journal.prune(self.app.db, keep_days=1)
        self.assertEqual(removed, 2)
        self.assertEqual(self._fts_count(), 0)
        self.assertEqual(self._line_count(), 0)
        self.assertGreater(before, 0)

    def test_ensure_index_backfills_history(self) -> None:
        # 模拟 1.7.0 之前建的库：有流水、没有索引
        self.app.db.conn.execute(
            "INSERT INTO journal_line(id, subject_id, session_id, day_key, ts, role, text, created_at) "
            "VALUES('legacy-1','self',NULL,'2026-09-01','2026-09-01T10:00:00','user',"
            "'用户那天在楼下修车摊换了后闸','2026-09-01T10:00:00')"
        )
        self.assertEqual(self._fts_count(), 0)
        filled = journal.ensure_index(self.app.db)
        self.assertEqual(filled, 1)
        self.assertEqual(journal.ensure_index(self.app.db), 0)
        result = self.app.search("修车摊给我换的什么零件", k=5)
        self.assertIn("后闸", "".join(item["text"] for item in result["data"]["items"]))

    def test_mmr_pool_keeps_result_shape(self) -> None:
        app = PMem.open(self.db_path, config=Config({"retrieval": {"mmr_pool": 8}}))
        try:
            for index in range(30):
                app.store.add(f"用户第 {index} 条测试记忆，关于订单服务和缓存", kind="semantic")
            items = app.search("订单服务 缓存", k=8)["data"]["items"]
            self.assertLessEqual(len(items), 8)
            self.assertTrue(items)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
