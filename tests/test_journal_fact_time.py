"""派生事实的时间语义（0.31.0）。

三件事分开守：

1. ``journal_line.day_key`` / ``memory.day_key`` 都必须是**发生日**——日记在次日凌晨
   才消化，如果取「写入时刻」，整篇日记派生出来的事实会集体晚一天（实测 B 层 6 个库
   2118/2227 条偏 +1 天，本机真实库 166 条全偏 1–2 天）。而面板和召回 payload 一直
   把 ``day_key`` 当日期显示，于是用户看到的是错日期。
2. 派生事实的 ``valid_from`` 必须是**它自己那行原话**的 ts，不是当天第一条：一天里上午
   和晚上说的两件事，日期相同、时刻不同。
3. repair 重放要与 digest 走同一套时间规则，否则补出来的事实会跳一天。

时间断言一律拿 ``clock.occurred_day(...)`` 现算，不写死日期：``day_key`` 折的是本机
时区，写死具体某天会在其它时区的 CI 上假红。测试里的时间点选在 UTC 上午，
保证常见时区（UTC / UTC+8 / Europe）都落在同一天。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.base.ids import ulid
from pmem.memory import journal
from pmem.memory.store import MemoryStore

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})

MORNING = "2026-05-03T10:00:00Z"
EVENING = "2026-05-03T11:00:00Z"
DIGEST_AT = "2026-05-04T10:00:00Z"  # 次日才整理：这正是会漂一天的时刻
DAY = clock.occurred_day(MORNING)


def _local(iso: str) -> str:
    return clock.occurred_day(iso) or ""


class OccurredDayHelperCase(unittest.TestCase):
    def test_occurred_day_folds_iso_to_local_date(self) -> None:
        import re

        self.assertRegex(clock.occurred_day(MORNING) or "", r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(
            clock.occurred_day(MORNING), _local(MORNING)
        )

    def test_occurred_day_falls_back_instead_of_raising(self) -> None:
        self.assertEqual(clock.occurred_day(None, fallback="2026-01-01"), "2026-01-01")
        self.assertEqual(clock.occurred_day("不是时间", fallback="2026-01-01"), "2026-01-01")
        self.assertIsNone(clock.occurred_day(""))

    def test_occurred_day_is_not_derived_from_now(self) -> None:
        """必须只看传入的时间，不能被 ``clock.set_now`` 影响。"""
        clock.set_now(datetime.fromisoformat("2027-01-01T10:00:00+00:00"))
        try:
            self.assertEqual(clock.occurred_day(MORNING), DAY)
        finally:
            clock.reset_now()


class JournalFactTimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("journal-fact-time")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")

    def tearDown(self) -> None:
        self.db.close()
        clock.reset_now()
        drop_dir(self.dir)

    def _append(self, text: str, ts: str, *, role: str = "user") -> None:
        clock.set_now(datetime.fromisoformat(ts))
        journal.append(self.db, text, role=role, ts=ts, session_id="s-2026-05-03")

    def _digest(self) -> dict:
        clock.set_now(datetime.fromisoformat(DIGEST_AT))
        return journal.digest(self.db, subject="self", day_key=DAY, store=self.store, config=CFG)

    def _memories(self) -> list:
        return list(
            self.db.conn.execute(
                "SELECT day_key, valid_from, text_raw FROM memory ORDER BY seq"
            )
        )

    def test_digest_next_morning_keeps_occurrence_day(self) -> None:
        self.assertEqual(
            clock.occurred_day(DIGEST_AT), "2026-05-04", "测试时间点应跨天（否则测不出漂移）"
        )
        self.assertNotEqual(clock.occurred_day(DIGEST_AT), DAY)
        self._append("上午去医院复查，医生让少熬夜", MORNING)
        self._append("好，记下了", MORNING, role="assistant")
        self._append("晚上在江北回声替唐粟，轻轨延误迟到了", EVENING)

        result = self._digest()
        self.assertTrue(result["ok"], result)

        rows = self._memories()
        self.assertEqual(len(rows), 2, rows)
        for row in rows:
            self.assertEqual(
                row["day_key"], DAY, f"day_key 应是发生日而不是整理日：{dict(row)}"
            )
        # 逐条时间：上午那条保留上午的时刻，不是当天第一条的「代表值」
        self.assertEqual([row["valid_from"] for row in rows], [MORNING, EVENING])

    def test_journal_line_and_fact_share_the_same_day(self) -> None:
        self._append("晚上在江北回声替唐粟，轻轨延误迟到了", EVENING)
        line = self.db.conn.execute(
            "SELECT id, day_key FROM journal_line ORDER BY seq LIMIT 1"
        ).fetchone()
        self.assertEqual(line["day_key"], DAY)

        self._digest()
        fact = self.db.conn.execute("SELECT day_key FROM memory LIMIT 1").fetchone()
        self.assertIsNotNone(fact, "digest 应该派生出一条事实")
        self.assertEqual(fact["day_key"], line["day_key"], "两层对同一时刻应落在同一天")

    def test_repair_replays_per_line_time(self) -> None:
        """digest 半截失败（只写成第一条）时，repair 补出来的那条要用**它自己**的时间。"""
        self._append("上午去医院复查，医生让少熬夜", MORNING)
        self._append("晚上在江北回声替唐粟，轻轨延误迟到了", EVENING)
        line_ids = [
            row["id"] for row in self.db.conn.execute("SELECT id FROM journal_line ORDER BY seq")
        ]
        # 造一篇「声称派生 2 条、实际只写成 1 条」的日记 —— digest 半截失败的真实形状。
        episode_id = ulid()
        derivation = json.dumps({"lines": line_ids, "line_count": 2, "mode": "template"})
        self.db.conn.execute(
            "INSERT INTO episode(id, subject_id, channel, started_at, ended_at, day_key, title, "
            "summary, memory_count, derivation, extra_json) "
            "VALUES(?, 'self', 'journal', ?, ?, ?, '测试日记', '上午复查，晚上替班', 2, ?, '{}')",
            (episode_id, MORNING, EVENING, DAY, derivation),
        )
        for line_id in line_ids:
            self.db.conn.execute(
                "UPDATE journal_line SET episode_id=? WHERE id=?", (episode_id, line_id)
            )
        self.store.add(
            "用户上午去医院复查，医生让少熬夜",
            **journal._fact_kwargs(episode_id, 0, mode="template", valid_from=MORNING),
        )

        repaired = journal.repair(self.db, subject="self", store=self.store)
        self.assertEqual(repaired["added"], 1, repaired)

        restored = self.db.conn.execute(
            "SELECT day_key, valid_from FROM memory WHERE valid_from=?", (EVENING,)
        ).fetchone()
        self.assertIsNotNone(restored, "repair 没把第二条补回来")
        self.assertEqual(restored["day_key"], DAY)
        self.assertEqual(restored["valid_from"], EVENING, "补写不能借用当天第一条的时间")


class StoreDayKeyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("store-day-key")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")

    def tearDown(self) -> None:
        self.db.close()
        clock.reset_now()
        drop_dir(self.dir)

    def test_explicit_day_key_wins(self) -> None:
        clock.set_now(datetime.fromisoformat(DIGEST_AT))
        saved = self.store.add(
            "用户上午去医院复查",
            source_kind="journal",
            valid_from=MORNING,
            day_key=DAY,
        )
        row = self.db.conn.execute(
            "SELECT day_key, valid_from FROM memory WHERE id=?", (saved["id"],)
        ).fetchone()
        self.assertEqual(row["day_key"], DAY)
        self.assertEqual(row["valid_from"], MORNING)

    def test_day_key_falls_back_to_write_time(self) -> None:
        clock.set_now(datetime.fromisoformat(DIGEST_AT))
        saved = self.store.add("用户喜欢下雨天")
        row = self.db.conn.execute(
            "SELECT day_key FROM memory WHERE id=?", (saved["id"],)
        ).fetchone()
        self.assertEqual(row["day_key"], clock.occurred_day(DIGEST_AT))

    def test_bad_day_key_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.store.add("用户喜欢下雨天", day_key="2026/05/03")
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"], 0
        )


if __name__ == "__main__":
    unittest.main()
