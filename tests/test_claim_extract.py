"""claim 抽取：host 任务书 + 校验入库 + 规则降级（全部默认关，显式调用才写库）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import claim, claim_extract, journal

from .support import drop_dir, temp_dir

ENABLED = Config(
    {
        "claim": {
            "enabled": True,
            "version": {"enabled": False},
            "extract": {
                "min_confidence": 0.6,
                "candidate_below": 0.8,
                "kinds": ["fact", "preference", "date", "third_party", "boundary"],
            },
        }
    }
)
DISABLED = Config({"claim": {"enabled": False}})


class ClaimExtractCase(unittest.TestCase):
    def setUp(self) -> None:
        clock.set_now("2026-11-08T12:00:00+08:00")
        self._dirs = []
        self._apps: list[PMem] = []
        self._dir = temp_dir("claim-extract")
        self._dirs.append(self._dir)
        self.app = PMem.open(self._dir / "pmem.db", config=ENABLED)
        self._apps.append(self.app)
        self.db = self.app.store.db
        self.line1 = journal.append(
            self.db, "我妈叫王秀兰，她住在杭州。", role="user",
            ts="2026-11-08T10:00:00+08:00",
        )
        self.line2 = journal.append(
            self.db, "我不吃香菜，明天要去医院复查。", role="user",
            ts="2026-11-08T10:05:00+08:00",
        )
        self.line3 = journal.append(
            self.db, "用户说他不吃香菜", role="assistant",
            ts="2026-11-08T10:06:00+08:00",
        )
        self.day = "2026-11-08"

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _claim(self, quote: str, **kwargs) -> dict:
        payload = {
            "kind": "preference",
            "predicate": "不吃",
            "object": "香菜",
            "polarity": 0,
            "status": "happened",
            "mentioned_at": "2026-11-08T10:05:00+08:00",
            "confidence": 0.9,
            "evidence_quote": quote,
        }
        payload.update(kwargs)
        return payload

    def _apply(self, claims: list[dict], config: Config = ENABLED) -> dict:
        return claim_extract.apply(
            self.db, day_key=self.day, payload={"claims": claims}, config=config
        )


class PrepTest(ClaimExtractCase):
    def test_prep_builds_material_and_executable_task(self) -> None:
        result = claim_extract.prep(self.db, day_key=self.day, config=ENABLED)
        self.assertTrue(result["enabled"])
        self.assertEqual(len(result["material"]), 3)
        self.assertTrue(result["task"])
        self.assertIn('"claims"', result["task"])
        for field in result["fields"]:
            self.assertIn(field, result["task"])
        # 示例必须能直接当 JSON 用，字段名与 claim 表一致。
        self.assertEqual(set(result["example"][0]) >= {"kind", "predicate", "object", "polarity", "evidence_quote"}, True)

    def test_prep_includes_subject_and_third_party_examples(self) -> None:
        result = claim_extract.prep(self.db, day_key=self.day, config=ENABLED)
        self.assertIn("subject", result["fields"])
        self.assertIn("subject", result["task"])
        third = [item for item in result["example"] if item["kind"] == "third_party"]
        self.assertGreaterEqual(len(third), 2)
        self.assertTrue(all(item.get("subject") for item in third))

    def test_prep_is_off_by_default(self) -> None:
        result = claim_extract.prep(self.db, day_key=self.day)  # 默认 claim.enabled=false
        self.assertFalse(result["enabled"])
        self.assertEqual(result["material"], [])
        self.assertEqual(result["task"], "")

    def test_prep_truncates_to_recent_chars_when_configured(self) -> None:
        result = claim_extract.prep(
            self.db, day_key=self.day, config=ENABLED, max_source_chars=10
        )
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["source_chars"], 10)
        self.assertEqual(len(result["material"]), 1)


class ApplyTest(ClaimExtractCase):
    def test_valid_claims_are_inserted(self) -> None:
        result = self._apply(
            [
                self._claim("我不吃香菜"),
                {
                    "kind": "third_party",
                    "predicate": "妈妈",
                    "object": "王秀兰",
                    "polarity": 1,
                    "status": "happened",
                    "mentioned_at": "2026-11-08T10:00:00+08:00",
                    "confidence": 0.85,
                    "evidence_quote": "我妈叫王秀兰",
                },
            ]
        )
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["dropped"], 0)
        rows = claim.list_claims(self.db, subject_id="self")
        self.assertEqual(len(rows), 2)

    def test_fabricated_quote_is_dropped(self) -> None:
        result = self._apply([self._claim("原话里没有这句")])
        self.assertEqual(result["inserted"], 0)
        self.assertGreaterEqual(result["dropped"], 1)
        self.assertIn("dropped_evidence", result["reasons"])
        self.assertEqual(claim.list_claims(self.db), [])

    def test_bigram_overlap_counts_as_evidence(self) -> None:
        # 「王秀兰我妈叫」不是原话子串，但双字覆盖 4/5=0.8 → 视为有据。
        result = self._apply(
            [
                {
                    "kind": "third_party",
                    "predicate": "妈妈",
                    "object": "王秀兰",
                    "polarity": 1,
                    "status": "happened",
                    "mentioned_at": "2026-11-08T10:00:00+08:00",
                    "confidence": 0.9,
                    "evidence_quote": "王秀兰我妈叫",
                }
            ]
        )
        self.assertEqual(result["inserted"], 1)

    def test_date_must_be_in_quote_or_derivable(self) -> None:
        good = self._apply(
            [
                {
                    "kind": "date",
                    "predicate": "event_on",
                    "object": "明天",
                    "polarity": 1,
                    "status": "happened",
                    "event_date": "2026-11-09",
                    "mentioned_at": "2026-11-08T10:05:00+08:00",
                    "confidence": 0.9,
                    "evidence_quote": "明天要去医院复查",
                }
            ]
        )
        self.assertEqual(good["inserted"], 1)
        bad = self._apply(
            [
                {
                    "kind": "date",
                    "predicate": "event_on",
                    "object": "3月8日",
                    "polarity": 1,
                    "status": "happened",
                    "event_date": "2026-05-01",
                    "mentioned_at": "2026-11-08T10:05:00+08:00",
                    "confidence": 0.9,
                    "evidence_quote": "我不吃香菜，明天要去医院复查",
                }
            ]
        )
        self.assertEqual(bad["inserted"], 1)
        rows = [row for row in claim.list_claims(self.db) if row["kind"] == "date"]
        unknown = [row for row in rows if row["object"] == "3月8日"]
        self.assertEqual(len(unknown), 1)
        # task-46：日期推不出来只置空 event_date，不把事实 status 拖成 unknown。
        self.assertIsNone(unknown[0]["event_date"])
        self.assertEqual(unknown[0]["status"], "happened")

    def test_host_date_object_is_normalized_never_relative(self) -> None:
        result = self._apply(
            [
                {
                    "kind": "date",
                    "predicate": "event_on",
                    "object": "明天",
                    "polarity": 1,
                    "status": "happened",
                    "event_date": "2026-11-09",
                    "mentioned_at": "2026-11-08T10:05:00+08:00",
                    "confidence": 0.9,
                    "evidence_quote": "明天要去医院复查",
                }
            ]
        )
        self.assertEqual(result["inserted"], 1)
        rows = [row for row in claim.list_claims(self.db, kind="date")]
        self.assertEqual(rows[0]["object"], "2026-11-09")

    def test_host_date_object_without_derivation_is_dropped(self) -> None:
        result = self._apply(
            [
                {
                    "kind": "date",
                    "predicate": "event_on",
                    "object": "昨天",
                    "polarity": 1,
                    "status": "happened",
                    "mentioned_at": "2026-11-08T10:05:00+08:00",
                    "confidence": 0.9,
                    "evidence_quote": "明天要去医院复查",
                }
            ]
        )
        self.assertEqual(result["inserted"], 0)
        self.assertIn("dropped_date_unresolved", result["reasons"])

    def test_third_party_subject_is_mapped_to_subject_id(self) -> None:
        material = [
            {
                "id": "line-pet",
                "role": "user",
                "text": "冻梨倒好，趴桌上睡得四仰八叉",
                "ts": "2026-10-06T10:00:00+08:00",
            }
        ]
        result = claim_extract.apply(
            self.db,
            day_key="2026-10-06",
            payload={
                "claims": [
                    {
                        "kind": "third_party",
                        "subject": "冻梨",
                        "predicate": "睡在",
                        "object": "桌上",
                        "polarity": 1,
                        "status": "happened",
                        "mentioned_at": "2026-10-06T10:00:00+08:00",
                        "confidence": 0.9,
                        "evidence_quote": "冻梨倒好，趴桌上睡得四仰八叉",
                    }
                ]
            },
            config=ENABLED,
            material=material,
        )
        self.assertEqual(result["inserted"], 1)
        rows = claim.list_claims(self.db, subject_id="冻梨")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["predicate"], "睡在")
        self.assertEqual(rows[0]["object"], "桌上")

    def test_clock_readings_and_phrase_predicates_are_dropped(self) -> None:
        material = [
            {"id": "l1", "role": "user", "text": "昨晚又两点多", "ts": "2026-10-06T10:00:00+08:00"},
            {"id": "l2", "role": "user", "text": "八点半了", "ts": "2026-10-08T10:00:00+08:00"},
            {"id": "l3", "role": "user", "text": "冻梨倒好，趴桌上睡得四仰八叉", "ts": "2026-10-06T10:00:00+08:00"},
        ]
        result = claim_extract.apply(
            self.db,
            day_key="2026-10-06",
            payload={
                "claims": [
                    {"kind": "date", "predicate": "又到", "object": "两点多", "polarity": 1,
                     "status": "happened", "mentioned_at": "2026-10-06T10:00:00+08:00",
                     "confidence": 0.9, "evidence_quote": "昨晚又两点多"},
                    {"kind": "date", "predicate": "当前时间", "object": "20:30", "polarity": 1,
                     "status": "happened", "mentioned_at": "2026-10-08T10:00:00+08:00",
                     "confidence": 0.9, "evidence_quote": "八点半了"},
                    {"kind": "date", "predicate": "日期", "object": "20:30", "polarity": 1,
                     "status": "happened", "mentioned_at": "2026-10-08T10:00:00+08:00",
                     "confidence": 0.9, "evidence_quote": "八点半了"},
                    {"kind": "third_party", "subject": "冻梨", "predicate": "趴在桌上睡觉",
                     "object": "冻梨", "polarity": 1, "status": "happened",
                     "mentioned_at": "2026-10-06T10:00:00+08:00", "confidence": 0.9,
                     "evidence_quote": "冻梨倒好，趴桌上睡得四仰八叉"},
                ]
            },
            config=ENABLED,
            material=material,
        )
        self.assertEqual(result["inserted"], 0)
        self.assertIn("dropped_predicate", result["reasons"])
        self.assertIn("dropped_clock", result["reasons"])

    def test_uncertain_statement_becomes_unknown(self) -> None:
        journal.append(
            self.db, "我可能不吃香菜", role="user", ts="2026-11-08T10:07:00+08:00"
        )
        result = self._apply([self._claim("我可能不吃香菜", object="香菜")])
        self.assertEqual(result["inserted"], 1)
        rows = claim.list_claims(self.db, subject_id="self")
        self.assertEqual(rows[0]["status"], "unknown")

    def test_negation_polarity_is_corrected(self) -> None:
        result = self._apply([self._claim("我不吃香菜", polarity=1)])
        self.assertEqual(result["inserted"], 1)
        rows = claim.list_claims(self.db, subject_id="self")
        self.assertEqual(rows[0]["polarity"], 0)
        self.assertIn("polarity_corrected", result["reasons"])

    def test_confidence_thresholds(self) -> None:
        dropped = self._apply([self._claim("我不吃香菜", confidence=0.4)])
        self.assertEqual(dropped["inserted"], 0)
        self.assertIn("dropped_confidence", dropped["reasons"])
        candidate = self._apply([self._claim("我不吃香菜", confidence=0.7, object="香菜")])
        self.assertEqual(candidate["inserted"], 1)
        rows = claim.list_claims(self.db, subject_id="self")
        self.assertEqual(rows[0]["status"], "candidate")

    def test_kind_filter(self) -> None:
        result = self._apply([self._claim("我不吃香菜", kind="unknown_kind")])
        self.assertEqual(result["inserted"], 0)
        self.assertIn("dropped_kind", result["reasons"])

    def test_apply_is_idempotent(self) -> None:
        first = self._apply([self._claim("我不吃香菜")])
        second = self._apply([self._claim("我不吃香菜")])
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(len(claim.list_claims(self.db, subject_id="self")), 1)

    def test_apply_off_by_default(self) -> None:
        result = self._apply([self._claim("我不吃香菜")], config=DISABLED)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(claim.list_claims(self.db), [])


class RuleQualityTest(unittest.TestCase):
    """rule_coverage 防垃圾：谓词/客体过滤、日期归一、per-kind 上限。"""

    def _line(self, text: str, ts: str = "2026-05-03T10:00:00+08:00") -> dict:
        return {"id": f"line-{abs(hash(text)) % 100000}", "role": "user", "text": text, "ts": ts}

    def test_lead_reported_garbage_is_filtered(self) -> None:
        material = [
            self._line("那我先叫你小满吧，别一上来太熟。"),
            self._line("你说这毛病是不是得改改，不喝又困得眼睛睁不开。"),
        ]
        candidates = claim_extract.rule_candidates(material)
        for item in candidates:
            self.assertNotIn(item["predicate"], {"别", "不", "没", "也", "就", "还"})
            self.assertNotIn("一上来太熟", item["object"])
            self.assertNotIn("又困得眼睛睁不开", item["object"])

    def test_relative_dates_are_normalised_to_absolute(self) -> None:
        material = [
            self._line("今天要去医院复查。"),
            self._line("明天还得早起陪爸爸去社区医院。"),
            # P20 实测快照里的原文（2026-04-13）：不得把「今天/明天」当 object。
            self._line("今天就这样吧，明儿还得早起，得把作息往回掰一掰。", "2026-04-13T23:35:00+08:00"),
            self._line(
                "剩下的稿子明天白天再啃，晚上脑子实在不顶用，写出来全是废话，白天清醒点说不定还快些。",
                "2026-04-13T23:35:00+08:00",
            ),
        ]
        candidates = claim_extract.rule_candidates(material)
        dates = [item for item in candidates if item["kind"] == "date"]
        self.assertTrue(dates)
        objects = {item["object"] for item in dates}
        self.assertNotIn("今天", objects)
        self.assertNotIn("明天", objects)
        self.assertIn("2026-05-03", objects)
        self.assertIn("2026-05-04", objects)

    def test_yesterday_in_jiangbei_echo_is_resolved(self) -> None:
        material = [self._line("昨天在江北回声那边的咖啡店等你。")]
        dates = [item for item in claim_extract.rule_candidates(material) if item["kind"] == "date"]
        self.assertTrue(dates)
        self.assertEqual(dates[0]["object"], "2026-05-02")
        self.assertEqual(dates[0]["event_date"], "2026-05-02")

    def test_per_kind_cap(self) -> None:
        foods = ["香菜", "芹菜", "韭菜", "菠菜", "生菜", "白菜", "紫菜", "海带", "苦瓜", "南瓜",
                 "冬瓜", "丝瓜", "黄瓜", "茄子", "豆角", "洋葱", "大蒜", "生姜", "辣椒", "胡椒"]
        material = [self._line(f"我不吃{food}。") for food in foods]
        candidates = claim_extract.rule_candidates(material)
        prefs = [item for item in candidates if item["kind"] == "preference"]
        self.assertLessEqual(len(prefs), 8)
        self.assertGreaterEqual(len(prefs), 1)


class RuleFallbackTest(ClaimExtractCase):
    def test_rule_extract_three_classes(self) -> None:
        result = claim_extract.rule_extract(
            self.db, day_key=self.day, config=ENABLED
        )
        self.assertTrue(result["enabled"])
        kinds = {item["kind"] for item in result["rule_candidates"]}
        self.assertIn("third_party", kinds)
        self.assertIn("date", kinds)
        self.assertIn("preference", kinds)
        self.assertGreaterEqual(result["inserted"], 3)
        rows = claim.list_claims(self.db, subject_id="self", limit=50)
        prefs = [row for row in rows if row["kind"] == "preference"]
        self.assertTrue(prefs)
        self.assertTrue(all(row["polarity"] == 0 for row in prefs))

    def test_rule_extract_is_idempotent(self) -> None:
        first = claim_extract.rule_extract(self.db, day_key=self.day, config=ENABLED)
        second = claim_extract.rule_extract(self.db, day_key=self.day, config=ENABLED)
        self.assertGreaterEqual(first["inserted"], 3)
        self.assertEqual(second["inserted"], 0)
        self.assertGreaterEqual(second["updated"], 1)

    def test_rule_extract_off_by_default(self) -> None:
        result = claim_extract.rule_extract(
            self.db, day_key=self.day, config=DISABLED
        )
        self.assertFalse(result["enabled"])
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(claim.list_claims(self.db), [])


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
