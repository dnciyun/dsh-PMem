"""渲染层相对日期换算（G3）：口径校准、边界、以及注入渲染的接线。"""

from __future__ import annotations

import unittest

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import timeresolve as tr


class TestResolver(unittest.TestCase):
    def test_week_starts_on_sunday(self) -> None:
        """靶场校准：2026-05-03（周日）说的「上周五」= 05-01，不是 04-24。"""
        self.assertEqual(tr.relative_dates("上周五下班拿走", "2026-05-03"), [("上周五", "2026-05-01")])

    def test_next_week_weekday(self) -> None:
        self.assertEqual(tr.relative_dates("下周三之前寄出", "2026-06-22"), [("下周三", "2026-07-01")])

    def test_relative_days(self) -> None:
        self.assertEqual(tr.relative_dates("昨天在江北", "2026-05-03"), [("昨天", "2026-05-02")])
        self.assertEqual(tr.relative_dates("前天搬的", "2026-05-27"), [("前天", "2026-05-25")])

    def test_offsets_with_chinese_numbers(self) -> None:
        pairs = dict(tr.relative_dates("三天前量了尺寸，两周后报价", "2026-05-28"))
        self.assertEqual(pairs["三天前"], "2026-05-25")
        self.assertEqual(pairs["两周后"], "2026-06-11")
        pairs = dict(tr.relative_dates("半个月前订的，十二天后到", "2026-05-28"))
        self.assertEqual(pairs["半个月前"], "2026-05-13")
        self.assertEqual(pairs["十二天后"], "2026-06-09")

    def test_bare_weekday_is_ambiguous_and_skipped(self) -> None:
        self.assertEqual(tr.relative_dates("周三去", "2026-05-03"), [])
        self.assertEqual(tr.relative_dates("上次说的", "2026-05-03"), [])

    def test_bad_base_returns_empty(self) -> None:
        self.assertEqual(tr.relative_dates("昨天", ""), [])
        self.assertEqual(tr.relative_dates("昨天", "不是日期"), [])

    def test_annotate_format(self) -> None:
        self.assertEqual(tr.annotate("上周五下班拿走", "2026-05-03"), "〔「上周五」=2026-05-01〕")
        self.assertEqual(tr.annotate("周三去", "2026-05-03"), "")


class TestRenderContext(unittest.TestCase):
    ITEM = {
        "id": "m1",
        "text": "叶楠上周五把相机拿走了",
        "valid_from": "2026-05-03T10:07:13+08:00",
        "day": "2026-05-03",
        "kind": "episodic",
        "status": "active",
    }

    def test_default_off(self) -> None:
        self.assertFalse(Config().get("retrieval.time_router.relative_dates"))
        block = PMem.render_context([self.ITEM])
        self.assertNotIn("「上周五」", block)

    def test_on_adds_annotation(self) -> None:
        block = PMem.render_context([self.ITEM], relative_dates=True)
        self.assertIn("「上周五」=2026-05-01", block)
        # 数据块形状不变：仍是定界数据、仍以日期开头
        self.assertTrue(block.startswith("<<<PMEM_MEMORY_DATA"))
        self.assertTrue(block.endswith("<<<END_PMEM_MEMORY_DATA>>>"))

    def test_on_without_relative_words_is_unchanged(self) -> None:
        item = dict(self.ITEM, text="用户在做一个记忆系统项目")
        self.assertEqual(
            PMem.render_context([item]),
            PMem.render_context([item], relative_dates=True),
        )

    def test_on_keeps_budget_contract(self) -> None:
        items = [dict(self.ITEM, id=f"m{i}") for i in range(20)]
        block = PMem.render_context(items, budget=200, relative_dates=True)
        self.assertLessEqual(len(block), 2000)


if __name__ == "__main__":
    unittest.main()
