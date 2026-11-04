"""召回：通道、融合、预算裁剪、过滤、副作用。"""

from __future__ import annotations

import unittest
from pathlib import Path

from datetime import UTC, datetime, timedelta

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.errors import ValidationError
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir


class SearchCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("search")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.store = self.app.store
        self.store.add("我住在杭州，常去西湖", session_id="s1")
        self.store.add("用户在做一个记忆系统项目", session_id="s1")
        self.store.add("下周三要交财务报告", kind="goal", session_id="s1")
        self.store.add("用户喜欢喝手冲咖啡", session_id="s1")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)


class TestDormantStillSearchable(SearchCase):
    """休眠只是「不再主动提」，不是「查不到」。"""

    def test_dormant_memory_is_still_recallable(self) -> None:
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        clock.set_now(start)
        try:
            mem = self.store.add("用户养了一只叫豆豆的猫", session_id="s1")
            self.store.update(mem["id"], set_fields={"status": "dormant"})
            result = self.store.search("豆豆")
            ids = [item["id"] for item in result["items"]]
            self.assertIn(mem["id"], ids, "休眠的记忆查不到了，等于「记住了但想不起来」")
        finally:
            clock.reset_now()

    def test_deleted_is_still_hidden(self) -> None:
        mem = self.store.add("这条要被删掉：临时口令 8899", session_id="s1")
        self.store.delete(mem["id"])
        result = self.store.search("临时口令")
        self.assertEqual([i["id"] for i in result["items"]], [])


class TestChannels(SearchCase):
    def test_short_cjk_query(self) -> None:
        result = self.store.search("杭州")
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertIn("杭州", result["items"][0]["text"])
        self.assertIn("tok", result["channels"])  # 双字 token 命中，不必全表 LIKE

    def test_longer_query(self) -> None:
        result = self.store.search("记忆系统")
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_english_query(self) -> None:
        self.store.add("deadline is friday for the report")
        result = self.store.search("deadline")
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_empty_query_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.search("   ")

    def test_no_match_returns_empty(self) -> None:
        result = self.store.search("量子纠缠拓扑绝缘体")
        self.assertEqual(result["items"], [])


class TestRanking(SearchCase):
    def test_scores_descending(self) -> None:
        result = self.store.search("记忆 项目", k=5)
        scores = [item["score"] for item in result["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_k_limits_items(self) -> None:
        result = self.store.search("用户", k=2)
        self.assertLessEqual(len(result["items"]), 2)

    def test_budget_truncates(self) -> None:
        self.store.add("这是一条非常长的记忆，" + "内容" * 400)
        result = self.store.search("非常长的记忆", budget=200)
        self.assertLessEqual(result["budget_used"], 200)
        if result["items"]:
            self.assertTrue(result["items"][0].get("truncated") or len(result["items"][0]["text"]) <= 400)


class TestFilters(SearchCase):
    def test_kind_filter(self) -> None:
        result = self.store.search("下周", filters={"kind": ["goal"]})
        self.assertTrue(all(item["kind"] == "goal" for item in result["items"]))

    def test_status_filter_excludes_candidate(self) -> None:
        self.store.add("用户周三常问财报", provenance="observed")  # candidate
        result = self.store.search("财报")
        self.assertEqual([item for item in result["items"] if "周三" in item["text"]], [])
        loose = self.store.search("财报", filters={"status": ["active", "candidate"]})
        self.assertGreaterEqual(len(loose["items"]), 1)

    def test_pinned_only(self) -> None:
        result = self.store.add("置顶这件事", pinned=True)
        hits = self.store.search("置顶", filters={"pinned_only": True})
        self.assertEqual([item["id"] for item in hits["items"]], [result["id"]])

    def test_since_filter(self) -> None:
        hits = self.store.search("杭州", filters={"since": "2999-01-01"})
        self.assertEqual(hits["items"], [])


class TestSecrecy(SearchCase):
    def test_secret_never_returned_by_default(self) -> None:
        writer = PMem.open(
            Path(self._dir) / "pmem.db", config=Config({"policy": {"allow_secret": True}})
        )
        try:
            writer.store.add("内部密钥是 ABC", sensitivity="secret")
        finally:
            writer.close()
        hits = self.store.search("内部密钥")
        self.assertEqual([i for i in hits["items"] if "密钥" in i["text"]], [])


class TestSideEffects(SearchCase):
    def test_recall_logged(self) -> None:
        before = self.app.db.conn.execute("SELECT COUNT(*) c FROM recall_log").fetchone()["c"]
        self.store.search("杭州")
        after = self.app.db.conn.execute("SELECT COUNT(*) c FROM recall_log").fetchone()["c"]
        self.assertEqual(after - before, 1)

    def test_use_count_incremented(self) -> None:
        hits = self.store.search("杭州")
        memory_id = hits["items"][0]["id"]
        item = self.store.get(memory_id)
        self.assertEqual(item["use_count"], 1)
        self.assertIsNotNone(item["last_used_at"])

    def test_recall_does_not_change_confidence(self) -> None:
        hits = self.store.search("杭州")
        memory_id = hits["items"][0]["id"]
        before = self.store.get(memory_id)["confidence"]
        self.store.search("杭州")
        self.assertEqual(self.store.get(memory_id)["confidence"], before)


class TestModes(SearchCase):
    def test_keyword_mode(self) -> None:
        result = self.store.search("杭州", mode="keyword")
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_vector_mode_degrades_gracefully(self) -> None:
        result = self.store.search("杭州", mode="vector")
        self.assertEqual(result["items"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
