"""调度器的 journal prune 必须每 tick 都跑。

原来的实现把 ``prune`` 挂在 ``digest`` 成功之后：不 due 直接 return、digest 抛异常
也 return。可清理是幂等、便宜的独立动作，不该被「今天还不用写日记」或
「写日记一直失败」挡住——否则过期流水会永远烂在库里。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.ops import scheduler

from .support import drop_dir, temp_dir

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class SchedulerPruneTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("sched-prune")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.db = self.app.db
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def _append(self, base: datetime, texts: list[str]) -> None:
        for index, text in enumerate(texts):
            journal.append(
                self.db,
                text,
                role="user",
                session_id="s1",
                ts=(base + timedelta(minutes=index)).isoformat(),
            )

    def _digest(self, base: datetime, texts: list[str]) -> None:
        self._append(base, texts)
        clock.set_now(base + timedelta(minutes=50))
        result = journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(journal.pending(self.db), [])

    def _line_count(self) -> int:
        return int(
            self.db.conn.execute("SELECT COUNT(*) c FROM journal_line").fetchone()["c"]
        )

    def test_prune_runs_when_not_due(self) -> None:
        self._digest(
            T0,
            ["我昨晚看了石头门", "今天中午吃了牛肉面", "我在杭州做前端开发"],
        )
        self.assertEqual(self._line_count(), 3)

        # 40 天后：没有待整理的流水（不 due），但 3 条已消化的旧流水早该清了
        clock.set_now(T0 + timedelta(days=40))
        note = scheduler.journal_tick(self.db, Config(), "self")
        self.assertTrue(note["ok"], note)
        self.assertIn("skipped", note)
        self.assertEqual(note["due"]["reason"], "no_lines")
        self.assertEqual(note["pruned"], 3)
        self.assertEqual(self._line_count(), 0)

    def test_prune_still_runs_when_digest_fails(self) -> None:
        self._digest(
            T0,
            ["我昨晚看了石头门", "今天中午吃了牛肉面", "我在杭州做前端开发"],
        )

        # 40 天后再攒一批流水，让 daemon 因为 idle 到期而去 digest；然后让它失败
        later = T0 + timedelta(days=40)
        self._append(
            later,
            ["今天又在写 surface 的过滤", "下午跑了 scheduler 的清理", "晚上继续改测试"],
        )
        clock.set_now(later + timedelta(minutes=50))
        with mock.patch.object(journal, "digest", side_effect=RuntimeError("boom")):
            note = scheduler.journal_tick(self.db, Config(), "self")

        self.assertFalse(note["ok"], note)
        self.assertIn("boom", note["error"])
        self.assertEqual(note["due"]["reason"], "idle")
        self.assertEqual(note["pruned"], 3)
        # 过期且已消化的旧流水被删，新攒的待整理流水原样保留
        old_day = T0.date().isoformat()
        old_left = self.db.conn.execute(
            "SELECT COUNT(*) c FROM journal_line WHERE day_key=?", (old_day,)
        ).fetchone()["c"]
        self.assertEqual(old_left, 0)
        self.assertEqual(len(journal.pending(self.db)), 3)


if __name__ == "__main__":
    unittest.main()
