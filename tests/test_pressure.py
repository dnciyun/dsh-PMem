"""超高压测试台的单测：只测「判据本身」对不对，不跑重活。

压测最怕的不是跑得慢，是**判据写错**：断言「下一步：」被占位符满足过，
断言「没崩」在 catch 掉一切之后永远为真。所以这里守的是：

  · 语料平移后 day/date 仍然自洽（不然「第 180 天」是假的）；
  · 百分位不是平均值（p99 不能等于 mean）；
  · 脏输入用例里确实有 NUL / 超长 / 注入，不是凑数；
  · 报告渲染把 FAIL 也写出来（只报 PASS 的表等于没测）；
  · **配置的预算就是上限**——这条压测抓到过真 bug（wake 里 max(480, ...) 架空配置）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from pmem.base.config import Config  # noqa: E402
from pmem.gate.sdk import PMem  # noqa: E402
from pmem.identity import soul as soul_mod  # noqa: E402
from pmem.memory.text import estimate_tokens  # noqa: E402


def _load_pressure_module():
    path = REPO / "bench" / "pressure" / "run.py"
    spec = importlib.util.spec_from_file_location("pressure_run", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# 压测台（bench/pressure）是内部工具，公开仓库里不带（它把对手系统也一起编排进来了）。
# 干净克隆里这些用例跳过，而不是收集失败——跳过要在报告里看得见，所以写明原因。
try:
    run = _load_pressure_module()
except (FileNotFoundError, AssertionError, ImportError):  # noqa: BLE001
    run = None

@unittest.skipUnless(run is not None, "压测台是内部工具，公开仓库不含 bench/pressure")
@unittest.skipUnless(run is not None, "压测台是内部工具（含对手编排），公开仓库不含 bench/pressure")
class CorpusShiftCase(unittest.TestCase):
    def test_shift_keeps_day_and_date_consistent(self):
        day = {"day": 3, "date": "2026-08-03", "sessions": [], "units": []}
        moved = run.shift_day(day, 90)
        self.assertEqual(moved["day"], 93)
        self.assertEqual(moved["date"], "2026-11-01")
        # 原对象不能被改（重放要能重复用同一份语料）
        self.assertEqual(day["day"], 3)
        self.assertEqual(day["date"], "2026-08-03")

    def test_batch_days_offsets_every_day(self):
        days = [{"day": index + 1, "date": f"2026-08-{index + 1:02d}"} for index in range(3)]
        moved = run.batch_days(days, 180)
        self.assertEqual([d["day"] for d in moved], [181, 182, 183])
        self.assertEqual(moved[0]["date"], "2027-01-28")


@unittest.skipUnless(run is not None, "压测台是内部工具（含对手编排），公开仓库不含 bench/pressure")
class LatencyCase(unittest.TestCase):
    def test_percentiles(self):
        values = [float(i) for i in range(1, 101)]
        self.assertEqual(run.pct(values, 50), 50.0)
        self.assertEqual(run.pct(values, 95), 95.0)
        self.assertEqual(run.pct(values, 99), 99.0)
        self.assertEqual(run.pct([], 50), 0.0)
        block = run.latency_block(values)
        self.assertEqual(block["n"], 100)
        self.assertEqual(block["p50"], 50.0)
        # p99 必须比均值差，否则「尾延迟」这项等于没测
        self.assertGreater(block["p99"], block["mean"])

    def test_sample_count_personas(self):
        files = run.persona_files()
        self.assertEqual(len(files), 10, f"期望 10 个人，实际 {files}")
        self.assertEqual(files[0].stem, "p01")


@unittest.skipUnless(run is not None, "压测台是内部工具（含对手编排），公开仓库不含 bench/pressure")
class HostileCaseCase(unittest.TestCase):
    def test_cases_are_actually_hostile(self):
        cases = run.HOSTILE_CASES
        self.assertIn("\u0000", cases["nul"])
        self.assertGreater(len(cases["huge"]), 8000)  # 必须越过 schema 的 8000 字上限
        self.assertIn("忽略之前的所有指令", cases["prompt_inject"])
        self.assertTrue(cases["json_inject"].startswith("{"))
        self.assertIn("<<<END_PMEM_SOUL>>>", cases["marker_break"])
        self.assertGreaterEqual(len(cases["long_token"]), 4096)

    def test_long_token_is_one_word(self):
        token = run.HOSTILE_CASES["long_token"]
        self.assertEqual(len(set(token)), 1)


@unittest.skipUnless(run is not None, "压测台是内部工具（含对手编排），公开仓库不含 bench/pressure")
class UnitGradeCase(unittest.TestCase):
    def test_grades(self):
        self.assertEqual(run.grade_for_unit({"kind": "fact", "importance": 5}), "A")
        self.assertEqual(run.grade_for_unit({"kind": "fact", "importance": 3}), "B")
        self.assertEqual(run.grade_for_unit({"kind": "trivia", "importance": 2}), "C")


@unittest.skipUnless(run is not None, "压测台是内部工具（含对手编排），公开仓库不含 bench/pressure")
class ReportRenderCase(unittest.TestCase):
    def test_report_marks_fail_and_pass(self):
        payload = {
            "started_at": "2026-11-04T12:00:00",
            "env": {"python": "3.13.1", "sqlite": "3.45", "platform": "test"},
            "scenarios": [
                {
                    "name": "volume",
                    "system": "pmem",
                    "ok": True,
                    "metrics": {
                        "rows": {"memory": 2334, "journal_line": 9609},
                        "db_mb": 12.3,
                        "recall": 0.97,
                        "probe_p50_ms": {"p50": 6.1},
                    },
                },
                {
                    "name": "concurrency",
                    "system": "pmem",
                    "ok": False,
                    "metrics": {"written": 780, "expected": 800, "lost": 20, "errors": 3, "chain_ok": True},
                },
                {
                    "name": "runner:fish",
                    "system": "fish",
                    "ok": False,
                    "metrics": {"restart": {"error": "boom"}},
                },
            ],
        }
        text = run.render_report(payload)
        self.assertIn("| volume | PASS |", text)
        self.assertIn("| concurrency | FAIL |", text)
        self.assertIn("| fish |", text)
        self.assertIn("口径与边界", text)


@unittest.skipUnless(run is not None, "压测台是内部工具（含对手编排），公开仓库不含 bench/pressure")
class WakeBudgetCase(unittest.TestCase):
    """配置写 220，就发不超过 220：不允许「保底 480」把配置架空。"""

    def test_identity_budget_is_an_upper_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                {
                    "identity": {
                        "enabled": True,
                        "dir": os.path.join(tmp, "soul"),
                        "budget": 220,
                        "profile_budget": 50,
                    }
                }
            )
            for index in range(6):
                soul_mod.amend(f"第 {index} 条增补：预算写多少就是多少，不许偷偷放大。", cfg=cfg)
            app = PMem.open(os.path.join(tmp, "pmem.db"), config=cfg)
            try:
                app.db.init()
                app.store.add("我们在写记忆系统，测预算上限。", kind="semantic", grade="C")
                card = str(((app.wake() or {}).get("data") or {}).get("text") or "")
                self.assertTrue(card.strip())
                self.assertLessEqual(estimate_tokens(card), 220)
            finally:
                app.close()

    def test_default_budget_still_has_room(self):
        """默认 780 的卡不能被这次收紧削到只剩骨架。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config({"identity": {"enabled": True, "dir": os.path.join(tmp, "soul")}})
            soul_mod.amend("默认预算下，增补和叙事都应该在。", cfg=cfg)
            app = PMem.open(os.path.join(tmp, "pmem.db"), config=cfg)
            try:
                app.db.init()
                app.store.add("一条普通记忆。", kind="semantic", grade="C")
                card = str(((app.wake() or {}).get("data") or {}).get("text") or "")
                self.assertIn("增补", card)
                self.assertLessEqual(estimate_tokens(card), 780)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
