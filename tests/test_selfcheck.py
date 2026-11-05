"""作答自检（selfcheck）：编造的日期/人名要被抓出来，有据的东西不能被误报。

守五件事：

1. 答案里写了库里真实存在的日期（``day_key`` 或 ``valid_from``）→ 不报；
2. 答案里的日期在 ``journal_line``（日记流水）里有 → 不报；
3. 库里没有那一天的记录 → 报 ``kind="date"``；当天/未来日期同样能被判断；
4. 实体表里有的人名 → 不报；库里查无此人的 2–4 字中文名 → 报 ``kind="name"``；
5. 干净答案 ``clean=True``，``format_report`` 一句话说清。

返回字段叫 ``clean`` 不叫 ``ok``：gate 的 ``_ok()`` 已经把整条结果包成
``{"ok": true, "data": ...}``，模块里再用 ``ok`` 会出现
``{"ok": true, "data": {"ok": false}}`` 这种自相矛盾的信封。

时间一律用 ``clock.set_now`` 固定，不靠真实日历——自检里「今天」要参与
无年份日期的年份推断，真实日历会让断言在某天之后漂掉。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

from pmem.base import clock
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.memory.selfcheck import check_answer, format_report

from .support import drop_dir, temp_dir

# 自检把「今天」当无年份日期的推断锚点：固定在 2026-11-05。
T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class SelfcheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("selfcheck")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.store = self.app.store
        clock.set_now(T0)
        # 第一条：真实日期走 valid_from（2026-11-03），写入日（day_key）是 2026-11-05。
        self.store.add(
            "和医生约了复查",
            session_id="s1",
            valid_from="2026-11-03T09:00:00+00:00",
            created_at="2026-11-05T09:00:00+00:00",
        )
        # 第二条：库里真实存在的人名「李静」。
        self.store.add("李静说下周要去上海出差", session_id="s1", created_at="2026-11-05T09:30:00+00:00")
        # 日记流水：2026-11-04 那天的原话（自检要能拿它当日期证据）。
        journal.append(
            self.app.db,
            "昨天商量了装修方案",
            role="user",
            session_id="s1",
            ts="2026-11-04T09:00:00+00:00",
        )

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def _kinds(self, result: dict) -> list[str]:
        return [str(issue.get("kind")) for issue in result.get("issues") or []]

    def _spans(self, result: dict, kind: str) -> list[str]:
        return [
            str(issue.get("span"))
            for issue in result.get("issues") or []
            if str(issue.get("kind")) == kind
        ]


class DateCheckTest(SelfcheckCase):
    def test_real_date_by_day_key_is_not_reported(self) -> None:
        result = check_answer(self.store, "你 2026年11月5日和李静去看了房子")
        self.assertTrue(result["clean"], result)
        self.assertEqual(self._kinds(result), [])
        self.assertEqual(result["checked"]["dates"], 1)

    def test_real_date_by_valid_from_is_not_reported(self) -> None:
        result = check_answer(self.store, "11月3日的复查报告出来了")
        self.assertTrue(result["clean"], result)

    def test_journal_date_is_not_reported(self) -> None:
        result = check_answer(self.store, "11月4日我们商量了装修方案")
        self.assertTrue(result["clean"], result)

    def test_fabricated_date_is_reported(self) -> None:
        result = check_answer(self.store, "你 11月8日去看了房子")
        self.assertFalse(result["clean"], result)
        self.assertEqual(self._kinds(result), ["date"])
        self.assertIn("11月8日", self._spans(result, "date")[0])
        self.assertTrue(result["issues"][0]["reason"], "date issue 必须说明为什么对不上")

    def test_future_date_can_be_judged(self) -> None:
        # 写明年份的未来日期也要被判：库里没有它的记录就是没出处。
        result = check_answer(self.store, "2027-01-01 要交报告")
        self.assertEqual(self._kinds(result), ["date"])
        self.assertIn("2027-01-01", self._spans(result, "date")[0])

    def test_impossible_date_is_reported(self) -> None:
        result = check_answer(self.store, "2026-02-30 那天签的合同")
        self.assertEqual(self._kinds(result), ["date"])


class NameCheckTest(SelfcheckCase):
    def test_known_name_is_not_reported(self) -> None:
        result = check_answer(self.store, "李静下周要去上海出差")
        self.assertTrue(result["clean"], result)
        self.assertEqual(self._spans(result, "name"), [])
        self.assertGreaterEqual(result["checked"]["names"], 1)

    def test_fabricated_name_is_reported(self) -> None:
        result = check_answer(self.store, "你上周和李婷去看了房子")
        self.assertFalse(result["clean"], result)
        self.assertEqual(self._kinds(result), ["name"])
        self.assertEqual(self._spans(result, "name"), ["李婷"])

    def test_name_span_does_not_absorb_trailing_function_word(self) -> None:
        # 「赵铁柱当时也在」：右边界要裁到「赵铁柱」，不能报成「赵铁柱当」。
        result = check_answer(self.store, "赵铁柱当时也在")
        self.assertEqual(self._spans(result, "name"), ["赵铁柱"])

    def test_name_variant_in_memory_text_is_grounded(self) -> None:
        # 实体表没抽到的名字，只要原文里出现过也算有据（实体抽取本来就宁可漏）。
        self.store.add("王小明昨天来家里坐了坐", session_id="s1")
        result = check_answer(self.store, "王小明昨天来家里坐了坐")
        self.assertTrue(result["clean"], result)


class CleanAnswerTest(SelfcheckCase):
    def test_clean_answer_is_ok(self) -> None:
        result = check_answer(
            self.store,
            "2026-11-05 你和李静约了复查，11月3日报告出来，11月4日商量了装修方案",
        )
        self.assertTrue(result["clean"], result)
        self.assertEqual(result["checked"]["dates"], 3)
        self.assertGreaterEqual(result["checked"]["names"], 1)

    def test_format_report_clean_is_one_line(self) -> None:
        report = format_report(check_answer(self.store, "11月3日复查了"))
        self.assertIn("日期/专名都对得上", report)
        self.assertNotIn("\n", report)

    def test_format_report_lists_issues(self) -> None:
        report = format_report(check_answer(self.store, "你 11月8日和李婷去看了房子"))
        self.assertIn("日期", report)
        self.assertIn("专名", report)
        self.assertIn("11月8日", report)
        self.assertIn("李婷", report)

    def test_long_answer_is_truncated_with_note(self) -> None:
        # 超长答案先截断再核对，并在 note 里说清楚（不然「为什么后半段没核」没人知道）。
        answer = "11月3日复查了。" * 600  # 5400 字符，超过 4000 的截断线
        self.assertGreater(len(answer), 4000)
        result = check_answer(self.store, answer)
        self.assertIn("截断", result["note"])

    def test_sdk_selfcheck_is_wired(self) -> None:
        payload = self.app.selfcheck("你 11月8日和李婷去看了房子")
        self.assertTrue(payload["ok"], payload)
        data = payload["data"]
        self.assertFalse(data["clean"])
        self.assertIn("11月8日", data["report"])


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
