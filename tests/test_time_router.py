"""时间路由（time router）：识别「问时间」的问句，并在检索侧把 k/budget 收窄。

守三件事：

1. ``timecue.is_time_question`` 认得日常中文里的时间说法（日期/相对词/量词），
   又不把普通问题误判进去——误判的代价是普通问题被压成小预算；
2. ``run_search`` 里时间题的大 k 会被压到 cap（默认 8），普通问题不受影响；
3. 收敛只影响**这一次检索**：不改 cfg、不写库，并且 ``meta.time_router`` 如实写明
   有没有被路由、实际生效的 k/budget（不然「为什么只回了 8 条」只能靠猜）。

配置一律显式关掉向量/图补候选：有没有本地向量模型取决于机器，测试只钉路由这一层。
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory.retrieval.timecue import is_time_question

from .support import drop_dir, temp_dir

# 正例：10 种以上不同说法。
TIME_QUESTIONS = [
    "上周咱们都干嘛了",
    "昨天那个会开了多久",
    "2026-11-05 发生了什么",
    "2026年10月有什么安排",
    "10月29日那天我去哪了",
    "29号有什么要交的",
    "3天前我说过什么",
    "两周前那次讨论定了什么",
    "去年冬天我们去哪玩了",
    "下个月有什么计划",
    "什么时候交房租",
    "上次那个 bug 修了吗",
    "半小时前的消息你看了吗",
    "星期几开会",
    "当时是怎么定的",
    "最近怎么样",
    "今年目标完成多少",
    "2019年以来最有成就感的事",
]

# 反例：普通问题，一个时间线索都没有。
PLAIN_QUESTIONS = [
    "我住在杭州",
    "PMem 的检索用的是覆盖率",
    "帮我写一个快速排序函数",
    "咖啡为什么苦",
    "她叫什么名字",
    "怎么提高代码质量",
]


BASE_OVERRIDES: dict = {
    "retrieval": {
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}


def _config(time_router: dict | None = None) -> Config:
    overrides = copy.deepcopy(BASE_OVERRIDES)
    if time_router is not None:
        overrides["retrieval"]["time_router"] = time_router
    return Config(overrides)


class TimecueRecognitionTest(unittest.TestCase):
    """纯识别层：不碰库、不碰配置。"""

    def test_time_phrasings_are_recognized(self) -> None:
        missing = [q for q in TIME_QUESTIONS if not is_time_question(q)]
        self.assertEqual(missing, [], f"这些时间问句没被认出来：{missing}")

    def test_plain_questions_are_not_routed(self) -> None:
        false_hits = [q for q in PLAIN_QUESTIONS if is_time_question(q)]
        self.assertEqual(false_hits, [], f"普通问题被路由了：{false_hits}")

    def test_weak_words_only_count_in_short_questions(self) -> None:
        # 「最近/上次」单看太宽：短问句算，长问句里必须和明确时间词共现才算。
        self.assertTrue(is_time_question("最近怎么样"))
        self.assertTrue(is_time_question("我们上次说的那个方案还作数吗"))
        long_question = "最近我一直在想整个检索链路的参数要不要全部重新标定一遍然后跑一轮离线评估"
        self.assertGreater(len(long_question), 30)
        self.assertFalse(is_time_question(long_question))

    def test_empty_question(self) -> None:
        self.assertFalse(is_time_question(""))
        self.assertFalse(is_time_question("   "))


class RouterSearchTest(unittest.TestCase):
    """检索层：临时库 + 20 条「上周…」记忆，验 k/budget 收敛。"""

    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _seed(self, time_router: dict | None = None) -> PMem:
        path = temp_dir("time-router")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=_config(time_router))
        self._apps.append(app)
        for index in range(20):
            app.add(f"上周我们去了第 {index} 个公园，记下这件事", session_id="s1")
        return app

    def test_time_question_clamps_k_and_budget(self) -> None:
        app = self._seed()
        result = app.store.search("上周都干嘛了", k=40, budget=5000, record=False)
        meta = result["meta"]["time_router"]
        self.assertTrue(meta["routed"], meta)
        self.assertEqual(meta["k"], 8, "时间题的 k 没有被压到 cap_k")
        self.assertEqual(meta["budget"], 600, "时间题的 budget 没有被压到 cap_budget")
        self.assertLessEqual(len(result["items"]), 8, "返回条数超过 cap_k")
        self.assertLessEqual(int(result["budget_used"]), 600, "实际注入量超过 cap_budget")

    def test_plain_question_keeps_large_k(self) -> None:
        app = self._seed()
        result = app.store.search("公园", k=40, budget=5000, record=False)
        meta = result["meta"]["time_router"]
        self.assertFalse(meta["routed"], meta)
        self.assertEqual(meta["k"], 40, "普通问题的 k 被改动了")
        self.assertEqual(meta["budget"], 5000, "普通问题的 budget 被改动了")
        self.assertGreater(len(result["items"]), 8, "普通问题不该被压到 8 条")

    def test_cap_is_overrideable(self) -> None:
        app = self._seed({"cap_k": 3, "cap_budget": 100})
        result = app.store.search("上周都干嘛了", k=40, record=False)
        meta = result["meta"]["time_router"]
        self.assertTrue(meta["routed"])
        self.assertEqual(meta["k"], 3)
        self.assertEqual(meta["budget"], 100)
        self.assertLessEqual(len(result["items"]), 3)

    def test_router_can_be_disabled(self) -> None:
        app = self._seed({"enabled": False})
        result = app.store.search("上周都干嘛了", k=40, record=False)
        meta = result["meta"]["time_router"]
        self.assertFalse(meta["routed"])
        self.assertEqual(meta["k"], 40)
        self.assertGreater(len(result["items"]), 8, "关掉路由后时间题不该被压条数")

    def test_meta_exists_even_when_not_routed(self) -> None:
        app = self._seed()
        result = app.store.search("公园", record=False)
        self.assertIn("meta", result)
        self.assertIn("time_router", result["meta"])


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
