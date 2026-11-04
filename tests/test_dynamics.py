"""M9.65：记忆动力学（ACT-R base-level → 浮现概率）。"""

from __future__ import annotations

import math
import unittest
from datetime import timedelta

from pmem.base.clock import now, to_iso
from pmem.memory import dynamics


def days_ago(days: float) -> str:
    return to_iso(now() - timedelta(days=days))


class TestActivation(unittest.TestCase):
    def test_no_access_is_negative_infinity(self) -> None:
        self.assertEqual(dynamics.activation([]), float("-inf"))
        self.assertEqual(dynamics.retrievability([]), 0.0)

    def test_older_is_weaker(self) -> None:
        fresh = dynamics.activation([days_ago(1)])
        stale = dynamics.activation([days_ago(60)])
        self.assertGreater(fresh, stale)

    def test_frequency_raises_activation(self) -> None:
        once = dynamics.activation([days_ago(10)])
        often = dynamics.activation([days_ago(10), days_ago(7), days_ago(4), days_ago(1)])
        self.assertGreater(often, once)

    def test_one_day_old_is_about_092(self) -> None:
        r = dynamics.retrievability([days_ago(1)])
        self.assertAlmostEqual(r, 0.92, delta=0.02)

    def test_one_year_is_about_008(self) -> None:
        r = dynamics.retrievability([days_ago(365)])
        self.assertAlmostEqual(r, 0.08, delta=0.03)

    def test_retrievability_is_bounded(self) -> None:
        for days in (0, 0.001, 1, 7, 30, 365, 3650):
            r = dynamics.retrievability([days_ago(days)])
            self.assertGreater(r, 0.0)
            self.assertLessEqual(r, 1.0)

    def test_bad_timestamp_is_skipped(self) -> None:
        self.assertEqual(dynamics.activation(["not-a-date"]), float("-inf"))

    def test_squash_saturates(self) -> None:
        self.assertEqual(dynamics.squash(1e9), 1.0)
        self.assertEqual(dynamics.squash(-1e9), 0.0)
        self.assertEqual(dynamics.squash(float("-inf")), 0.0)


class TestScore(unittest.TestCase):
    def test_grade_weight(self) -> None:
        self.assertEqual(dynamics.grade_weight("S"), 1.0)
        self.assertEqual(dynamics.grade_weight("d"), 0.0)
        self.assertEqual(dynamics.grade_weight(None), dynamics.NO_GRADE_WEIGHT)

    def test_d_grade_never_surfaces(self) -> None:
        self.assertEqual(dynamics.score("D", [days_ago(0)], floor=dynamics.CUE_FLOOR), 0.0)

    def test_cue_floor_keeps_old_memory_above_threshold(self) -> None:
        # 一年没碰的 A 级：自然概率很低，但"你刚提到它"就该提。
        raw = dynamics.score("A", [days_ago(365)])
        floored = dynamics.score("A", [days_ago(365)], floor=dynamics.CUE_FLOOR)
        self.assertLess(raw, dynamics.TURN_THRESHOLD)
        self.assertGreaterEqual(floored, dynamics.TURN_THRESHOLD)
        self.assertAlmostEqual(floored, dynamics.CUE_FLOOR * 0.9, delta=1e-6)

    def test_boost_multiplies(self) -> None:
        base = dynamics.score("B", [days_ago(0)])
        boosted = dynamics.score("B", [days_ago(0)], boost=0.5)
        self.assertAlmostEqual(boosted, base * 0.5, delta=1e-6)

    def test_describe_explains(self) -> None:
        info = dynamics.describe("A", [days_ago(7)])
        self.assertIn("b", info)
        self.assertIn("r", info)
        self.assertEqual(info["accesses"], 1)
        self.assertAlmostEqual(info["p"], info["r"] * 0.9, delta=1e-3)

    def test_describe_empty_is_safe(self) -> None:
        info = dynamics.describe("A", [])
        self.assertIsNone(info["b"])
        self.assertEqual(info["p"], 0.0)

    def test_due_window(self) -> None:
        self.assertTrue(dynamics.is_due(0.3))
        self.assertFalse(dynamics.is_due(0.9))
        self.assertFalse(dynamics.is_due(0.01))


class TestCurveShape(unittest.TestCase):
    """三条性质：常想起更牢、久不想起更淡、再想起能回来。"""

    def test_relearning_after_silence(self) -> None:
        stale = dynamics.retrievability([days_ago(60)])
        relearned = dynamics.retrievability([days_ago(60), days_ago(0)])
        self.assertGreater(relearned, stale)
        self.assertGreater(relearned, 0.9)

    def test_monotone_in_time(self) -> None:
        marks = [days_ago(30)]
        values = [
            dynamics.retrievability(marks, now=to_iso(now() + timedelta(days=offset)))
            for offset in (0, 5, 30, 120)
        ]
        for earlier, later in zip(values, values[1:]):
            self.assertGreaterEqual(earlier, later)

    def test_math_is_finite(self) -> None:
        self.assertFalse(math.isnan(dynamics.retrievability([days_ago(0), days_ago(0)])))


if __name__ == "__main__":
    unittest.main()
