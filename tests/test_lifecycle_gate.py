"""淡忘可控（task-37）：``lifecycle.enabled`` 默认关时两处自动触发点都不写状态。

守住四件事：

1. 默认关（``lifecycle.enabled=false``）跑 s7_reflect → 无 dormant/archived 产生，
   报告里 ``lifecycle_skipped: True``、``lifecycle_moved: 0``；
2. 打开时 s7_reflect 与直接调 ``lifecycle.tick`` 的结果一致（0.39.0 行为）；
3. 浮现（surface）自动 tick 关时只写 ``surface.lifecycle_day`` 标记，不碰记忆状态；
4. 显式 ``app.lifecycle()`` / CLI 不受开关限制，只加一行 note。
"""

from __future__ import annotations

import unittest

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import lifecycle
from pmem.sleep.stages import SleepContext, s7_reflect

from .support import drop_dir, temp_dir

OLD_ACTIVE = "2026-01-01T10:00:00+00:00"
OLD_DORMANT = "2025-01-01T10:00:00+00:00"


class LifecycleGateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs = []
        self._apps: list[PMem] = []
        clock.set_now("2026-11-07T12:00:00+08:00")

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, *, enabled: bool | None = None) -> PMem:
        path = temp_dir("lifecycle-gate")
        self._dirs.append(path)
        config = None
        if enabled is not None:
            config = Config({"lifecycle": {"enabled": enabled}})
        app = PMem.open(path / "pmem.db", config=config)
        self._apps.append(app)
        return app

    def _add(self, app: PMem, text: str, created_at: str) -> str:
        app.store.add(text, session_id="s1", created_at=created_at)
        row = app.db.conn.execute(
            "SELECT id FROM memory WHERE text_raw=? ORDER BY seq DESC LIMIT 1", (text,)
        ).fetchone()
        self.assertIsNotNone(row, text)
        return str(row["id"])

    def _status(self, app: PMem, memory_id: str) -> str:
        row = app.db.conn.execute(
            "SELECT status FROM memory WHERE id=?", (memory_id,)
        ).fetchone()
        return str(row["status"])

    def _sleep_ctx(self, app: PMem) -> SleepContext:
        return SleepContext(
            db=app.db,
            subject="self",
            day_key=clock.day_key(),
            run_id="lifecycle-gate-test",
            config=app.config,
        )


class SleepLifecycleGateTest(LifecycleGateCase):
    def test_default_off_reflect_does_not_change_status(self) -> None:
        app = self._open()  # 不传 config → 默认 lifecycle.enabled=false
        active = self._add(app, "旧任务细节，早该被淡忘", OLD_ACTIVE)
        result = s7_reflect(self._sleep_ctx(app))
        self.assertEqual(result["lifecycle_moved"], 0)
        self.assertTrue(result["lifecycle_skipped"])
        self.assertTrue(result["lifecycle_dry_run"] is False)
        self.assertEqual(self._status(app, active), "active")

    def test_enabled_reflect_matches_direct_tick(self) -> None:
        app = self._open(enabled=True)
        active = self._add(app, "旧任务细节，该转休眠了", OLD_ACTIVE)
        dormant = self._add(app, "更早的任务细节，该归档了", OLD_DORMANT)
        app.db.conn.execute("UPDATE memory SET status='dormant' WHERE id=?", (dormant,))
        app.db.conn.commit()

        dry = lifecycle.tick(app.db, subject="self", dry_run=True)
        result = s7_reflect(self._sleep_ctx(app))
        self.assertNotIn("lifecycle_skipped", result, "打开时报告应与 0.39.0 一致（不加新字段）")
        self.assertEqual(result["lifecycle_moved"], dry["moved"])
        self.assertEqual(self._status(app, active), "dormant")
        self.assertEqual(self._status(app, dormant), "archived")


class SurfaceLifecycleGateTest(LifecycleGateCase):
    def test_surface_off_only_writes_day_marker(self) -> None:
        app = self._open(enabled=False)
        active = self._add(app, "旧任务细节，浮现时不该被动", OLD_ACTIVE)
        data = app.surface(session_id="surface-gate", mode="session_start")
        self.assertTrue(data["ok"], data)
        self.assertEqual(self._status(app, active), "active")
        self.assertEqual(app.db.meta("surface.lifecycle_day"), clock.day_key())

    def test_surface_on_runs_auto_tick(self) -> None:
        app = self._open(enabled=True)
        active = self._add(app, "旧任务细节，浮现时该转休眠", OLD_ACTIVE)
        data = app.surface(session_id="surface-gate-on", mode="session_start")
        self.assertTrue(data["ok"], data)
        self.assertEqual(self._status(app, active), "dormant")


class ExplicitLifecycleTest(LifecycleGateCase):
    def test_explicit_lifecycle_runs_even_when_disabled(self) -> None:
        app = self._open(enabled=False)
        active = self._add(app, "旧任务细节，显式调用仍要流转", OLD_ACTIVE)
        data = app.lifecycle()
        self.assertTrue(data["ok"], data)
        self.assertGreaterEqual(data["data"]["moved"], 1)
        self.assertIn("自动淡忘当前关闭", data["data"]["note"])
        self.assertEqual(self._status(app, active), "dormant")

    def test_explicit_lifecycle_note_absent_when_enabled(self) -> None:
        app = self._open(enabled=True)
        data = app.lifecycle(dry_run=True)
        self.assertTrue(data["ok"], data)
        self.assertNotIn("note", data["data"])


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
