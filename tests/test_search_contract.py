"""检索契约（Store 拆分后的回归守卫）：只比文本，不比 ULID。

拆分前 ``store.py`` 2233 行里有约 1500 行是检索；编排搬进 ``memory/retrieval/``
之后，行为漂移最可能的表现就是「同一个库、同一句问话，top-1 变了」。所以这里用
固定种子文本 + 固定配置，按**文本**断言三件事：

1. top-1 是哪一条（词法命中明确的问句）；
2. 多条候选时的先后顺序（覆盖率更高的在前）；
3. 完全不相关的问句必须返回空（不是硬塞一条尾巴）。

比 text 不比 id：id 是 ULID，随插入时刻变；文本才是用户看到的东西。
配置里显式关掉向量通道与图补候选：前者取决于机器上有没有本地模型（CI 没装），
后者会用全库共现给「词法只命中一条」的问句补尾巴；两者都不是本测试要钉的那一层。
"""

from __future__ import annotations

import unittest

from pmem.base.config import Config
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

CONFIG = Config(
    {
        "retrieval": {
            # 关掉向量通道：有没有本地向量模型取决于机器（CI 没装），否则同一个问句
            # 在「本地有模型 / CI 没模型」之间排序会漂。
            "vector": {"enabled": False, "channel": False},
            # 关掉图补候选：图是基于全库共现的派生结构，虽然本身确定，但会让
            # 「词法只命中一条」的问句多出几条尾巴，契约测试只钉词法+融合这一层。
            "graph": {"append": False},
        }
    }
)

HANGZHOU = "用户住在杭州，常去西湖散步"
COFFEE = "用户喜欢喝手冲咖啡，不加糖"
PROJECT = "用户在做一个记忆系统项目，叫 PMem"
RETRIEVAL = "PMem 的检索用覆盖率和 IDF 加权"
REPORT = "下周三要交财务报告"

SEED = [HANGZHOU, COFFEE, PROJECT, RETRIEVAL, REPORT]


class SearchContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("search-contract")
        self.app = PMem.open(self.dir / "pmem.db", config=CONFIG)
        for text in SEED:
            self.app.add(text)

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self.dir)

    def _search(self, query: str, **kwargs: object) -> list[dict]:
        payload = self.app.search(query, record=False, **kwargs)
        self.assertTrue(payload["ok"], payload)
        return payload["data"]["items"]

    def test_top1_is_the_only_match(self) -> None:
        items = self._search("杭州")
        self.assertEqual([item["text"] for item in items], [HANGZHOU])

    def test_top1_prefers_more_covered_memory(self) -> None:
        # 两条都含 PMem；只有 RETRIEVAL 同时含「检索/覆盖率」，覆盖率更高 → 排第一。
        items = self._search("PMem 检索 覆盖率")
        self.assertEqual([item["text"] for item in items], [RETRIEVAL, PROJECT])

    def test_unrelated_query_returns_empty(self) -> None:
        self.assertEqual(self._search("量子纠缠拓扑绝缘体"), [])
        self.assertEqual(self._search("zebra quantum"), [])

    def test_k_limit_is_respected(self) -> None:
        items = self._search("用户", k=2)
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["text"] in SEED for item in items))

    def test_kind_filter_narrows_results(self) -> None:
        items = self._search("下周三", filters={"kind": ["goal"]})
        self.assertEqual(items, [])  # 种子记忆全是 semantic
        goal = "下周三要交财务报告（写进 goal）"
        self.app.add(goal, kind="goal")
        items = self._search("下周三", filters={"kind": ["goal"]})
        self.assertEqual([item["text"] for item in items], [goal])

    def test_clear_winner_survives_mmr_pool(self) -> None:
        """候选数 > mmr_pool(32) 时才会走到最终排序分支：放 40 条弱候选 + 1 条高覆盖。

        这一条专门钉「最终排序/截断」那一层：如果排序被反转或池子被截错，
        弱候选会顶到前面，高覆盖那条就丢了。
        """
        for index in range(40):
            self.app.add(f"项目杂项 {index}：整理资料")
        winner = "项目杂项 关键：青鸟七号 代码 模块 全部对上"
        self.app.add(winner)
        items = self._search("青鸟七号 代码 模块 项目")
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[0]["text"], winner)

    def test_budget_actually_trims(self) -> None:
        """预算裁剪：同一条超长记忆，小预算给的条数必须比大预算少（不是照塞）。"""
        long_text = "长文本记忆：" + "甲乙丙丁戊己庚辛壬癸" * 40
        self.app.add(long_text)
        tight = self._search("长文本记忆", budget=40)
        loose = self._search("长文本记忆", budget=600)
        self.assertTrue(loose, "大预算下应该能召回长文本记忆")
        self.assertLess(len(tight), len(loose), "小预算没有裁掉任何候选")

    def test_mmr_pool_never_caps_below_requested_k(self) -> None:
        """召回契约：mmr_pool 是 MMR 前的性能保护，不得把调用方请求的 k 静默截小。

        2026-11-07 长文档记忆实测：默认 mmr_pool=16 时 k=50 只回 18 条，
        候选池被截到 16 条（gold 召回被这一刀压掉约 1/3）。契约修复后
        pool_cap=max(mmr_pool, k)：k=40 必须能拿到 ≥40 条（预算足够时），
        而 k≤mmr_pool 的默认路径行为不变（由其余用例锁定）。
        """
        for index in range(60):
            self.app.add(f"项目杂项 {index}：整理资料")
        payload = self.app.search("项目杂项 资料", k=40, budget=30000, record=False)
        self.assertTrue(payload["ok"], payload)
        data = payload["data"]
        self.assertGreaterEqual(len(data["items"]), 40,
                                "请求 k=40 只回了 %d 条：mmr_pool 又把候选池截小了" % len(data["items"]))
        info = (data.get("meta") or {}).get("retrieval") or {}
        self.assertGreaterEqual(info.get("pool_cap", 0), 40)
        self.assertEqual(info.get("k"), 40)
        self.assertGreaterEqual(int(info.get("candidates") or 0), 40)


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
