"""证据契约（P2-11/P2-14）：把「要覆盖什么 / 够不够答 / 风险多高」变成字段。

守四件事：

1. 字段形状固定（调用方无条件读 contract/cards 的键）；
2. ``must_cover`` 只由 ``answer_type.kind_of`` + 并列信号推，只标注不扣分；
3. ``answerability`` 三档确定性可复现；``answerability_enabled=False`` 时为 None；
4. 纯函数：不读 DB、不调模型、同输入同输出。
"""

from __future__ import annotations

import unittest

from pmem.memory import contract, query

ITEMS = [
    {"id": "m1", "text": "用户的生日是5月1日"},
    {"id": "m2", "text": "用户养了3只猫"},
    {"id": "m3", "text": "用户住在杭州"},
]
CONTRACT_KEYS = {
    "kind", "must_cover", "must_cover_slots", "answerability", "risk", "reasons", "coverage",
}
CARD_KEYS = {"id", "text", "subject", "type", "span", "via", "covers"}


class ContractCase(unittest.TestCase):
    def test_shape(self) -> None:
        built = contract.build("我养了几只猫", ITEMS)
        self.assertEqual(set(built), {"contract", "cards"})
        self.assertEqual(set(built["contract"]), CONTRACT_KEYS)
        for card in built["cards"]:
            self.assertEqual(set(card), CARD_KEYS)

    def test_kind_follows_plan(self) -> None:
        built = contract.build(
            "随便问问", ITEMS, plan={"kind": "aggregate", "aggregate_terms": ["各"]}
        )
        self.assertEqual(built["contract"]["kind"], "aggregate")
        # 渲染层直接 join 中文标签；机器槽位另给一列。
        self.assertIn("多条", built["contract"]["must_cover"])
        self.assertIn("aggregate", built["contract"]["must_cover_slots"])

    def test_must_cover_number(self) -> None:
        built = contract.build("房子在几层？", [])
        self.assertEqual(built["contract"]["must_cover"], ["数字"])
        self.assertEqual(built["contract"]["must_cover_slots"], ["number"])

    def test_must_cover_place(self) -> None:
        built = contract.build("在哪个城市？", [])
        self.assertEqual(built["contract"]["must_cover"], ["地点"])
        self.assertEqual(built["contract"]["must_cover_slots"], ["place"])

    def test_must_cover_time(self) -> None:
        built = contract.build("他什么时候开始的？", [])
        self.assertEqual(built["contract"]["must_cover"], ["时间"])
        self.assertEqual(built["contract"]["must_cover_slots"], ["time"])

    def test_must_cover_aggregate(self) -> None:
        built = contract.build("我爸妈各喜欢什么？", [])
        self.assertEqual(built["contract"]["must_cover"], ["多条"])
        self.assertEqual(built["contract"]["must_cover_slots"], ["aggregate"])
        self.assertEqual(built["contract"]["kind"], "aggregate")

    def test_answerability_disabled_is_none(self) -> None:
        built = contract.build("我养了几只猫", ITEMS, answerability_enabled=False)
        self.assertIsNone(built["contract"]["answerability"])

    def test_answerability_unanswerable_when_no_items(self) -> None:
        built = contract.build("我养了几只猫", [])
        self.assertEqual(built["contract"]["answerability"], "unanswerable")

    def test_answerability_answerable_when_covered(self) -> None:
        built = contract.build("生日是什么时候", ITEMS)
        self.assertEqual(built["contract"]["answerability"], "answerable")

    def test_answerability_partial_when_unrelated(self) -> None:
        built = contract.build("我的驾照什么时候过期", ITEMS)
        self.assertIn(built["contract"]["answerability"], {"partial", "unanswerable"})

    def test_uncertainty_weak_lowers_answerability(self) -> None:
        strong = contract.build("生日是什么时候", ITEMS)["contract"]
        weak = contract.build("生日是什么时候", ITEMS, uncertainty={"weak": True})["contract"]
        self.assertEqual(strong["answerability"], "answerable")
        self.assertEqual(weak["answerability"], "partial")
        self.assertGreater(weak["risk"], strong["risk"])
        self.assertIn("uncertain", weak["reasons"])

    def test_risk_range_and_monotonic(self) -> None:
        covered = contract.build("我养了几只猫", ITEMS)["contract"]
        bare = contract.build("我养了几只猫", [])["contract"]
        self.assertGreaterEqual(covered["risk"], 0.0)
        self.assertLessEqual(covered["risk"], 1.0)
        self.assertGreater(bare["risk"], covered["risk"])
        self.assertIn("low_coverage", bare["reasons"])

    def test_cards_cover_slots(self) -> None:
        items = [
            {"id": "a", "text": "房子在3层", "via": "tok"},
            {"id": "b", "text": "他说房子挺好"},
        ]
        built = contract.build("房子在几层？", items)
        card = built["cards"][0]
        self.assertEqual(card["id"], "a")
        self.assertEqual(card["type"], "number")
        self.assertEqual(card["covers"], ["number"])
        self.assertEqual(card["via"], "tok")
        # 只标 must_cover 里的槽位：普通查找题不把「有数字」算成覆盖。
        plain = contract.build("我的工作是什么？", [{"id": "c", "text": "工资 3000"}])
        self.assertEqual(plain["cards"][0]["type"], "number")
        self.assertEqual(plain["cards"][0]["covers"], [])

    def test_card_subject_passthrough(self) -> None:
        built = contract.build(
            "我的工作是什么", [{"id": "x", "text": "用户是产品经理", "subject": "沈知夏"}]
        )
        self.assertEqual(built["cards"][0]["subject"], "沈知夏")

    def test_no_question_returns_empty_contract(self) -> None:
        built = contract.build("", ITEMS)
        self.assertIsNone(built["contract"]["answerability"])
        self.assertIsNone(built["contract"]["risk"])
        self.assertEqual(built["contract"]["reasons"], ["no_question"])
        self.assertEqual(len(built["cards"]), len(ITEMS))

    def test_deterministic(self) -> None:
        question = "我爸妈各喜欢什么？"
        self.assertEqual(contract.build(question, ITEMS), contract.build(question, ITEMS))

    def test_plan_reuse(self) -> None:
        plan = query.plan("我爸妈各喜欢什么？")
        built = contract.build("我爸妈各喜欢什么？", ITEMS, plan=plan)
        self.assertEqual(built["contract"]["kind"], plan["kind"])


if __name__ == "__main__":
    unittest.main()
