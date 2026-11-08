"""作答计划（answerplan）：纪律是字段，不是散文。

守六件事：

1. 默认策略产出固定条数的纪律，且 ``render`` 只有一个「作答纪律：」块；
2. 关掉一条（比如 ``assert_when_evidenced=False``）就少一条——**不靠改字符串**；
3. ``suppress`` 里加一个类目，渲染里多一个「不许补」的类目（数据驱动，不新开分支）；
4. 未知 code 不崩（前向兼容：新版本写的配置旧版本读得到）；
5. 证据卡字段固定（id/day/role/kind/text…），渲染器只读卡不看原始 item；
6. CLI / MCP / SDK 三个入口拿到的是同一份计划（同一个 ``render``）。

时间一律 ``clock.set_now`` 固定，别让「今天」漂。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import answerplan
from pmem.memory.answerplan import build_plan, render, rules_for, POLICY_KEYS, SUPPRESS_LABEL

from .support import drop_dir, temp_dir

T0 = datetime(2026, 4, 18, 22, 50, tzinfo=UTC)


class PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("answerplan")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)


class DefaultPolicyTest(PlanCase):
    def test_default_plan_has_expected_shape(self) -> None:
        plan = build_plan(self.store, "上次说的夜班见闻，我还欠你几件？")
        self.assertEqual(plan["date_basis"], "2026-04-18")
        self.assertTrue(plan["time_scoped"])
        for key in ("question", "terms", "policy", "rules", "evidence", "must_not_add"):
            self.assertIn(key, plan)
        self.assertEqual(plan["must_not_add"], list(plan["policy"]["suppress"]))

    def test_render_is_a_single_block(self) -> None:
        # 产品默认关（enabled=False），这里显式开一下，验证渲染本身
        cfg = Config({"answer": {"plan": {**answerplan.DEFAULT_POLICY}}})
        text = render(build_plan(_FakeStore(cfg), "我住哪个城市来着？"))
        self.assertEqual(text.count("作答纪律："), 1)
        self.assertIn("只回答题目问到的点", text)
        self.assertIn("最多 2 句", text)
        self.assertIn("今天是 2026-04-18", text)

    def test_rules_are_data(self) -> None:
        base = rules_for(answerplan.DEFAULT_POLICY)
        codes = [rule["code"] for rule in base]
        self.assertIn("answer_asked_only", codes)
        self.assertIn("keep_it_short", codes)
        # 每条都只有 code/text 两个字段：渲染器不会去读别的键
        for rule in base:
            self.assertEqual(set(rule), {"code", "text"})


class _FakeStore:
    """只带 cfg 的壳：answerplan 只需要 cfg（其余数据它不读）。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg


class PolicyIsDataTest(PlanCase):
    def _with(self, **overrides):
        raw = dict(answerplan.DEFAULT_POLICY)
        raw.update(overrides)
        store = _FakeStore(Config({"answer": {"plan": raw}}))
        return build_plan(store, "那本旧书的作者是谁？")

    def test_turning_one_flag_off_removes_one_rule(self) -> None:
        on = len(self._with()["rules"])
        off = len(self._with(assert_when_evidenced=False)["rules"])
        self.assertEqual(on - off, 1)

    def test_suppress_is_a_category_list(self) -> None:
        plan = self._with(suppress=["unrelated_past", "other_private"])
        text = render(plan)
        self.assertIn("和问题无关的旧事", text)
        self.assertIn("别人的私事", text)
        self.assertNotIn("没有依据的日期", text)

    def test_unknown_category_survives(self) -> None:
        plan = self._with(suppress=["future_category_from_new_version"])
        self.assertIn("future_category_from_new_version", render(plan))

    def test_empty_suppress_drops_the_rule(self) -> None:
        plan = self._with(suppress=[])
        codes = [rule["code"] for rule in plan["rules"]]
        self.assertNotIn("no_unsupported_details", codes)

    def test_max_sentences_is_interpolated(self) -> None:
        plan = self._with(max_sentences=1)
        self.assertIn("最多 1 句", render(plan))

    def test_creative_switch_adds_a_rule(self) -> None:
        codes = [rule["code"] for rule in self._with(allow_creative=True)["rules"]]
        self.assertIn("creative_allowed", codes)

    def test_disabled_drops_the_asked_only_rule(self) -> None:
        codes = [rule["code"] for rule in self._with(enabled=False)["rules"]]
        self.assertNotIn("answer_asked_only", codes)

    def test_date_basis_rule_can_be_turned_off(self) -> None:
        plan = self._with(date_basis_required=False)
        codes = [rule["code"] for rule in plan["rules"]]
        self.assertNotIn("date_with_basis", codes)
        self.assertNotIn("今天是", render(plan))


class EvidenceCardTest(PlanCase):
    def test_cards_are_normalized(self) -> None:
        items = [
            {
                "id": "m1",
                "text": "用户欠三件夜班见闻",
                "day": "2026-04-13",
                "role": "user",
                "kind": "episodic",
                "confidence": 0.9,
                "age_days": 5,
            },
            {"id": "m2", "text": "很早以前的事", "valid_from": "2026-01-02T09:00:00+00:00"},
        ]
        plan = build_plan(self.store, "我还欠你几件？", evidences=items)
        self.assertEqual(plan["evidence_count"], 2)
        first = plan["evidence"][0]
        self.assertEqual(
            set(first), {"id", "day", "role", "kind", "confidence", "age_days", "text"}
        )
        self.assertEqual(first["day"], "2026-04-13")
        # 没给 day 的用 valid_from 前 10 位兜底
        self.assertEqual(plan["evidence"][1]["day"], "2026-01-02")

    def test_render_never_mentions_evidence_details(self) -> None:
        plan = build_plan(
            self.store, "我还欠你几件？", evidences=[{"id": "m1", "text": "用户欠三件夜班见闻"}]
        )
        self.assertNotIn("用户欠三件夜班见闻", render(plan))


class EntryPointsTest(PlanCase):
    def test_sdk_returns_render_and_summary(self) -> None:
        result = self.app.answer_plan("我住哪个城市来着？")
        self.assertTrue(result.get("ok"))
        data = result["data"]
        self.assertIn("作答纪律：", data["render"])
        self.assertIn("计划：", data["summary"])

    def test_sdk_passes_when_through(self) -> None:
        result = self.app.answer_plan("明天几点见面？", when="2026-05-01T09:00:00+08:00")
        self.assertEqual(result["data"]["date_basis"], "2026-05-01")

    def test_policy_keys_cover_defaults(self) -> None:
        for key in POLICY_KEYS:
            self.assertIn(key, answerplan.DEFAULT_POLICY)
        for code in answerplan.DEFAULT_POLICY["suppress"]:
            self.assertIn(code, SUPPRESS_LABEL)

    def test_shipped_default_is_off(self) -> None:
        """0.37.0 全量实测为负（-57，修正口径后 -98，见 docs/roadmap.md §3.6/§3.8），产品默认关；要开必须显式改配置。"""
        self.assertFalse(Config().get("answer.plan.enabled"))
        # 模块默认（没有配置文件时的策略）仍然是「开」，方便调用方显式启用
        self.assertTrue(answerplan.DEFAULT_POLICY["enabled"])


if __name__ == "__main__":
    unittest.main()
