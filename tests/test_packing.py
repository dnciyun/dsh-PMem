"""预算打包（P2-12，默认关）：按价值装箱决定「注入哪些 / 什么顺序」，不改候选评分。

守五件事：

1. 默认关：与 0.38.0 逐字节等价（同一实例热身后比 SQL 语句序列 + 返回体）；
2. 开：条数 ≤ k、token ≤ budget，且每条 ``score`` 与不开时完全一致（只换装填策略）；
3. must_cover 优先：预算紧时先把「有 number/place/time 字面证据」的条目装进去，
   没传 must_cover 则退化为纯价值排序；
4. 贪心装箱按价值（coverage×类型×新鲜度）选子集、决定注入顺序；
5. 放不下的长条目按 fit_budget 同款规则截断（标 ``truncated``），不因一条太长而整批停。
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory.retrieval import channels
from pmem.memory.retrieval import search as search_mod

from .support import drop_dir, temp_dir

BASE_OVERRIDES: dict = {
    "retrieval": {
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}
NEW_KEYS = frozenset({"retrieval.budget_trace", "retrieval.packing", "retrieval.spans",
                      "retrieval.aggregate", "retrieval.scope.project"})


def _config(**retrieval_overrides: object) -> Config:
    raw = copy.deepcopy(BASE_OVERRIDES)
    raw["retrieval"].update(retrieval_overrides)
    return Config(raw)


class LegacyCfg:
    """0.38.0 的 cfg 视图：新键一律不存在。"""

    def __init__(self, real: object) -> None:
        self._real = real

    def get(self, key: str, default: object = None) -> object:
        if key in NEW_KEYS:
            return default
        return self._real.get(key, default)  # type: ignore[attr-defined]


def _run_counted(store: object, query: str, cfg: object | None = None,
                 filters: dict | None = None) -> tuple[dict, list[str]]:
    real_cfg = store.cfg
    if cfg is not None:
        store.cfg = cfg
    statements: list[str] = []
    conn = store.db.conn
    conn.set_trace_callback(statements.append)
    try:
        result = store.search(query, k=8, record=False, filters=filters)
    finally:
        conn.set_trace_callback(None)
        store.cfg = real_cfg
    return result, statements


def _payload(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "latency_ms"}


class PackingUnitTest(unittest.TestCase):
    """channels.pack_items 是纯函数，先单独钉装箱语义。"""

    def test_selects_by_value_desc(self) -> None:
        items = [{"text": "甲甲甲甲"}, {"text": "乙乙乙乙"}, {"text": "丙丙丙丙"}]
        packed, seqs, used = channels.pack_items(
            items, [1, 2, 3], budget=1000, k=3, values=[0.1, 0.9, 0.5]
        )
        self.assertEqual([item["text"] for item in packed], ["乙乙乙乙", "丙丙丙丙", "甲甲甲甲"])
        self.assertEqual(seqs, [2, 3, 1])
        self.assertGreater(used, 0)

    def test_respects_k_and_budget(self) -> None:
        items = [{"text": "同一句话"} for _ in range(10)]
        packed, _seqs, used = channels.pack_items(
            items, list(range(10)), budget=60, k=2, values=[0.5] * 10
        )
        self.assertLessEqual(len(packed), 2)
        self.assertLessEqual(used, 60)

    def test_must_cover_wins_over_value(self) -> None:
        items = [{"text": "高价值但没有数字"}, {"text": "低价值但有 3 个证据"}]
        packed, seqs, _used = channels.pack_items(
            items,
            [1, 2],
            budget=1000,
            k=2,
            values=[0.9, 0.1],
            covers=[set(), {"number"}],
            must_cover=["number"],
        )
        self.assertEqual(packed[0]["text"], "低价值但有 3 个证据", "must_cover 槽位必须先被覆盖")
        self.assertEqual(seqs, [2, 1])

    def test_without_must_cover_degrades_to_value_order(self) -> None:
        items = [{"text": "第一条"}, {"text": "第二条"}]
        packed, _seqs, _used = channels.pack_items(
            items, [1, 2], budget=1000, k=2, values=[0.2, 0.8]
        )
        self.assertEqual([item["text"] for item in packed], ["第二条", "第一条"])

    def test_long_item_is_truncated_not_dropped(self) -> None:
        text = "长" * 200
        packed, _seqs, used = channels.pack_items(
            [{"text": text, "truncated": False}], [1], budget=130, k=1, values=[1.0]
        )
        self.assertEqual(len(packed), 1)
        self.assertTrue(packed[0]["truncated"])
        self.assertTrue(packed[0]["text"].endswith("…"))
        self.assertLessEqual(used, 130)


class PackingOffTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("packing-off")
        self.app = PMem.open(Path(self._dir) / "pmem.db", config=_config())
        for text in ("用户喜欢喝手冲咖啡，不加糖", "用户常去一家手冲咖啡店", "用户住在杭州"):
            self.app.store.add(text, session_id="s1")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_off_equals_038_and_zero_extra_sql(self) -> None:
        self.app.store.search("手冲咖啡", k=8, record=False)  # 预热
        real, real_sql = _run_counted(self.app.store, "手冲咖啡")
        legacy, legacy_sql = _run_counted(self.app.store, "手冲咖啡", LegacyCfg(self.app.store.cfg))
        self.assertEqual(real_sql, legacy_sql, "关时（相对 0.38.0）多发了 SQL")
        self.assertEqual(
            json.dumps(_payload(real), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy), ensure_ascii=False, sort_keys=True),
        )


class PackingOnTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, **retrieval_overrides: object) -> PMem:
        path = temp_dir("packing-on")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=_config(**retrieval_overrides))
        self._apps.append(app)
        return app

    def test_keeps_scores_and_respects_caps(self) -> None:
        off_app = self._open()
        on_app = self._open(packing={"enabled": True, "type_weight": 0.15})
        for app in (off_app, on_app):
            for text in ("用户喜欢喝手冲咖啡，不加糖", "用户常去一家手冲咖啡店", "用户住在杭州"):
                app.store.add(text, session_id="s1")
        off = off_app.store.search("手冲咖啡", k=2, budget=60, record=False)
        on = search_mod.run_search(on_app.store, "手冲咖啡", k=2, budget=60, record=False)
        self.assertLessEqual(len(on["items"]), 2)
        self.assertLessEqual(on["budget_used"], 60)
        off_scores = {item["text"]: item["score"] for item in off["items"]}
        for item in on["items"]:
            if item["text"] in off_scores:
                self.assertEqual(item["score"], off_scores[item["text"]], "打包不该改评分")

    def test_must_cover_changes_injection_under_tight_budget(self) -> None:
        app = self._open(packing={"enabled": True, "type_weight": 0.15})
        long_text = "用户养了几只猫这件事聊过很多次，每次都聊得又长又细，从名字到习惯都说过一遍"
        number_text = "用户养了 3 只猫"
        app.store.add(long_text, session_id="s1")
        app.store.add(number_text, session_id="s1")
        query = "我养了几只猫"
        off = app.store.search(query, k=8, budget=60, record=False)
        self.assertEqual([item["text"] for item in off["items"]], [long_text], "前置条件：紧预算下只装得下长条目")
        packed = search_mod.run_search(
            app.store, query, k=8, budget=60, record=False, must_cover=["number"]
        )
        self.assertEqual(packed["items"][0]["text"], number_text, "紧预算时应先保证 must_cover 槽位")
        self.assertLessEqual(packed["budget_used"], 60)

    def test_packing_without_must_cover_is_value_order(self) -> None:
        app = self._open(packing={"enabled": True, "type_weight": 0.15})
        for text in ("用户养了 3 只猫", "用户喜欢喝手冲咖啡，不加糖"):
            app.store.add(text, session_id="s1")
        result = search_mod.run_search(app.store, "手冲咖啡 猫", k=8, record=False)
        self.assertTrue(result["items"])
        self.assertNotIn("truncated", {item["text"] for item in result["items"]})

    def test_k_and_budget_are_hard_caps(self) -> None:
        app = self._open(packing={"enabled": True, "type_weight": 0.15})
        for index in range(10):
            app.store.add(f"咖啡记录第 {index} 条：手冲咖啡的味道笔记", session_id="s1")
        result = search_mod.run_search(app.store, "手冲咖啡", k=3, budget=80, record=False)
        self.assertLessEqual(len(result["items"]), 3)
        self.assertLessEqual(result["budget_used"], 80)


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
