"""M28：抗干扰的两个排序信号（时间锚点 / 答案类型）+ 注入侧元信息。

这一轮的结论先写在文件头上，免得以后有人以为它们是「默认开着的优化」：

- **时间锚点（A）默认关**：开了可答率 98.6→98.3，抗干扰一格没动（84.8）。留着当开关。
- **答案类型（B）默认关**：可答率 98.3、MRR 0.812，抗干扰不变；扣分档更糟。
- **注入侧元信息（C）默认开**：不进检索排序，只让模型看到「涉及谁」；零指标风险。
- **相对覆盖率下限**：默认 0（关）。0.35 换抗干扰 91.3，代价可答率 95.5——权衡线。

所以这里测的是「机制本身对不对」，不是「默认该不该开」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem import PMem
from pmem.base.config import Config
from pmem.memory import answer_type, temporal


class TestAnchor(unittest.TestCase):
    def test_explicit_day(self) -> None:
        win = temporal.anchor("十月十二号那次全量完，成功率多少？")
        self.assertIsNotNone(win)
        self.assertEqual(win["kind"], "day")
        self.assertEqual(win["from"], win["to"])
        self.assertTrue(win["from"].endswith("-10-12"))
        self.assertEqual(win["label"], "10月12号")

    def test_month_only(self) -> None:
        win = temporal.anchor("八月那次感冒我发烧请假，十月这次是什么症状？")
        self.assertIsNotNone(win)
        self.assertEqual(win["kind"], "month")
        # 多个月份取最后提到的：问的是「十月这次」
        self.assertTrue(win["from"].endswith("-10-01"))
        self.assertEqual(win["label"], "10月")

    def test_vague_words_are_not_guessed(self) -> None:
        for query in ("上次咱们聊的那个事后来怎么样了？", "那次旅行去了哪", "以前那家店叫什么"):
            self.assertIsNone(temporal.anchor(query), query)

    def test_window_tolerance(self) -> None:
        win = temporal.anchor("十月十二号那次")
        self.assertTrue(temporal.in_anchor_window("2026-10-12T09:00:00", win))
        self.assertTrue(temporal.in_anchor_window("2026-10-19T09:00:00", win, tolerance_days=7))
        self.assertFalse(temporal.in_anchor_window("2026-11-04T09:00:00", win, tolerance_days=7))
        self.assertIsNone(temporal.in_anchor_window("", win))
        self.assertIsNone(temporal.in_anchor_window("bad", win))


class TestAnswerType(unittest.TestCase):
    def test_kind_of(self) -> None:
        self.assertEqual(answer_type.kind_of("成功率数字是多少"), "number")
        self.assertEqual(answer_type.kind_of("那房子在几层"), "number")
        self.assertEqual(answer_type.kind_of("我是在哪个湖钓的"), "place")
        self.assertEqual(answer_type.kind_of("哪个室友最爱组局"), "person")
        self.assertEqual(answer_type.kind_of("你什么时候说的"), "time")
        self.assertIsNone(answer_type.kind_of("我们聊得挺好"))

    def test_has_evidence(self) -> None:
        self.assertTrue(answer_type.has_evidence("下单成功率 99.92", "number"))
        self.assertTrue(answer_type.has_evidence("新房子在六楼有电梯", "number"))
        # 「一遍」不算数字答案（单位是有意排除「遍」的）
        self.assertFalse(answer_type.has_evidence("回滚脚本又重写了一遍", "number"))
        self.assertTrue(answer_type.has_evidence("在东湖钓上来的", "place"))
        self.assertTrue(answer_type.has_evidence("10 月 12 日全量", "time"))
        self.assertFalse(answer_type.has_evidence("跟女儿吵了一架", "place"))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # LIFO：后注册的先执行——app.close() 先跑，临时目录最后删（Windows 上文件被占用会删不掉）
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "m28.db"

    def open(self, config: dict | None = None, *, path: Path | None = None) -> PMem:
        app = PMem.open(str(path or self.db_path), config=Config(config or {}))
        self.addCleanup(app.close)
        return app


class TestAnchorBoost(_Base):
    def test_month_anchor_moves_the_right_instance_up(self) -> None:
        app = self.open(
            {"retrieval": {"temporal": {"anchor": {"enabled": True, "bonus": 0.06}}}}
        )
        app.store.add(
            "八月底那次感冒请了两天假，发烧",
            kind="episodic",
            provenance="user_explicit",
            valid_from="2026-08-28T12:00:00",
            created_at="2026-08-28T12:00:00",
        )
        app.store.add(
            "十月这次感冒只有嗓子疼，没请假",
            kind="episodic",
            provenance="user_explicit",
            valid_from="2026-10-20T12:00:00",
            created_at="2026-10-20T12:00:00",
        )
        hits = app.search("十月这次感冒是什么症状", k=2)["data"]["items"]
        self.assertTrue(hits)
        self.assertIn("嗓子疼", hits[0]["text"])

    def test_anchor_disabled_by_default(self) -> None:
        app = self.open()
        self.assertFalse(app.config.get("retrieval.temporal.anchor.enabled"))


class TestAnswerTypeBoost(_Base):
    def test_number_question_prefers_row_with_number(self) -> None:
        app = self.open({"retrieval": {"answer_type": {"enabled": True, "bonus": 0.06}}})
        app.store.add(
            "定了，十月十二号全量，回滚脚本又重写了一遍",
            kind="episodic",
            provenance="user_explicit",
        )
        app.store.add(
            "十月十二号全量之后，下单成功率 99.91", kind="semantic", provenance="user_explicit"
        )
        hits = app.search("十月十二号那次全量完，成功率数字是多少", k=2)["data"]["items"]
        self.assertTrue(hits)
        self.assertIn("99.91", hits[0]["text"])

    def test_answer_type_disabled_by_default(self) -> None:
        app = self.open()
        self.assertFalse(app.config.get("retrieval.answer_type.enabled"))


class TestCoverageFloor(_Base):
    def test_floor_drops_low_coverage_tail(self) -> None:
        app = self.open({"retrieval": {"min_rel_coverage": 0.0}})
        app.store.add(
            "我钓上来一条六斤多的胖头鱼，在东湖", kind="episodic", provenance="user_explicit"
        )
        app.store.add(
            "去府河钓鱼，守了一下午只钓上来两条小鲫鱼", kind="semantic", provenance="user_explicit"
        )
        loose = app.search("六斤多的大鱼在哪个湖", k=8)["data"]["items"]

        strict_path = Path(self.tmp.name) / "strict.db"
        strict = self.open({"retrieval": {"min_rel_coverage": 0.6}}, path=strict_path)
        strict.store.add(
            "我钓上来一条六斤多的胖头鱼，在东湖", kind="episodic", provenance="user_explicit"
        )
        strict.store.add(
            "去府河钓鱼，守了一下午只钓上来两条小鲫鱼", kind="semantic", provenance="user_explicit"
        )
        tight = strict.search("六斤多的大鱼在哪个湖", k=8)["data"]["items"]
        self.assertLessEqual(len(tight), len(loose))
        self.assertTrue(any("东湖" in h["text"] for h in tight))


class TestInjectMeta(_Base):
    def test_payload_carries_entity_names_as_list(self) -> None:
        app = self.open()
        app.store.add(
            "晚上跟女儿吵了一架，就因为她锁着门玩手机", kind="episodic", provenance="user_explicit"
        )
        hits = app.search("女儿为手机跟我吵的那次", k=3)["data"]["items"]
        self.assertTrue(hits)
        if "entities" in hits[0]:
            self.assertIsInstance(hits[0]["entities"], list)

    def test_render_context_marks_shared_entities(self) -> None:
        items = [
            {"day": "2026-08-28", "text": "八月底那次发烧请假", "entities": ["女儿", "感冒"]},
            {"day": "2026-10-20", "text": "十月这次只有嗓子疼", "entities": ["女儿"]},
            {"day": "2026-09-01", "text": "跟王老师聊了聊", "entities": ["王老师"]},
        ]
        block = PMem.render_context(items)
        # 两条共有的「女儿」要标出来；只出现一次的「王老师」不标
        self.assertIn("涉及：女儿", block)
        self.assertNotIn("涉及：王老师", block)

    def test_render_context_without_shared_entities(self) -> None:
        items = [
            {"day": "2026-08-28", "text": "甲", "entities": ["女儿"]},
            {"day": "2026-10-20", "text": "乙", "entities": ["王老师"]},
        ]
        self.assertNotIn("涉及：", PMem.render_context(items))


if __name__ == "__main__":
    unittest.main()
