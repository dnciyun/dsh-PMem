"""M26：PMem 的「按字查原话」——精确子串，不是相关性。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem import PMem


class TraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PMem.open(str(Path(self.tmp.name) / "trace.db"))
        self.app.journal_append(
            "为什么要我点头，我不是说了吗这是你的记忆",
            role="user",
            session_id="session-aaa",
        )
        self.app.journal_append(
            "换完了，是真的在跑，不是改完配置就报功",
            role="assistant",
            session_id="session-aaa",
        )
        self.app.journal_append(
            "先把它现在在干什么说清楚", role="user", session_id="session-bbb"
        )

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def test_exact_hit_with_role_and_line(self) -> None:
        result = self.app.trace("为什么要我点头")
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["count"], 1)
        item = data["items"][0]
        self.assertEqual(item["role"], "user")
        self.assertEqual(item["session_id"], "session-aaa")
        self.assertEqual(item["line_no"], 1)
        self.assertIn("这是你的记忆", item["text"])

    def test_line_no_is_per_session(self) -> None:
        data = self.app.trace("先把它现在在干什么")["data"]
        self.assertEqual(data["items"][0]["session_id"], "session-bbb")
        self.assertEqual(data["items"][0]["line_no"], 1)

    def test_too_short_is_not_no(self) -> None:
        data = self.app.trace("好吧")["data"]
        self.assertTrue(data["too_short"])
        self.assertEqual(data["count"], 0)
        self.assertIn("不算", data["note"])

    def test_missing_says_missing(self) -> None:
        data = self.app.trace("这句话肯定没出现过abc")["data"]
        self.assertFalse(data["too_short"])
        self.assertEqual(data["count"], 0)
        self.assertIn("没有", data["note"])
        self.assertIn("PMem", data["scope"])

    def test_limit(self) -> None:
        for i in range(5):
            self.app.journal_append(f"重复的线索句子第{i}遍", role="user", session_id="session-ccc")
        data = self.app.trace("重复的线索句子", limit=2)["data"]
        self.assertEqual(data["count"], 2)


if __name__ == "__main__":
    unittest.main()
