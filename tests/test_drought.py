"""记忆断档（M19 补）：分清楚「这事没记过」和「最近根本没在记」。

为什么值得单独一层：召回空结果时，模型很容易顺口说「没发生过吧」。
但空结果有两种原因——

1. **这件事确实没记**（断档之外）：正常，说「没找到」即可；
2. **最近根本没在记**（断档）：这不是「没找到」，是「那段时间我什么都没留下」，
   说清楚才有用——用户才能去补记忆，或者决定先聊点别的。

判据只用确定性数据：`memory` 表里最近一条活着的记忆写进来的时间，
距现在超过 `retrieval.drought_days`（默认 14 天）就算断档。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base import clock
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("drought")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def _add_days_ago(self, text: str, days: int) -> dict:
        """造一条「days 天前写进来」的记忆。

        不能直接 UPDATE created_at（触发器钉死了原文与写入时间），
        正确做法是**把时钟拨回去再写**——这也是测试里唯一诚实的时间旅行方式。
        """
        clock.set_now(T0 - timedelta(days=days))
        try:
            return self.store.add(text)
        finally:
            clock.set_now(T0)

    def test_empty_recall_on_a_drought_reports_drought(self) -> None:
        self._add_days_ago("用户养了一只布偶猫，叫团团", 30)
        result = self.store.search("量子纠缠的退相干时间是多少", k=3)
        self.assertEqual(result["items"], [])
        self.assertIn("empty", result["uncertainty"]["reasons"])
        self.assertIn("drought", result["uncertainty"]["reasons"])
        self.assertGreaterEqual(result["uncertainty"]["drought_days"], 14)

    def test_recent_memory_does_not_report_drought(self) -> None:
        self._add_days_ago("用户养了一只布偶猫，叫团团", 1)
        result = self.store.search("量子纠缠的退相干时间是多少", k=3)
        self.assertEqual(result["items"], [])
        self.assertIn("empty", result["uncertainty"]["reasons"])
        self.assertNotIn("drought", result["uncertainty"]["reasons"])

    def test_non_empty_result_never_reports_drought(self) -> None:
        self._add_days_ago("用户养了一只布偶猫，叫团团", 30)
        result = self.store.search("我养的那只布偶猫叫什么", k=3)
        self.assertTrue(result["items"])
        self.assertNotIn("drought", result["uncertainty"]["reasons"])

    def test_drought_can_be_disabled(self) -> None:
        from pmem.base.config import Config

        cfg = Config({"retrieval": {"drought_days": 0}, "retrieval.vector": {"enabled": False}})
        app = PMem.open(Path(self._dir) / "db2.db", config=cfg)
        try:
            clock.set_now(T0 - timedelta(days=30))
            try:
                app.store.add("用户养了一只布偶猫，叫团团")
            finally:
                clock.set_now(T0)
            result = app.store.search("量子纠缠的退相干时间是多少", k=3)
            self.assertNotIn("drought", result["uncertainty"]["reasons"])
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
