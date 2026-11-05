"""外部基准 A 层：只测匹配/汇总逻辑（不跑真实 714 题，不依赖外部 clone）。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BENCH_PATH = REPO / "bench" / "external" / "chat_memory_bench.py"


def load_bench():
    spec = importlib.util.spec_from_file_location("chat_memory_bench_under_test", BENCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_row(
    question_id: str = "P01-X/d0",
    *,
    memory: float,
    window: float,
    facts: int = 1,
    missed: bool = False,
    tags: list[str] | None = None,
    interval: int = 0,
    memory_best: float = 0.0,
) -> dict:
    per_fact = [
        {"fact_id": f"F{index + 1}", "meaning": "m", "memory_recall": memory >= 1.0, "window_recall": window >= 1.0}
        for index in range(facts)
    ]
    primary = {
        "facts": per_fact,
        "memory_recall": memory,
        "window_recall": window,
        "combined_recall": max(memory, window),
        "wake_recall": 0.0,
        "memory_best_coverage": memory_best,
        "window_best_coverage": 0.0,
    }
    return {
        "id": question_id,
        "persona": "P01",
        "question": "问题",
        "interval_days": interval,
        "content_tags": tags or ["日常"],
        "returned": 3,
        "search_ms": 1.0,
        "wake_tokens": 0,
        "primary": primary,
        "pure": primary,
        "thresholds": {"0.35": primary},
        "journal_items": 0,
        "memory_items": [{"text": "某个检索条目", "score": 0.9, "coverage": memory_best}],
        "window_items": ["用户：某句原话"],
        "missed": missed,
    }


class MatchLogicCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_bench()

    def test_bigrams_ignore_punctuation_and_case(self) -> None:
        self.assertEqual(self.mod.norm("你好，PMem！"), "你好pmem")
        self.assertEqual(self.mod.bigrams("妈妈生日"), {"妈妈", "妈生", "生日"})
        self.assertEqual(self.mod.bigrams(""), set())

    def test_coverage_exact_and_partial(self) -> None:
        grams = self.mod.bigrams("我妈生日是三月八号")
        self.assertEqual(self.mod.coverage(grams, "我妈生日是三月八号。"), 1.0)
        self.assertLess(self.mod.coverage(grams, "三月八号"), 1.0)
        self.assertGreater(self.mod.coverage(grams, "三月八号"), 0.0)
        self.assertEqual(self.mod.coverage(grams, "完全无关"), 0.0)

    def test_question_recall_two_channels_and_threshold(self) -> None:
        facts = [
            {
                "fact_id": "F1",
                "meaning": "我妈生日是三月八号",
                "options": [self.mod.bigrams("我妈生日是三月八号")],
                "strict": self.mod.bigrams("我妈生日是三月八号"),
            }
        ]
        recalled = self.mod.question_recall(
            facts, ["我妈生日是三月八号，每年她都说不用买东西"], [], "", 0.35
        )
        self.assertEqual(recalled["memory_recall"], 1.0)
        self.assertEqual(recalled["window_recall"], 0.0)
        self.assertEqual(recalled["combined_recall"], 1.0)
        self.assertTrue(recalled["facts"][0]["memory_recalled"])

        # 窗口原文里有原话 → window 召回；memory 空
        recalled = self.mod.question_recall(
            facts, [], ["用户：我妈生日是三月八号"], "", 0.35
        )
        self.assertEqual(recalled["window_recall"], 1.0)
        self.assertEqual(recalled["memory_recall"], 0.0)

        # 低于阈值不算召回
        recalled = self.mod.question_recall(facts, ["三月八号"], [], "", 0.5)
        self.assertEqual(recalled["combined_recall"], 0.0)

    def test_primary_uses_individual_sources_and_strict_union_is_lower_bound(self) -> None:
        long_irrelevant = "那天风特别大，我们从南岗一路走到江边，路上说了很多和这个问题无关的话"
        fact = {
            "fact_id": "F1",
            "meaning": "用户住哈尔滨",
            "options": [
                self.mod.bigrams("用户住哈尔滨"),
                self.mod.bigrams(long_irrelevant),
            ],
            "strict": self.mod.bigrams(f"用户住哈尔滨 {long_irrelevant}"),
        }
        memory = ["用户叫秦渡，写悬疑的，住哈尔滨。"]
        primary = self.mod.question_recall([fact], memory, [], "", 0.35)
        strict = self.mod.question_recall([fact], memory, [], "", 0.35, mode="strict")
        self.assertEqual(primary["memory_recall"], 1.0, "meaning 单独被覆盖就该算事实可重建")
        self.assertEqual(strict["memory_recall"], 0.0, "长 quote 拼接口径应把这条判为未召回")


class SummaryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_bench()

    def test_summarize_memory_gain_and_average(self) -> None:
        rows = [
            fake_row(memory=1.0, window=0.0),
            fake_row("P01-Y/d0", memory=0.0, window=1.0),
            fake_row("P01-Z/d0", memory=0.0, window=0.0, facts=2, missed=True),
        ]
        summary = self.mod.summarize_questions(rows)
        self.assertEqual(summary["questions"], 3)
        self.assertEqual(summary["facts"], 4)
        self.assertEqual(summary["memory_recall"], 33.3)
        self.assertEqual(summary["window_recall"], 33.3)
        self.assertEqual(summary["combined_recall"], 66.7)
        self.assertEqual(summary["memory_gain_pp"], 33.3)

    def test_by_threshold_and_pure_lookup(self) -> None:
        row = fake_row(memory=1.0, window=0.0)
        row["thresholds"] = {"0.25": dict(row["primary"]), "0.35": dict(row["primary"]), "0.5": dict(row["primary"])}
        summary = self.mod.summarize_questions([row], threshold_key="0.25")
        self.assertEqual(summary["memory_recall"], 100.0)
        summary = self.mod.summarize_questions([row], threshold_key="pure")
        self.assertEqual(summary["memory_recall"], 100.0)

    def test_group_and_failure_samples(self) -> None:
        rows = [
            fake_row(memory=1.0, window=0.0, tags=["边界"], interval=0),
            fake_row("P01-A/d3", memory=0.0, window=1.0, tags=["边界"], interval=3),
            fake_row("P01-B/d45", memory=0.0, window=0.0, facts=1, missed=True, tags=["欠账"], interval=45, memory_best=0.2),
        ]
        grouped = self.mod.group_summaries(rows, lambda row: row["content_tags"])
        self.assertEqual(grouped["边界"]["questions"], 2)
        self.assertEqual(grouped["欠账"]["questions"], 1)
        failures = self.mod.build_failure_samples(rows)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["id"], "P01-B/d45")
        self.assertEqual(len(failures[0]["evidence_meanings"]), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
