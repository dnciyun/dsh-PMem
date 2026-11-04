"""stats.recent 与 journal 索引重建（0.27.4）。

两个问题的落点：
- 「最近记了什么」不能靠相关性检索去猜——stats 里直接给按写入顺序的最后几条；
- FTS5 报 malformed inverted index 时，ensure_index 只补缺的不修坏的，需要一个确定性重建入口。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem import PMem
from pmem.memory import journal


class TestStatsRecent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = PMem.open(Path(self.tmp.name) / "recent.db")
        self.addCleanup(self.app.close)

    def test_recent_is_newest_first(self) -> None:
        first = self.app.store.add("第一条：猫叫年糕", provenance="user_explicit")["id"]
        second = self.app.store.add("第二条：爸爸最帅", provenance="user_explicit")["id"]
        recent = self.app.stats()["data"]["recent"]
        self.assertEqual([item["id"] for item in recent[:2]], [second, first])

    def test_recent_skips_deleted(self) -> None:
        keep = self.app.store.add("留着的这条", provenance="user_explicit")["id"]
        gone = self.app.store.add("要删掉的这条", provenance="user_explicit")["id"]
        self.app.store.delete(gone)
        ids = [item["id"] for item in self.app.stats()["data"]["recent"]]
        self.assertIn(keep, ids)
        self.assertNotIn(gone, ids)


class TestRebuildIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = PMem.open(Path(self.tmp.name) / "idx.db")
        self.addCleanup(self.app.close)
        for i in range(3):
            journal.append(self.app.db, f"第{i}句原话：跨端记忆测试", role="user", session_id="s1")

    def test_dry_run_counts_without_touching(self) -> None:
        before = self.app.db.conn.execute("SELECT COUNT(*) c FROM journal_fts").fetchone()["c"]
        stats = journal.rebuild_index(self.app.db, dry_run=True)
        after = self.app.db.conn.execute("SELECT COUNT(*) c FROM journal_fts").fetchone()["c"]
        self.assertEqual(stats["lines"], 3)
        self.assertEqual(before, after)

    def test_rebuild_restores_missing_rows(self) -> None:
        # 模拟索引坏掉：删掉一行
        self.app.db.conn.execute("DELETE FROM journal_fts WHERE rowid=1")
        self.app.db.conn.commit()
        stats = journal.rebuild_index(self.app.db)
        self.assertEqual(stats["indexed"], 3)
        count = self.app.db.conn.execute("SELECT COUNT(*) c FROM journal_fts").fetchone()["c"]
        self.assertEqual(count, 3)

    def test_rebuild_keeps_index_usable(self) -> None:
        journal.rebuild_index(self.app.db)
        hit = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM journal_fts WHERE journal_fts MATCH '跨端'"
        ).fetchone()["c"]
        self.assertGreaterEqual(hit, 1)


if __name__ == "__main__":
    unittest.main()
