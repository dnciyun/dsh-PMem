"""打分：重要度、可信度封顶、时效、RRF、MMR、预算。"""

from __future__ import annotations

import unittest

from pmem.memory import scoring as S


class TestImportance(unittest.TestCase):
    def test_user_explicit_statement(self) -> None:
        value = S.importance(provenance="user_explicit", text="我住在杭州")
        self.assertAlmostEqual(value, 0.595, places=3)

    def test_pinned_is_max(self) -> None:
        self.assertEqual(S.importance(provenance="inferred", text="随便说说", pinned=True), 1.0)

    def test_inferred_is_low(self) -> None:
        value = S.importance(provenance="inferred", text="他可能喜欢猫", novelty=0.2)
        self.assertLess(value, 0.45)

    def test_emphasis_boosts(self) -> None:
        plain = S.importance(provenance="observed", text="用户问了一个问题")
        marked = S.importance(provenance="observed", text="用户问了一个问题 记住 这个很重要")
        self.assertGreater(marked, plain)

    def test_long_text_penalised(self) -> None:
        short = S.importance(provenance="external", text="短" * 30)
        long = S.importance(provenance="external", text="长" * 3000)
        self.assertLess(long, short)

    def test_bounds(self) -> None:
        for prov in ("user_explicit", "observed", "inferred", "unconfirmed"):
            value = S.importance(provenance=prov, text="测试", novelty=1.0)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class TestConfidence(unittest.TestCase):
    def test_single_explicit(self) -> None:
        self.assertAlmostEqual(S.confidence_from_tiers("user_explicit", ["user_explicit"]), 0.918, places=3)

    def test_two_sessions(self) -> None:
        value = S.confidence_from_tiers("user_explicit", ["user_explicit", "user_explicit"])
        self.assertAlmostEqual(value, 0.930, places=3)

    def test_inferred_capped(self) -> None:
        value = S.confidence_from_tiers("inferred", ["inferred"] * 6)
        self.assertLessEqual(value, S.CAP_T4_T5_ONLY)

    def test_observed_only_capped(self) -> None:
        self.assertLessEqual(S.confidence_from_tiers("observed", ["observed"] * 6), S.CAP_T3_ONLY)

    def test_weak_single_session_capped(self) -> None:
        self.assertLessEqual(S.confidence_from_tiers("observed", ["observed"]), S.CAP_SINGLE_SESSION)

    def test_absolute_cap(self) -> None:
        value = S.confidence_from_tiers("user_explicit", ["user_explicit"] * 20)
        self.assertLessEqual(value, S.CAP_ABSOLUTE)

    def test_mixed_strong_and_weak(self) -> None:
        value = S.confidence_from_tiers("user_explicit", ["user_explicit", "inferred"])
        self.assertGreater(value, 0.9)
        self.assertLess(value, S.CAP_ABSOLUTE)

    def test_auto_trait_capped(self) -> None:
        value = S.confidence("user_explicit", [("user_explicit", "s1")], auto_trait=True)
        self.assertLessEqual(value, S.CAP_AUTO_TRAIT)


class TestEvidence(unittest.TestCase):
    def test_same_session_counted_once(self) -> None:
        d = S.evidence_diversity([("user_explicit", "s1"), ("user_explicit", "s1")])
        self.assertAlmostEqual(d, 1.0)

    def test_different_sessions_add_up(self) -> None:
        d = S.evidence_diversity([("user_explicit", "s1"), ("user_explicit", "s2")])
        self.assertAlmostEqual(d, 2.0)

    def test_capped_at_two(self) -> None:
        d = S.evidence_diversity([("user_explicit", f"s{i}") for i in range(10)])
        self.assertAlmostEqual(d, 2.0)


class TestRecency(unittest.TestCase):
    def test_now_is_one(self) -> None:
        self.assertAlmostEqual(S.recency(0, 45), 1.0)

    def test_half_life(self) -> None:
        self.assertAlmostEqual(S.recency(45, 45), 0.5, places=6)

    def test_decays(self) -> None:
        self.assertLess(S.recency(365, 45), S.recency(30, 45))


class TestFusion(unittest.TestCase):
    def test_rrf_prefers_both_channels(self) -> None:
        scores = S.rrf([[1, 2], [2, 3]])
        self.assertGreater(scores[2], scores[1])
        self.assertGreater(scores[2], scores[3])

    def test_rrf_normalize_bounds(self) -> None:
        self.assertAlmostEqual(S.rrf_normalize(2 / 61, 2), 1.0, places=6)
        self.assertLessEqual(S.rrf_normalize(100, 1), 1.0)

    def test_final_score_range(self) -> None:
        value = S.final_score(
            rrf_norm=1.0, importance_v=1.0, confidence_v=1.0, recency_v=1.0, graph_affinity=1.0
        )
        self.assertAlmostEqual(value, 0.55 + 0.15 + 0.12 + 0.10 + 0.08, places=6)

    def test_mmr_drops_near_duplicates(self) -> None:
        # 2 与 1 高度相似（0.92）：0.7*0.88 - 0.3*0.92 = 0.340 < 0.35，于是被 3 反超
        sim = {(1, 2): 0.92}
        picked = S.mmr_select(
            [(1, 0.90), (2, 0.88), (3, 0.50)],
            lambda a, b: sim.get((a, b), sim.get((b, a), 0.0)),
            limit=3,
        )
        self.assertEqual([p[0] for p in picked], [1, 3, 2])


class TestBudget(unittest.TestCase):
    def test_small_items_fit(self) -> None:
        items = [{"text": "短" * 5}, {"text": "短" * 5}]
        kept, used = S.fit_budget(items, 200)
        self.assertEqual(len(kept), 2)
        self.assertGreater(used, 0)

    def test_long_item_truncated(self) -> None:
        items = [{"text": "长" * 400}]
        kept, _ = S.fit_budget(items, 200)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["truncated"])
        self.assertTrue(kept[0]["text"].endswith("…"))

    def test_budget_exhausted_returns_nothing(self) -> None:
        items = [{"text": "长" * 400}]
        kept, _ = S.fit_budget(items, 60)
        self.assertEqual(kept, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
