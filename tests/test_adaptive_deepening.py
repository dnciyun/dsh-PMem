"""低覆盖自适应加深 + 查询改写（0.32.0）。

守四件事：
1. 「库里明明有、问法完全不同」的题，低覆盖时用确定性改写再找一次能召回；
2. 关闭 adaptive 时行为与旧版一致（不加深、不多条）；
3. 递归保护（``_deep=True`` 不再二次加深）；
4. 条数与预算都是硬上限：深捞只填空位，不能突破 k / budget。
"""

from __future__ import annotations

import copy
import unittest

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import query as query_mod

from .support import drop_dir, temp_dir

BASE_OVERRIDES: dict = {
    "retrieval": {
        # 关掉向量/图补候选：机器上是否装了本地模型不固定，测试只钉「加深」这一层。
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}

# B 层真实失败形态：问「全名/多大」，记忆写「用户叫…24」，没有一个双字交集。
TARGET = "用户叫沈知夏，知了的知，夏天的夏，24"
NOISE = [
    "用户昨天在河边钓到两条小鲫鱼",
    "下周三要交财务报告",
    "用户在做一个记忆系统项目，叫 PMem",
    "用户喜欢喝手冲咖啡，不加糖",
    "用户周末去爬山，累得腿软",
    "用户养了一只叫豆豆的猫",
    "用户把阳台的绿萝修了一遍",
]
WEAK_QUERY = "填个表 我全名咋写 多大来着"


def _config(*, adaptive: bool = True) -> Config:
    overrides = copy.deepcopy(BASE_OVERRIDES)
    overrides["retrieval"]["adaptive"] = {"enabled": adaptive}
    return Config(overrides)


class _Case(unittest.TestCase):
    config_maker = staticmethod(_config)

    def setUp(self) -> None:
        self.dir = temp_dir("adaptive-deepen")
        self.app = PMem.open(self.dir / "pmem.db", config=self.config_maker())
        self.store = self.app.store
        self.target = self.store.add(TARGET, session_id="s1")
        for text in NOISE:
            self.store.add(text, session_id="s1")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self.dir)

    def _ids(self, result: dict) -> list[str]:
        return [str(item["id"]) for item in result["items"]]

    def _search(self, query: str, **kwargs: object) -> dict:
        return self.store.search(query, record=False, **kwargs)


class AdaptiveEnabledCase(_Case):
    config_maker = staticmethod(_config)

    def test_weak_query_deepens_and_recalls_target(self) -> None:
        data = self._search(WEAK_QUERY, k=8)
        self.assertIn(self.target["id"], self._ids(data), "低覆盖加深没有把「叫沈知夏」那条找回来")
        deep = data.get("deepen")
        self.assertIsNotNone(deep, "低覆盖查询应该带 deepen 元信息")
        self.assertTrue(deep["expanded"])
        self.assertGreaterEqual(deep["added"], 1)
        self.assertIn(deep["trigger"], ("few_items", "low_coverage"))
        # 加深后仍要能回答「这次到底有没有把握」：deep 补进来的条目用深捞覆盖率占位。
        self.assertIn("uncertainty", data)
        self.assertGreater(float(data["uncertainty"]["coverage"]), 0.0)

    def test_no_expansion_still_reports_metadata_but_skips(self) -> None:
        """低覆盖但 query 没有可改写的词：如实标 expanded=False，不跑第二次检索。"""
        data = self._search("量子纠缠拓扑绝缘体", k=8)
        self.assertEqual(data["items"], [])
        deep = data.get("deepen") or {}
        self.assertFalse(deep.get("expanded"))
        self.assertEqual(deep.get("skipped"), "no_expansion")
        self.assertEqual(deep.get("added"), 0)

    def test_budget_and_k_are_hard_caps(self) -> None:
        data = self._search(WEAK_QUERY, k=2, budget=50)
        items = data["items"]
        self.assertLessEqual(len(items), 2, f"深捞突破了 k：{len(items)}")
        self.assertLessEqual(int(data["budget_used"]), 50, data["budget_used"])
        deep = data.get("deepen") or {}
        self.assertLessEqual(int(deep.get("added") or 0), 2)

    def test_deep_guard_prevents_second_pass(self) -> None:
        """``_deep=True`` 是内部递归保护：第二次检索不许再触发第三次。"""
        from pmem.memory.retrieval import search as search_mod

        result = search_mod.run_search(
            self.store, WEAK_QUERY, k=8, record=False, _deep=True
        )
        self.assertNotIn("deepen", result)
        # 首遍词法确实一条都撞不上（这条断言同时说明「加深不是白加」）。
        self.assertEqual(result["items"], [])

    def test_strong_query_does_not_deepen(self) -> None:
        # 强命中语料：同一句话出现在 6 条记忆里，k=3 取满且覆盖率很高。
        for index in range(6):
            self.store.add(f"项目截止日期是 6 月 {index + 1} 日，请提前准备", session_id="s2")
        data = self._search("项目截止日期", k=3)
        self.assertEqual(len(data["items"]), 3)
        self.assertNotIn("deepen", data, "查询不弱时不该做第二次检索")


