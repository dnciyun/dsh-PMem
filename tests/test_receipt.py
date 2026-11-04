"""M9.9：浮现回执——这一轮的回答里真的用到了哪几条浮现过的记忆。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.gate.sdk import PMem
from pmem.memory import receipt

from .support import drop_dir, temp_dir

TEXT = "我最喜欢的颜色是深蓝色，因为夜里看海最安静。"
QUOTE = "深蓝色，因为夜里看海最安静"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("receipt")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store
        self.mem = self.store.add(TEXT, groups=["我/偏好"], grade="B")
        # 先让它浮现：回执只判「浮现过」的那些。
        self.app.surface(session_id="s1", cue="颜色", mode="turn")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def usage(self) -> tuple[int, str | None]:
        row = self.db.conn.execute(
            "SELECT use_count, last_used_at FROM memory WHERE id=?", (self.mem["id"],)
        ).fetchone()
        return int(row["use_count"]), row["last_used_at"]

    def used_events(self) -> list:
        return self.db.conn.execute(
            "SELECT * FROM event_log WHERE op='surface.used'"
        ).fetchall()


class TestJudge(Case):
    def test_verbatim_reuse_counts_as_used(self) -> None:
        before, _ = self.usage()
        data = self.app.receipt(session_id="s1", answer=f"我记得你说过：{QUOTE}。")["data"]
        self.assertEqual(len(data["matched"]), 1)
        self.assertEqual(data["matched"][0]["memory_id"], self.mem["id"])
        span = data["matched"][0]["span"]
        self.assertEqual(len(span), receipt.MIN_SPAN)
        self.assertIn(span, QUOTE)
        after, last_used = self.usage()
        self.assertEqual(after, before + 1)
        self.assertIsNotNone(last_used)
        self.assertEqual(self.used_events()[0]["op"], "surface.used")

    def test_unrelated_answer_is_not_used(self) -> None:
        before, _ = self.usage()
        data = self.app.receipt(session_id="s1", answer="好的，我这就去改那个脚本。")["data"]
        self.assertEqual(data["matched"], [])
        self.assertEqual(self.usage()[0], before)
        self.assertEqual(len(self.used_events()), 0)

    def test_echoing_the_question_is_not_used(self) -> None:
        # 用户这句话本身就含那 8 个字：回答复述题面，不能算「用了记忆」。
        cue = QUOTE
        before, _ = self.usage()
        data = self.app.receipt(session_id="s1", answer=f"你问的是「{cue}」，我的回答是……", exclude=cue)["data"]
        self.assertEqual(data["matched"], [])
        self.assertEqual(self.usage()[0], before)

    def test_short_overlap_does_not_count(self) -> None:
        # 只撞了 4 个字：改写/转述不算，宁可少记。
        data = self.app.receipt(session_id="s1", answer="深蓝色很好看。")["data"]
        self.assertEqual(data["matched"], [])

    def test_dry_run_writes_nothing(self) -> None:
        before, _ = self.usage()
        data = self.app.receipt(session_id="s1", answer=QUOTE, dry_run=True)["data"]
        self.assertEqual(len(data["matched"]), 1)
        self.assertEqual(self.usage()[0], before)
        self.assertEqual(len(self.used_events()), 0)

    def test_receipt_is_idempotent_per_session(self) -> None:
        self.app.receipt(session_id="s1", answer=QUOTE)
        second = self.app.receipt(session_id="s1", answer=QUOTE)["data"]
        # 已经回过执的不再进候选
        self.assertEqual(second["candidates"], 0)
        self.assertEqual(len(self.used_events()), 1)

    def test_unknown_session_is_harmless(self) -> None:
        data = self.app.receipt(session_id="nope", answer=QUOTE)["data"]
        self.assertEqual(data["candidates"], 0)
        self.assertEqual(data["matched"], [])

    def test_deleted_memory_is_skipped(self) -> None:
        self.store.delete(self.mem["id"])
        data = self.app.receipt(session_id="s1", answer=QUOTE)["data"]
        self.assertEqual(data["matched"], [])


class TestTokenSample(Case):
    def test_token_sample_is_recorded_and_aggregated(self) -> None:
        self.app.receipt(session_id="s1", answer="无关", tokens=420)
        self.app.receipt(session_id="s1", answer="无关", tokens=380)
        stats = receipt.token_stats(self.db)
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["mean"], 400.0)
        self.assertEqual(stats["max"], 420.0)

    def test_token_stats_empty(self) -> None:
        self.assertEqual(receipt.token_stats(self.db)["samples"], 0)


if __name__ == "__main__":
    unittest.main()
