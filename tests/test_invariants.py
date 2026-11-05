"""不变量体检的**注册与基线**（I1–I22 中 check_all 负责的 19 条）。

逐条特例（I4 扩表 / I21 FTS 漂移 / I22 事件链锚点 / I20 skip key）在
``tests/test_invariants_extra.py``；这里只钉住「有哪些条、干净库全绿、summary 形状」。

为什么项数是 19 而不是 22：I7 由唯一索引执法、I10 由召回 SQL + 契约测试守、
I11 由 ``pmem backup drill`` 与 sleep s8 自检覆盖，三者不在 ``check_all`` 里。
"""

from __future__ import annotations

import unittest

from pmem.base.db import Database
from pmem.ops import invariants

EXPECTED_IDS = [
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "I6",
    "I8",
    "I9",
    "I12",
    "I13",
    "I14",
    "I15",
    "I16",
    "I17",
    "I18",
    "I19",
    "I20",
    "I21",
    "I22",
]


class TestRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init()

    def tearDown(self) -> None:
        self.db.close()

    def test_ids_and_total(self) -> None:
        results = invariants.check_all(self.db)
        self.assertEqual([r["id"] for r in results], EXPECTED_IDS)
        self.assertEqual(len(results), 19)

    def test_result_shape(self) -> None:
        for item in invariants.check_all(self.db):
            self.assertEqual(set(item), {"id", "ok", "detail"})
            self.assertIsInstance(item["ok"], bool)
            self.assertIsInstance(item["detail"], str)

    def test_clean_db_is_green(self) -> None:
        failed = [r for r in invariants.check_all(self.db) if not r["ok"]]
        self.assertEqual(failed, [], failed)

    def test_summary_shape(self) -> None:
        summary = invariants.summary(invariants.check_all(self.db))
        self.assertEqual(summary["total"], 19)
        self.assertEqual(summary["passed"], 19)
        self.assertEqual(summary["failed"], [])
        self.assertTrue(summary["ok"])

    def test_summary_reports_failure(self) -> None:
        # I21：把原话索引搞坏一行 → 必定红，summary 要如实算 failed/passed。
        from pmem.memory import journal

        journal.append(self.db, "一句原话", role="user", session_id="s1")
        self.db.conn.execute("DELETE FROM journal_fts")
        summary = invariants.summary(invariants.check_all(self.db))
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["passed"], summary["total"] - 1)
        self.assertEqual([r["id"] for r in summary["failed"]], ["I21"])


if __name__ == "__main__":
    unittest.main()
