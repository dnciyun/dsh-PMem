"""时间闸门：让「六个月」能在一个下午里走完。

这一层如果只是 monkeypatch ``clock.now``，是**不会生效**的——全项目三十多个模块
`from ..base.clock import now_iso`，绑定发生在 import 时。所以闸门必须写在
``now()`` 内部，让已经绑定的引用也一起走模拟时间。这个测试就是来守这件事的。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base import clock
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.memory import lifecycle

from .support import drop_dir, temp_dir

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


class Base(unittest.TestCase):
    def tearDown(self) -> None:
        clock.reset_now()
        os.environ.pop(clock.ENV_NOW, None)


class TestSeam(Base):
    def test_set_now_drives_rbound_imports(self) -> None:
        # 这些名字在 import 时就已经绑好了；闸门必须仍然生效。
        from pmem.memory.store import now_iso as store_now_iso
        from pmem.memory.surface import day_key as surface_day_key

        clock.set_now(T0)
        self.assertEqual(store_now_iso(), "2026-01-01T09:00:00.000Z")
        self.assertEqual(surface_day_key(), "2026-01-01")
        self.assertEqual(clock.now(), T0)

    def test_accepts_iso_string(self) -> None:
        clock.set_now("2027-03-04T05:06:07Z")
        self.assertEqual(clock.now_iso(), "2027-03-04T05:06:07.000Z")

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        clock.set_now(datetime(2026, 6, 1, 12, 0))
        self.assertEqual(clock.now_iso(), "2026-06-01T12:00:00.000Z")

    def test_reset_returns_to_real_time(self) -> None:
        clock.set_now(T0)
        clock.reset_now()
        delta = abs((clock.now() - datetime.now(UTC)).total_seconds())
        self.assertLess(delta, 60)

    def test_env_var_is_honoured_and_can_be_cleared(self) -> None:
        os.environ[clock.ENV_NOW] = "2028-02-02T02:02:02Z"
        self.assertEqual(clock.now_iso(), "2028-02-02T02:02:02.000Z")
        os.environ[clock.ENV_NOW] = "2029-03-03T03:03:03Z"
        self.assertEqual(clock.now_iso(), "2029-03-03T03:03:03.000Z")
        os.environ.pop(clock.ENV_NOW)
        self.assertLess(abs((clock.now() - datetime.now(UTC)).total_seconds()), 60)

    def test_set_now_wins_over_env(self) -> None:
        os.environ[clock.ENV_NOW] = "2028-02-02T02:02:02Z"
        clock.set_now(T0)
        self.assertEqual(clock.now(), T0)

    def test_days_between_uses_the_override(self) -> None:
        clock.set_now(T0 + timedelta(days=30))
        self.assertAlmostEqual(clock.days_between("2026-01-01T09:00:00.000Z"), 30.0, places=3)


class TestTimeTravel(Base):
    def setUp(self) -> None:
        self._dir = temp_dir("clock")
        self.db_path = Path(self._dir) / "pmem.db"
        clock.set_now(T0)
        self.app = PMem.open(self.db_path)
        self.db = self.app.db

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)
        super().tearDown()

    def test_memory_created_at_follows_simulated_clock(self) -> None:
        added = self.app.store.add("时间旅行：今天的记忆", grade="B")
        row = self.db.conn.execute(
            "SELECT created_at FROM memory WHERE id=?", (added["id"],)
        ).fetchone()
        self.assertEqual(row["created_at"], "2026-01-01T09:00:00.000Z")

    def test_decay_runs_on_simulated_time(self) -> None:
        mem = self.app.store.add("时间旅行：会沉下去的一条", grade="B", groups=["工作/项目"])
        clock.set_now(T0 + timedelta(days=120))  # B 级 90 天
        result = lifecycle.tick(self.db, subject="self")
        self.assertEqual(result["by_status"].get("dormant"), 1)
        row = self.db.conn.execute("SELECT status FROM memory WHERE id=?", (mem["id"],)).fetchone()
        self.assertEqual(row["status"], "dormant")

    def test_cli_now_flag(self) -> None:
        code = cli.main(
            [
                "--now", "2027-07-07T07:07:07Z",
                "--db", str(self.db_path),
                "--json", "add", "时间旅行：CLI 写的",
            ]
        )
        self.assertEqual(code, 0)
        out = self.db.conn.execute(
            "SELECT created_at FROM memory ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(out["created_at"], "2027-07-07T07:07:07.000Z")


if __name__ == "__main__":
    unittest.main()
