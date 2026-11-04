"""语文压力题库（stress-v1）：46 个可复现的小场景，守「谁的事」与「什么时候成立」。

为什么放进单测：这套题不是为了刷分，是为了把「过期事实」「未生效计划」
「我 vs 我朋友」「小明 vs 小明他爸」「同名小明」这些容易翻车的地方钉死。
每道题一个干净小库，跑一遍不到两秒，适合在每次提交时回归。

注意：题库 runner 会 `clock.set_now` 固定时间，测试结束必须复位，否则会影响别的测试。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_PY = ROOT / "bench" / "stress" / "run.py"

# 题库里必须覆盖的类别（新增类别时一起改这里，防止悄悄少一类）
REQUIRED_CATEGORIES = {
    "expired",
    "future",
    "oldref",
    "selfref",
    "coref",
    "coref_multi",
    "longref",
    "parent_child",
    "kin_long",
    "distractor",
}


def load_runner():
    spec = importlib.util.spec_from_file_location("bench_stress_run", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class TestStressCorpus(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_runner()
        self.doc = self.mod.load_corpus()
        self.cases = self.doc["cases"]

    def test_categories_covered(self) -> None:
        categories = {case["category"] for case in self.cases}
        self.assertTrue(REQUIRED_CATEGORIES <= categories, REQUIRED_CATEGORIES - categories)

    def test_schema(self) -> None:
        ids = set()
        for case in self.cases:
            self.assertNotIn(case["id"], ids, f"重复 id {case['id']}")
            ids.add(case["id"])
            self.assertTrue(case["query"])
            self.assertTrue(case["memories"])
            self.assertTrue(case["expect"]["any"], case["id"])
            for memory in case["memories"]:
                self.assertTrue(memory["text"])

    def test_size(self) -> None:
        self.assertGreaterEqual(len(self.cases), 60)


class TestStressBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_runner()
        self.doc = self.mod.load_corpus()
        self.now = self.doc["now"]

    def tearDown(self) -> None:
        from pmem.base import clock

        clock.reset_now()

    def _run(self, overrides: dict) -> tuple[dict, list[dict]]:
        results = [self.mod.run_case(case, overrides, self.now) for case in self.doc["cases"]]
        return self.mod.summarize(results), results

    def test_full_features_pass(self) -> None:
        summary, results = self._run({})
        self.assertEqual(summary["total"]["hit"], summary["total"]["n"])
        self.assertEqual(summary["total"]["noise"], 0)
        self.assertGreaterEqual(summary["total"]["mrr"], 0.99)
        for row in results:
            self.assertTrue(row["hit"], f"{row['id']} 没命中")

    def test_features_are_doing_something(self) -> None:
        """关掉时效语义与语文范围，MRR 必须掉下来，否则题库没在测东西。"""
        summary, _results = self._run(
            {"retrieval": {"temporal": {"enabled": False}, "scope": {"enabled": False}}}
        )
        self.assertLess(summary["total"]["mrr"], 0.99)
        self.assertGreater(summary["total"]["noise"], 0)

    def test_noise_definition_is_top1(self) -> None:
        """干扰题的口径：同一件事的近邻出现在 2-3 名是合理的，第 1 名才算答错。"""
        _summary, results = self._run({})
        cases = {case["id"]: case for case in self.doc["cases"]}
        for row in results:
            none = cases[row["id"]]["expect"]["none"]
            if not none:
                self.assertFalse(row["noise"], row["id"])
                continue
            top1 = row["items"][0]["text"] if row["items"] else ""
            expected = any(self.mod.contains_any(self.mod.norm(top1), [n]) for n in none)
            self.assertEqual(row["noise"], expected, row["id"])


if __name__ == "__main__":
    unittest.main()
