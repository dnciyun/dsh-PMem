"""M9.9 维护窗口闸：窗口内 daemon 不执行任何任务，过期自动失效，手动命令不受影响。

闸门只跳过「执行」，不跳过「自愈」：``ensure_jobs`` + ``jobs.recover_stale`` 照跑，
任务的 ``next_run_at`` 原地不动，窗口结束后下一次 tick 自动补跑（补偿式调度）。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base.config import Config
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.ops import jobs, maintenance, scheduler

from .support import drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class MaintenanceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("mnt")
        self.path = Path(self._dir) / "pmem.db"
        self.backup_dir = Path(self._dir) / "backups"
        self.app = PMem.open(self.path)
        self.cfg = Config(
            {
                "runtime": {
                    "backup_dir": str(self.backup_dir),
                    "backup_keep": 14,
                    "daily_keep": 7,
                    "stale_minutes": 60,
                    "backup_at": "04:00",
                    "sleep_at": "03:30",
                }
            }
        )
        self.t0 = datetime.now(UTC).replace(microsecond=0)

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    # -- 构造 ---------------------------------------------------------------
    def _due_sleep_job(self) -> str:
        """登记全部任务，并把 sleep 的 next_run_at 拨到过去，制造「到期」。"""
        scheduler.ensure_jobs(self.app.db, now=self.t0, config=self.cfg)
        due = scheduler.to_iso(self.t0 - timedelta(minutes=1))
        self.app.db.conn.execute("UPDATE job SET next_run_at=? WHERE id='sched:sleep'", (due,))
        return jobs.get(self.app.db, "sleep")["next_run_at"]

    # -- 标记文件 -----------------------------------------------------------
    def test_mark_writes_reason_started_until(self) -> None:
        result = maintenance.mark(
            self.app.db, config=self.cfg, reason="迁移前窗口", minutes=30, at=self.t0
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["active"])
        path = Path(result["path"])
        self.assertEqual(path, Path(self._dir) / "maintenance.json")
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["reason"], "迁移前窗口")
        self.assertEqual(data["started_at"], scheduler.to_iso(self.t0))
        self.assertEqual(
            data["until"], scheduler.to_iso(self.t0 + timedelta(minutes=30))
        )
        state = maintenance.active(self.app.db, config=self.cfg, at=self.t0 + timedelta(minutes=5))
        self.assertTrue(state["active"])
        self.assertEqual(state["reason"], "迁移前窗口")
        self.assertEqual(state["remaining_seconds"], 25 * 60)

    def test_expired_marker_is_inactive(self) -> None:
        # 写在 10 分钟前、窗口只有 5 分钟：对任何「现在」都是过期状态（CLI 也读得到）
        maintenance.mark(
            self.app.db,
            config=self.cfg,
            reason="短窗口",
            minutes=5,
            at=self.t0 - timedelta(minutes=10),
        )
        state = maintenance.active(self.app.db, config=self.cfg, at=self.t0)
        self.assertFalse(state["active"])
        self.assertTrue(state["expired"])
        self.assertEqual(state["reason"], "短窗口")
        # 过期不算活跃，但也不需要删文件；status 能讲清楚
        code, out = run_cli(["--db", str(self.path), "--json", "maintenance", "status"])
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertFalse(data["active"])

    def test_clear_is_idempotent(self) -> None:
        maintenance.mark(self.app.db, config=self.cfg, minutes=10)
        first = maintenance.clear(self.app.db, config=self.cfg)
        self.assertTrue(first["ok"])
        self.assertTrue(first["cleared"])
        second = maintenance.clear(self.app.db, config=self.cfg)
        self.assertTrue(second["ok"])
        self.assertFalse(second["cleared"])
        self.assertFalse(maintenance.active(self.app.db, config=self.cfg)["active"])

    # -- tick 闸门 ----------------------------------------------------------
    def test_tick_skips_every_job_in_window(self) -> None:
        before = self._due_sleep_job()
        maintenance.mark(self.app.db, config=self.cfg, reason="备份窗口", minutes=30, at=self.t0)
        called: list[str] = []

        def fake(db, cfg, subject, kind, payload):
            called.append(kind)
            return {"ok": True}

        result = scheduler.tick(
            self.app.db, now=self.t0 + timedelta(minutes=5), config=self.cfg, runner=fake
        )
        self.assertTrue(result["maintenance"])
        self.assertEqual(result["reason"], "备份窗口")
        self.assertEqual(result["ran"], [])
        self.assertEqual(called, [])
        self.assertIn("sleep", result["skipped"])
        self.assertEqual(result["journal"]["skipped"], "maintenance")
        # next_run_at 不动：这就是「窗口结束后自动补跑」的依据
        self.assertEqual(jobs.get(self.app.db, "sleep")["next_run_at"], before)

    def test_tick_runs_after_window_ends(self) -> None:
        self._due_sleep_job()
        maintenance.mark(self.app.db, config=self.cfg, reason="短窗口", minutes=5, at=self.t0)
        called: list[str] = []

        def fake(db, cfg, subject, kind, payload):
            called.append(kind)
            return {"ok": True}

        result = scheduler.tick(
            self.app.db, now=self.t0 + timedelta(minutes=10), config=self.cfg, runner=fake
        )
        self.assertFalse(result["maintenance"])
        self.assertIn("sleep", called)
        self.assertIn("sleep", [item["kind"] for item in result["ran"]])
        # 跑成功了才把 next_run_at 推到下一天
        self.assertGreater(jobs.get(self.app.db, "sleep")["next_run_at"], scheduler.to_iso(self.t0))

    def test_recover_stale_still_happens_in_window(self) -> None:
        """闸门只拦「执行」，不拦自愈：卡在 running 的任务要能被放回 pending。"""
        scheduler.ensure_jobs(self.app.db, now=self.t0, config=self.cfg)
        self.app.db.conn.execute(
            "UPDATE job SET state='running', updated_at=?, next_run_at=? WHERE id='sched:sleep'",
            ("2026-01-01T00:00:00.000Z", scheduler.to_iso(self.t0 - timedelta(minutes=1))),
        )
        maintenance.mark(self.app.db, config=self.cfg, minutes=30, at=self.t0)
        result = scheduler.tick(
            self.app.db,
            now=self.t0 + timedelta(minutes=5),
            config=self.cfg,
            runner=lambda *a, **k: {"ok": True},
        )
        self.assertTrue(result["maintenance"])
        self.assertEqual(result["recovered"], ["sched:sleep"])
        self.assertEqual(jobs.get(self.app.db, "sleep")["state"], "pending")

    # -- 手动命令不受影响 ---------------------------------------------------
    def test_manual_cli_backup_works_during_maintenance(self) -> None:
        maintenance.mark(self.app.db, config=self.cfg, reason="迁移窗口", minutes=30, at=self.t0)
        code, out = run_cli(["--db", str(self.path), "backup", "run"])
        self.assertEqual(code, 0, out)
        self.assertIn("✓ 备份", out)
        self.assertIn("kind=manual", out)
        rows = self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE kind='manual'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(Path(rows[0]["path"]).exists())
        # 手动命令跑完后窗口仍然在（备份不会把闸门关掉）
        self.assertTrue(maintenance.active(self.app.db, config=Config())["active"])

    # -- CLI：on / off / status / daemon --once ----------------------------
    def test_cli_on_off_status(self) -> None:
        code, out = run_cli(
            ["--db", str(self.path), "--json", "maintenance", "on",
             "--minutes", "7", "--reason", "回归验证"]
        )
        self.assertEqual(code, 0, out)
        data = json.loads(out)["data"]
        self.assertTrue(data["active"])
        self.assertEqual(data["reason"], "回归验证")
        self.assertEqual(data["minutes"], 7)

        code, out = run_cli(["--db", str(self.path), "--json", "maintenance", "status"])
        self.assertTrue(json.loads(out)["data"]["active"])

        code, out = run_cli(["--db", str(self.path), "--json", "maintenance", "off"])
        self.assertTrue(json.loads(out)["data"]["ok"])
        self.assertFalse(maintenance.active(self.app.db, config=self.cfg)["active"])

        code, out = run_cli(["--db", str(self.path), "--json", "maintenance", "off"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["data"]["cleared"])

    def test_daemon_once_reports_maintenance(self) -> None:
        self._due_sleep_job()
        maintenance.mark(self.app.db, config=self.cfg, reason="人工迁库", minutes=30, at=self.t0)

        code, out = run_cli(["--db", str(self.path), "--json", "daemon", "--once"])
        self.assertEqual(code, 0, out)
        data = json.loads(out)["data"]
        self.assertTrue(data["maintenance"])
        self.assertEqual(data["reason"], "人工迁库")
        self.assertEqual(data["ran"], [])
        self.assertIn("sleep", data["skipped"])

        # 人话输出也要一眼看见在维护（计划任务日志排查用）
        code, out = run_cli(["--db", str(self.path), "daemon", "--once"])
        self.assertEqual(code, 0)
        self.assertIn("维护窗口", out)
        self.assertIn("maintenance=true", out)


if __name__ == "__main__":
    unittest.main()
