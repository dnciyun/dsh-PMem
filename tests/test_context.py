"""上下文水位（M19）：在爆之前提醒，而不是爆了才说。

守四件事：

1. **估得出来**：中文/英文/block 数组都能估，估出来的数落在合理量级；
2. **分得清三档**：ok / warn / urgent 由配置阈值决定；
3. **跨档才说话**：同一档位反复提醒等于没提醒，必须只喊一次；
4. **状态能跨进程**：插件重启后仍然记得喊过哪一档，否则每次刷新都重复喊。

时间、文件全部用临时目录，不碰真实的 `~/.dsh/pmem/context.json`。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pmem import context
from pmem.base.config import Config

from .support import drop_dir, temp_dir


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("context")
        self.state = str(Path(self._dir) / "context.json")
        self.cfg = Config(
            {
                "context": {
                    "enabled": True,
                    "window_tokens": 100_000,
                    "base_tokens": 0,
                    "warn_at": 0.65,
                    "urgent_at": 0.85,
                }
            }
        )

    def tearDown(self) -> None:
        drop_dir(self._dir)


class EstimateCase(Case):
    def test_estimate_chinese_and_latin(self) -> None:
        self.assertGreaterEqual(context.estimate("中文五个字"), 4)
        self.assertLessEqual(context.estimate("中文五个字"), 8)
        # 英文同样字符数应该明显更便宜
        self.assertLess(context.estimate("hello world this is a test"), context.estimate("这是一句同样长度的中文测试句子"))

    def test_estimate_messages_handles_blocks(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "我们继续做上下文检测"}]},
            {"role": "assistant", "content": "好，先算一下现在用了多少"},
        ]
        total = context.estimate_messages(messages)
        self.assertGreater(total, 10)
        self.assertLess(total, 200)
        self.assertEqual(context.estimate_messages("不是数组"), 0)

    def test_estimate_empty(self) -> None:
        self.assertEqual(context.estimate(""), 0)
        self.assertEqual(context.estimate_messages([]), 0)


class LevelCase(Case):
    def test_levels(self) -> None:
        self.assertEqual(context.status(0, cfg=self.cfg)["level"], "ok")
        self.assertEqual(context.status(70_000, cfg=self.cfg)["level"], "warn")
        self.assertEqual(context.status(90_000, cfg=self.cfg)["level"], "urgent")

    def test_base_overhead_is_added(self) -> None:
        heavy = Config({"context": {"window_tokens": 100_000, "base_tokens": 6_000}})
        found = context.status(0, cfg=heavy)
        self.assertEqual(found["used"], 6_000)
        self.assertEqual(found["level"], "ok")

    def test_window_override(self) -> None:
        found = context.status(30_000, cfg=self.cfg, window=40_000)
        self.assertEqual(found["window"], 40_000)
        self.assertEqual(found["level"], "warn")

    def test_render_silent_when_ok(self) -> None:
        self.assertEqual(context.render(context.status(0, cfg=self.cfg), cfg=self.cfg), "")

    def test_render_speaks_at_warn_and_urgent(self) -> None:
        warn = context.render(context.status(70_000, cfg=self.cfg), cfg=self.cfg)
        urgent = context.render(context.status(90_000, cfg=self.cfg), cfg=self.cfg)
        self.assertIn("收个尾", warn)
        self.assertIn("就该收尾", urgent)
        self.assertIn("开新会话", urgent)


class RecordCase(Case):
    def test_speaks_only_when_crossing(self) -> None:
        first = context.record("s1", 10_000, cfg=self.cfg, path=self.state)
        self.assertFalse(first["speak"])
        self.assertEqual(first["crossed"], "")

        warn = context.record("s1", 70_000, cfg=self.cfg, path=self.state)
        self.assertTrue(warn["speak"])
        self.assertEqual(warn["crossed"], "ok→warn")

        again = context.record("s1", 72_000, cfg=self.cfg, path=self.state)
        self.assertFalse(again["speak"], "同一档位不该再喊")
        self.assertEqual(again["crossed"], "")

        urgent = context.record("s1", 90_000, cfg=self.cfg, path=self.state)
        self.assertTrue(urgent["speak"])
        self.assertEqual(urgent["crossed"], "warn→urgent")

        again_urgent = context.record("s1", 95_000, cfg=self.cfg, path=self.state)
        self.assertFalse(again_urgent["speak"])

    def test_state_survives_reload(self) -> None:
        context.record("s1", 70_000, cfg=self.cfg, path=self.state)
        payload = json.loads(Path(self.state).read_text(encoding="utf-8"))
        self.assertIn("s1", payload["sessions"])
        self.assertEqual(payload["sessions"]["s1"]["level"], "warn")
        # 模拟插件重启：同一个会话已经喊过 warn，不该再喊一遍
        again = context.record("s1", 70_500, cfg=self.cfg, path=self.state)
        self.assertFalse(again["speak"])

    def test_sessions_are_independent(self) -> None:
        context.record("s1", 90_000, cfg=self.cfg, path=self.state)
        fresh = context.record("s2", 90_000, cfg=self.cfg, path=self.state)
        self.assertTrue(fresh["speak"], "新会话第一次到 urgent 必须说")

    def test_report_lists_sessions(self) -> None:
        context.record("s1", 10_000, cfg=self.cfg, path=self.state)
        context.record("s2", 70_000, cfg=self.cfg, path=self.state)
        report = context.report(cfg=self.cfg, path=self.state)
        sessions = {item["session"] for item in report["sessions"]}
        self.assertEqual(sessions, {"s1", "s2"})
        only = context.report(session_id="s2", cfg=self.cfg, path=self.state)
        self.assertEqual([item["session"] for item in only["sessions"]], ["s2"])

    def test_auto_fires_once_at_threshold(self) -> None:
        # 80%（可配）到线不再只是提醒：auto.do 只给一次，之后不再喊「该收尾」。
        found = context.record("s1", 82_000, cfg=self.cfg, path=self.state)
        self.assertTrue(found["auto"]["do"])
        self.assertTrue(found["speak"])
        self.assertIn("交接线", found["text"])
        again = context.record("s1", 90_000, cfg=self.cfg, path=self.state)
        self.assertFalse(again["auto"]["do"])
        self.assertFalse(again["speak"], "已经交接过的会话不该再喊")
        self.assertTrue(again["auto"]["done"])

    def test_window_by_model(self) -> None:
        # 换模型（128k ↔ 1M）时窗口差好几倍：配错整档偏移，所以能按模型名覆盖。
        by_model = Config({"context": {"windows": {"deepseek-chat": 64_000}}})
        self.assertEqual(context.window_tokens(by_model, "DeepSeek-Chat-v3"), 64_000)
        self.assertEqual(context.window_tokens(by_model, "some-other-model"), context.DEFAULT_WINDOW)
        self.assertEqual(context.window_tokens(self.cfg, None), 100_000)

    def test_default_window_is_long_context(self) -> None:
        # 实测这一代 tokenMeter 报过 477k 仍在对话：默认窗口不能还是 128k。
        self.assertGreaterEqual(context.DEFAULT_WINDOW, 1_000_000)

    def test_auto_can_be_disabled(self) -> None:
        off = Config({"context": {"window_tokens": 100_000, "base_tokens": 0, "auto": False}})
        found = context.record("s1", 95_000, cfg=off, path=self.state)
        self.assertFalse(found["auto"]["do"])
        self.assertTrue(found["speak"], "关掉自动交接后仍该提醒")

    def test_auto_bypass_argument(self) -> None:
        found = context.record("s1", 95_000, cfg=self.cfg, path=self.state, auto=False)
        self.assertFalse(found["auto"]["do"])

    def test_real_measurement_wins_over_estimate(self) -> None:
        # 宿主有 tokenMeter 时用真值：估算（含底座）不参与。
        found = context.status(10_000, cfg=self.cfg, actual=90_000)
        self.assertEqual(found["source"], "meter")
        self.assertEqual(found["used"], 90_000)
        self.assertEqual(found["level"], "urgent")
        est = context.status(10_000, cfg=self.cfg)
        self.assertEqual(est["source"], "estimate")

    def test_reports_auto_done(self) -> None:
        context.record("s1", 82_000, cfg=self.cfg, path=self.state)
        report = context.report(cfg=self.cfg, path=self.state)
        self.assertTrue(report["sessions"][0]["auto_done"])
        self.assertEqual(report["sessions"][0]["source"], "estimate")

    def test_missing_or_broken_state_does_not_raise(self) -> None:
        self.assertEqual(context.load_state(str(Path(self._dir) / "nope.json")), {})
        broken = Path(self._dir) / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(context.load_state(str(broken)), {})
        # 坏文件也要能继续记录
        result = context.record("s1", 70_000, cfg=self.cfg, path=str(broken))
        self.assertTrue(result["speak"])


if __name__ == "__main__":
    unittest.main()
