"""词面第二路检索（G2）：配置开关、补候选不重排首遍、默认关闭时逐位不变。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

SECOND = {"retrieval": {"second_query": {"enabled": True}}}
QUERY = "你还记得上次说的那个财务报告什么时候交吗"


class SecondQueryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("second-query")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.store = self.app.store
        self.store.add("用户下周三要交财务报告，还没写完", session_id="s1")
        self.store.add("上周去西湖边走了走，风很大", session_id="s1")
        self.store.add("阿岚说下个月想去看海边的钟楼", session_id="s1")
        self.store.add("财务报告要交给苏编，别拖到月底", session_id="s1")
        self.store.add("用户在做一个记忆系统项目", session_id="s1")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _search(self, config: Config, query: str = QUERY) -> dict:
        app = PMem.open(Path(self._dir) / "pmem.db", config=config)
        try:
            return app.store.search(query)
        finally:
            app.close()

    def test_default_off(self) -> None:
        cfg = Config()
        self.assertFalse(cfg.get("retrieval.second_query.enabled"))
        self.assertEqual(cfg.get("retrieval.second_query.max_add"), 6)
        self.assertEqual(cfg.get("retrieval.second_query.head"), 2)
        self.assertEqual(cfg.get("retrieval.second_query.max_evict"), 3)

    def test_off_does_not_run_second_pass(self) -> None:
        result = self._search(Config())
        self.assertNotIn("second_query", result)

    def test_on_runs_second_pass(self) -> None:
        result = self._search(Config(SECOND))
        self.assertIn("second_query", result)
        self.assertGreaterEqual(result["second_query"]["added"], 0)

    def test_on_keeps_first_pass_order_and_contract(self) -> None:
        """补候选不许重排首遍：头部条目顺序必须一致，条数不许超过 k。"""
        off = self._search(Config(), "财务报告要交给谁")
        on = self._search(Config(SECOND), "财务报告要交给谁")
        off_ids = [item["id"] for item in off["items"]]
        on_ids = [item["id"] for item in on["items"]]
        self.assertEqual(on_ids[: min(2, len(off_ids))], off_ids[: min(2, len(off_ids))])
        self.assertLessEqual(len(on_ids), 8)
        self.assertTrue(on_ids)

    def test_zero_max_add_is_identical_to_off(self) -> None:
        off = self._search(Config())
        zero = self._search(Config({"retrieval": {"second_query": {"enabled": True, "max_add": 0}}}))
        self.assertEqual([item["id"] for item in off["items"]], [item["id"] for item in zero["items"]])

    def test_on_does_not_exceed_k(self) -> None:
        app = PMem.open(Path(self._dir) / "pmem.db", config=Config(SECOND))
        try:
            result = app.store.search(QUERY, k=2)
            self.assertLessEqual(len(result["items"]), 2)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
