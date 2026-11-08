"""按需自检的**答案侧**门控（2026-11-07 版）：只数可核验断言，不读检索覆盖度。

为什么换信号：全量 A/B 证明「按 coverage 触发」方向反了（触发题 +4、未触发题 −10）。
所以这一版门控只看答案里有多少具体断言（日期/数字/专名/相对时间）——断言越多，越可能
夹带无据细节，才值得多跑一遍模型核对。默认关；换了信号必须重新 A/B 才能开。
"""

from __future__ import annotations

import unittest
from unittest import mock

from pmem.base.config import Config
from pmem.gate.sdk import PMem, _verifiable_assertions

from .support import drop_dir, temp_dir


class AssertionDensityCase(unittest.TestCase):
    def test_counts_dates_numbers_relative(self) -> None:
        self.assertEqual(_verifiable_assertions("嗯，我记着呢"), 0)
        self.assertEqual(_verifiable_assertions("上周五我们在 2026-05-01 见的"), 2)
        self.assertGreaterEqual(_verifiable_assertions("她61了，从社区超市退下来"), 1)

    def test_counts_capped_input(self) -> None:
        self.assertGreaterEqual(_verifiable_assertions("3 月 8 日、10 月 2 日、12 月 31 日"), 3)


class OnDemandGateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("scondemand")
        self.db = self.dir / "pmem.db"

    def tearDown(self) -> None:
        drop_dir(self.dir)

    def _app(self, overrides: dict) -> PMem:
        return PMem.open(self.db, config=Config(overrides))

    def test_low_density_skips_model_check(self) -> None:
        app = self._app(
            {
                "selfcheck": {
                    "model": "deepseek-chat",
                    "on_demand": {"enabled": True, "min_assertions": 2},
                }
            }
        )
        app.add("用户喜欢手冲咖啡", kind="semantic")
        try:
            with mock.patch("pmem.bench.model.OpenAICompatModel") as fake:
                out = app.selfcheck("嗯，我记着呢", question="我喜欢喝什么")
            fake.assert_not_called()
        finally:
            app.close()
        self.assertTrue(out["ok"], out)
        self.assertIn("跳过模型复核", str(out["data"].get("note") or ""))

    def test_gate_off_still_attempts_model(self) -> None:
        app = self._app({"selfcheck": {"model": "deepseek-chat"}})
        app.add("用户喜欢手冲咖啡", kind="semantic")
        try:
            with mock.patch("pmem.bench.model.OpenAICompatModel") as fake:
                fake.side_effect = RuntimeError("no creds")
                out = app.selfcheck("3 月 8 日喝过手冲", question="我喜欢喝什么")
            fake.assert_called_once()
        finally:
            app.close()
        self.assertTrue(out["ok"], out)
        # 模型没跑起来只写 note，不影响确定性结论
        self.assertIn("没跑起来", str(out["data"].get("note") or ""))


if __name__ == "__main__":
    unittest.main()