class AdaptiveEvictsFullPayloadCase(_Case):
    """0.33.0：低覆盖但槽位已满时，允许深捞换掉最弱的尾巴（0.32.0 会直接跳过）。

    小库上覆盖率只有 0/1 两档（IDF 只在语料里出现过的词上算），构造不出真实的
    「低覆盖 + 满槽位」；所以把触发线调到 1 以上（覆盖率恒低于它），专门测接线。
    """

    config_maker = staticmethod(
        lambda: Config(
            {
                "retrieval": {
                    "vector": {"enabled": False, "channel": False},
                    "graph": {"append": False},
                    # 触发线 > 1：所有查询都算「低覆盖」，等价于真实语料里覆盖率不足的情形；
                    # 门槛 > 1：小库上覆盖率恒为 1，不抬高门槛就永远挑不出「更弱」的尾巴；
                    # evict 显式打开（0.33.0 默认关，理由见 config 注释）。
                    "adaptive": {"enabled": True, "coverage": 1.1, "evict": True, "evict_cover": 1.5},
                }
            }
        )
    )

    def setUp(self) -> None:
        super().setUp()
        # 8 条「全名」噪声：和 WEAK_QUERY 只共享一个双字，足以占满 k=8 个槽位。
        for index in range(8):
            self.store.add(f"全名登记表第 {index} 号，别的没写", session_id="s3")

    def test_deepen_replaces_weak_tail(self) -> None:
        data = self._search(WEAK_QUERY, k=8)
        deep = data.get("deepen") or {}
        self.assertEqual(deep.get("trigger"), "low_coverage", deep)
        self.assertGreaterEqual(int(deep.get("added") or 0), 1, deep)
        self.assertGreaterEqual(int(deep.get("evicted") or 0), 1, deep)
        self.assertLessEqual(len(data["items"]), 8)
        ids = self._ids(data)
        self.assertIn(self.target["id"], ids, "深捞应该把「用户叫沈知夏」换进槽位")
        # 头部保护：前三条词法结果不动。
        self.assertTrue(all("全名登记表" in str(item["text"]) for item in data["items"][:3]))

    def test_evict_off_keeps_old_contract(self) -> None:
        """默认配置（evict 关）保持 0.32 契约：槽位满就如实标 no_slot，绝不置换。"""
        overrides = copy.deepcopy(BASE_OVERRIDES)
        overrides["retrieval"]["adaptive"] = {"enabled": True, "coverage": 1.1}
        app = PMem.open(self.dir / "pmem-evict-off.db", config=Config(overrides))
        try:
            store = app.store
            target = store.add(TARGET, session_id="s1")
            for text in NOISE:
                store.add(text, session_id="s1")
            for index in range(8):
                store.add(f"全名登记表第 {index} 号，别的没写", session_id="s3")
            data = store.search(WEAK_QUERY, k=8, record=False)
            deep = data.get("deepen") or {}
            self.assertEqual(deep.get("skipped"), "no_slot", deep)
            self.assertNotIn(target["id"], [str(item["id"]) for item in data["items"]])
        finally:
            app.close()


class AdaptiveDisabledCase(_Case):
    config_maker = staticmethod(lambda: _config(adaptive=False))

    def test_disabled_keeps_old_behaviour(self) -> None:
        data = self._search(WEAK_QUERY, k=8)
        self.assertNotIn(self.target["id"], self._ids(data), "关掉 adaptive 后不应有新结果")
        self.assertNotIn("deepen", data)
        self.assertEqual(data["items"], [], "旧行为是这条查询一条都召回不到")


class ExpandForMemoryContractCase(unittest.TestCase):
    def test_expansion_adds_memory_phrasing(self) -> None:
        expanded = query_mod.expand_for_memory(WEAK_QUERY)
        self.assertIn("用户叫", expanded)
        self.assertIn("名字", expanded)
        self.assertIn("年龄", expanded)
        # 口语修饰必须去掉
        for fluff in ("填个表", "咋写", "来着"):
            self.assertNotIn(fluff, expanded)

    def test_expansion_is_word_level_only(self) -> None:
        expanded = query_mod.expand_for_memory("我全名是什么")
        self.assertTrue(expanded.startswith("我全名"), expanded)
        self.assertIn("名字", expanded)

    def test_no_expansion_returns_original(self) -> None:
        self.assertEqual(query_mod.expand_for_memory("杭州西湖"), "杭州西湖")

    def test_empty_query_returns_original(self) -> None:
        self.assertEqual(query_mod.expand_for_memory(""), "")

    def test_config_defaults_exist(self) -> None:
        cfg = Config()
        self.assertTrue(cfg.get("retrieval.adaptive.enabled"))
        self.assertEqual(cfg.get("retrieval.adaptive.k"), 20)
        self.assertEqual(cfg.get("retrieval.adaptive.coverage"), 0.45)
        self.assertEqual(cfg.get("retrieval.adaptive.max_add"), 8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
