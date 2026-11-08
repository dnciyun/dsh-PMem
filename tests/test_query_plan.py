"""问句计划（P1-7）：只产字段，不写题型模板分支。

守四件事：

1. 四类问法（lookup / aggregate / compare / time）各自能分对；
2. 纯函数：同一句话永远同一份结果，不读 DB / cfg；
3. ``subject_terms`` ≤4，且不含时间词、疑问词、聚合/比较信号字；
4. 空输入不炸，字段形状稳定（调用方可以无条件读四个键）。
"""

from __future__ import annotations

import unittest

from pmem.memory import query

PLAN_KEYS = {"kind", "aggregate_terms", "subject_terms", "confidence"}


class QueryPlanCase(unittest.TestCase):
    def test_lookup(self) -> None:
        self.assertEqual(query.plan("我的工作是什么")["kind"], "lookup")

    def test_aggregate_each(self) -> None:
        plan = query.plan("我爸妈各喜欢什么")
        self.assertEqual(plan["kind"], "aggregate")
        self.assertIn("各", plan["aggregate_terms"])

    def test_aggregate_which(self) -> None:
        plan = query.plan("哪几门课我挂了")
        self.assertEqual(plan["kind"], "aggregate")
        self.assertIn("哪几", plan["aggregate_terms"])

    def test_aggregate_needs_length(self) -> None:
        # 「各」单字 + 句长 <6：不够判聚合，退回 lookup，不硬猜。
        self.assertEqual(query.plan("各")["kind"], "lookup")

    def test_compare(self) -> None:
        plan = query.plan("和去年相比今年工作怎么样")
        self.assertEqual(plan["kind"], "compare")
        self.assertEqual(plan["aggregate_terms"], [])

    def test_time(self) -> None:
        self.assertEqual(query.plan("上周我们聊了啥")["kind"], "time")

    def test_deterministic(self) -> None:
        question = "我爸妈各自都喜欢吃什么"
        self.assertEqual(query.plan(question), query.plan(question))

    def test_shape_stable(self) -> None:
        for question in ("", "   ", "我的工作是什么", "哪几门课我挂了"):
            plan = query.plan(question)
            self.assertEqual(set(plan), PLAN_KEYS)
            self.assertIsInstance(plan["subject_terms"], list)
            self.assertIsInstance(plan["aggregate_terms"], list)
            self.assertGreaterEqual(float(plan["confidence"]), 0.0)
            self.assertLessEqual(float(plan["confidence"]), 1.0)

    def test_empty_is_lookup(self) -> None:
        plan = query.plan("")
        self.assertEqual(plan["kind"], "lookup")
        self.assertEqual(plan["confidence"], 0.0)
        self.assertEqual(plan["subject_terms"], [])

    def test_subject_terms_limits(self) -> None:
        plan = query.plan("我爸妈各自喜欢什么")
        terms = plan["subject_terms"]
        self.assertLessEqual(len(terms), 4)
        self.assertNotIn("什么", terms)
        self.assertNotIn("各自", terms)
        # 时间词不能当主体锚（timecue 词表）。
        self.assertNotIn("上周", query.plan("上周我爸妈喜欢什么")["subject_terms"])

    def test_subject_terms_are_content_words(self) -> None:
        terms = query.plan("我的工作是什么")["subject_terms"]
        self.assertIn("工作", terms)
        # 首尾单字虚词的双字（「我的」「的工」）不是主体锚。
        self.assertNotIn("我的", terms)
        self.assertNotIn("的工", terms)

    def test_aggregate_terms_are_substrings(self) -> None:
        question = "哪几门课我挂了，都考了什么"
        for term in query.plan(question)["aggregate_terms"]:
            self.assertIn(term, question)

    def test_aggregate_terms_bounded(self) -> None:
        plan = query.plan("各各自分别哪几哪些两类几类几个几件")
        self.assertLessEqual(len(plan["aggregate_terms"]), 4)


if __name__ == "__main__":
    unittest.main()
