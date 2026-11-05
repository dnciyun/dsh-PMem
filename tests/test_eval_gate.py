"""质量回归门（D）的判定逻辑测试。

只测「比较/阈值/退出码」这几层；308 题的真实评测由 nightly 跑
`python scripts/eval_gate.py`，不在这里重复（那会把单测拖慢 10s+）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE_PATH = REPO / "scripts" / "eval_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("eval_gate_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_result(**overrides: float) -> dict:
    metrics = {
        "answer_rate": 97.9,
        "mrr": 0.813,
        "long_horizon": 96.9,
        "temporal": 100.0,
        "noise_resistance": 84.8,
        "faithfulness": 100.0,
        "hardneg_top3_purity": 57.8,
        "holdout_answer_rate": 98.9,
        "holdout_mrr": 0.79,
    }
    metrics.update(overrides)
    return {
        "metrics": metrics,
        "counts": {"personas": 10, "probes": 308, "hardneg_probes": 277, "holdout_probes": 100},
        "extras": {"retrieval_p50_ms": 7.0, "inject_tokens_mean": 400.0, "hardneg": {}, "holdout": {}},
        "elapsed_s": 0.01,
    }


class CompareMetricsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_gate()
        self.result = fake_result()
        self.baseline = self.mod.build_baseline(self.result, k=8)

    def test_identical_metrics_pass(self) -> None:
        rows = self.mod.compare_metrics(self.result["metrics"], self.baseline)
        self.assertTrue(rows)
        self.assertTrue(all(row["ok"] for row in rows), rows)

    def test_drop_beyond_tolerance_is_regression(self) -> None:
        worse = dict(self.result["metrics"])
        worse["answer_rate"] = self.result["metrics"]["answer_rate"] - 1.0
        rows = {row["metric"]: row for row in self.mod.compare_metrics(worse, self.baseline)}
        self.assertFalse(rows["answer_rate"]["ok"])
        self.assertLess(rows["answer_rate"]["delta"], 0)

    def test_boundary_equal_to_tolerance_passes(self) -> None:
        base = self.result["metrics"]["answer_rate"]
        tolerance = self.baseline["tolerance"]["answer_rate"]
        boundary = dict(self.result["metrics"])
        boundary["answer_rate"] = base - tolerance
        rows = {row["metric"]: row for row in self.mod.compare_metrics(boundary, self.baseline)}
        self.assertTrue(rows["answer_rate"]["ok"], rows["answer_rate"])
        just_below = dict(self.result["metrics"])
        just_below["answer_rate"] = base - tolerance - 0.001
        rows = {row["metric"]: row for row in self.mod.compare_metrics(just_below, self.baseline)}
        self.assertFalse(rows["answer_rate"]["ok"])

    def test_missing_metric_is_regression(self) -> None:
        broken = dict(self.result["metrics"])
        broken.pop("mrr")
        rows = {row["metric"]: row for row in self.mod.compare_metrics(broken, self.baseline)}
        self.assertFalse(rows["mrr"]["ok"])


class CompareCountsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_gate()
        self.baseline = self.mod.build_baseline(fake_result(), k=8)

    def test_same_counts_pass(self) -> None:
        rows = self.mod.compare_counts(
            {"personas": 10, "probes": 308, "hardneg_probes": 277, "holdout_probes": 100},
            self.baseline,
        )
        self.assertTrue(all(row["ok"] for row in rows), rows)

    def test_missing_probes_flagged(self) -> None:
        rows = self.mod.compare_counts(
            {"personas": 10, "probes": 300, "hardneg_probes": 277, "holdout_probes": 100},
            self.baseline,
        )
        failed = [row for row in rows if not row["ok"]]
        self.assertEqual([row["check"] for row in failed], ["count.probes"])


class ExitCodeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_gate()
        self.result = fake_result()

    def _run(self, argv: list[str]) -> int:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            return self.mod.main(argv)

    def test_main_passes_on_matching_baseline(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "eval-baseline.json"
            baseline_path.write_text(
                json.dumps(self.mod.build_baseline(self.result, k=8), ensure_ascii=False),
                encoding="utf-8",
            )
            self.mod.compute_metrics = lambda **kwargs: self.result
            self.assertEqual(self._run(["--baseline", str(baseline_path)]), 0)

    def test_main_returns_nonzero_on_regression(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "eval-baseline.json"
            baseline = self.mod.build_baseline(self.result, k=8)
            baseline["metrics"]["answer_rate"] = 100.0
            baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
            self.mod.compute_metrics = lambda **kwargs: self.result
            self.assertEqual(self._run(["--baseline", str(baseline_path)]), 1)

    def test_update_baseline_writes_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new-baseline.json"
            self.mod.compute_metrics = lambda **kwargs: self.result
            self.assertEqual(self._run(["--update-baseline", "--baseline", str(target)]), 0)
            document = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(document["counts"]["probes"], 308)
            self.assertIn("answer_rate", document["metrics"])
            self.assertIn("tolerance", document)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
