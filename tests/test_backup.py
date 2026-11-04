"""M9.9 备份保留：manual / daily 分级轮转（monthly、pre-migrate 不删）。

背景：daemon 04:00 的定时备份原来被标成 ``manual``，而 ``prune`` 只清 manual，
于是真正的日快照既没被标记、也没有 7 天保留。这里锁住三件事：

1. 定时任务写的是 ``kind='daily'``（scheduler.default_runner）；
2. ``daily`` 按 ``runtime.daily_keep``（默认 7）轮转，文件与表行一起清；
3. ``manual`` 仍按 ``runtime.backup_keep``（默认 14，``keep=`` 只覆盖这一类），
   ``monthly`` / ``pre-migrate`` 一条都不动。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.base.config import Config
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.ops import backup, scheduler

from .support import drop_dir, temp_dir

KINDS = ("daily", "monthly", "manual", "pre-migrate")


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class BackupPruneCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("backup")
        self.path = Path(self._dir) / "pmem.db"
        self.backup_dir = Path(self._dir) / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.app = PMem.open(self.path)
        self.cfg = Config(
            {
                "runtime": {
                    "backup_dir": str(self.backup_dir),
                    "backup_keep": 14,
                    "daily_keep": 7,
                }
            }
        )

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    # -- 构造 ---------------------------------------------------------------
    def _plant(self, kind: str, count: int, *, tag: str = "") -> list[Path]:
        """直接造 snapshot 行 + 同名假文件（不动真备份，秒级造出 9 条）。"""
        assert kind in KINDS
        paths: list[Path] = []
        for i in range(count):
            path = self.backup_dir / f"fake-{kind}-{tag}{i:02d}.db"
            path.write_bytes(b"fake-snapshot")
            paths.append(path)
            self.app.db.conn.execute(
                "INSERT INTO snapshot(id, created_at, path, size_bytes, sha256, schema_version, "
                "memory_count, verified_at, kind) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"snap-{kind}-{tag}{i:02d}",
                    f"2026-10-{i + 1:02d}T04:00:00.000Z",
                    str(path),
                    len(b"fake-snapshot"),
                    "0" * 64,
                    "test",
                    1,
                    f"2026-10-{i + 1:02d}T04:00:01.000Z",
                    kind,
                ),
            )
        return paths

    def _rows(self, kind: str) -> list:
        return self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE kind=? ORDER BY rowid", (kind,)
        ).fetchall()

    def _plant_path(self, kind: str, name: str, snapshot_id: str) -> Path:
        """按指定文件名造一条快照（用于分辨日快照的「生产线」前缀）。"""
        path = self.backup_dir / name
        path.write_bytes(b"fake-snapshot")
        self.app.db.conn.execute(
            "INSERT INTO snapshot(id, created_at, path, size_bytes, sha256, schema_version, "
            "memory_count, verified_at, kind) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                "2026-10-01T04:00:00.000Z",
                str(path),
                len(b"fake-snapshot"),
                "0" * 64,
                "test",
                1,
                "2026-10-01T04:00:01.000Z",
                kind,
            ),
        )
        return path

    # -- daily 保留 ---------------------------------------------------------
    def test_daily_keeps_latest_seven(self) -> None:
        daily_paths = self._plant("daily", 9)
        manual_paths = self._plant("manual", 2)
        removed = backup.prune(self.app.db, config=self.cfg)

        rows = self._rows("daily")
        self.assertEqual(len(rows), 7, [row["id"] for row in rows])
        self.assertEqual([row["id"] for row in rows], [f"snap-daily-{i:02d}" for i in range(2, 9)])
        self.assertEqual(len(removed), 2)
        # 文件与表行一起清：最老两份没了，剩下 7 份还在
        self.assertFalse(daily_paths[0].exists())
        self.assertFalse(daily_paths[1].exists())
        self.assertTrue(all(path.exists() for path in daily_paths[2:]))
        # manual 只有 2 份，远没到 14，一个都不许动
        self.assertEqual(len(self._rows("manual")), 2)
        self.assertTrue(all(path.exists() for path in manual_paths))

    def test_daily_keep_is_configurable(self) -> None:
        self._plant("daily", 5)
        cfg = Config({"runtime": {"backup_dir": str(self.backup_dir), "daily_keep": 3}})
        backup.prune(self.app.db, config=cfg)
        self.assertEqual(len(self._rows("daily")), 3)

    def test_daily_pools_rotate_separately(self) -> None:
        """调度器离线备份（pmem-backup-*）与睡眠每日副本（pmem-YYYYMMDD）分池各留 7 份。

        混在一个池子里轮转的话，一天各产一份、7 份就只剩 3.5 天——两条安全网互相挤掉。
        """
        sched = [self._plant_path("daily", f"pmem-backup-2026100{i}-x.db", f"snap-sched-{i}") for i in range(9)]
        sleep = [self._plant_path("daily", f"pmem-2026100{i}.db", f"snap-sleep-{i}") for i in range(9)]
        removed = backup.prune(self.app.db, config=self.cfg)

        rows = self._rows("daily")
        self.assertEqual(len(rows), 14, [row["id"] for row in rows])
        self.assertEqual({row["id"] for row in rows}, {f"snap-sched-{i}" for i in range(2, 9)} | {f"snap-sleep-{i}" for i in range(2, 9)})
        self.assertEqual(len(removed), 4)
        self.assertFalse(sched[0].exists())
        self.assertFalse(sleep[0].exists())
        self.assertTrue(sched[-1].exists() and sleep[-1].exists())

    def test_manual_uses_backup_keep_and_keep_param(self) -> None:
        self._plant("manual", 5)
        self._plant("daily", 9)
        # keep= 只覆盖 manual；daily 仍按 daily_keep=7
        removed = backup.prune(self.app.db, config=self.cfg, keep=2)
        self.assertEqual(len(self._rows("manual")), 2)
        self.assertEqual(len(self._rows("daily")), 7)
        self.assertEqual(len(removed), 3 + 2)

    # -- 其它 kind 不删 ----------------------------------------------------
    def test_monthly_and_pre_migrate_untouched(self) -> None:
        self._plant("monthly", 4)
        self._plant("pre-migrate", 3)
        self._plant("daily", 9)
        self._plant("manual", 20)
        backup.prune(self.app.db, config=self.cfg)
        self.assertEqual(len(self._rows("monthly")), 4)
        self.assertEqual(len(self._rows("pre-migrate")), 3)

    # -- 计数 --------------------------------------------------------------
    def test_counts_by_kind(self) -> None:
        self._plant("daily", 3)
        self._plant("manual", 2)
        self._plant("monthly", 1)
        counts = backup.counts(self.app.db)
        self.assertEqual(counts["daily"], 3)
        self.assertEqual(counts["manual"], 2)
        self.assertEqual(counts["monthly"], 1)
        self.assertEqual(counts["total"], 6)

    # -- 真备份：kind 落库 + 定时任务标 daily ------------------------------
    def test_run_records_requested_kind(self) -> None:
        self.app.add("保留策略测试记忆", kind="semantic", provenance="user_explicit")
        result = backup.run(self.app.db, config=self.cfg, kind="daily", tag="scheduled")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "daily")
        row = self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE id=?", (result["snapshot_id"],)
        ).fetchone()
        self.assertEqual(row["kind"], "daily")
        event = self.app.db.conn.execute(
            "SELECT payload_json FROM event_log WHERE op='ops.backup' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["tag"], "scheduled")

    def test_scheduler_backup_runner_marks_daily(self) -> None:
        """定时备份必须是 daily，否则永远进不了 daily 轮转。"""
        self.app.add("定时任务 kind 测试记忆", kind="semantic", provenance="user_explicit")
        result = scheduler.default_runner(self.app.db, self.cfg, "self", "backup", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "daily")
        row = self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE id=?", (result["snapshot_id"],)
        ).fetchone()
        self.assertEqual(row["kind"], "daily")
        # 不传 kind 的默认仍是 manual（CLI 手动备份的语义不变）
        manual = backup.run(self.app.db, config=self.cfg)
        manual_row = self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE id=?", (manual["snapshot_id"],)
        ).fetchone()
        self.assertEqual(manual_row["kind"], "manual")

    def test_run_leaves_no_sqlite_sidecars(self) -> None:
        """校验时的只读连接会建 -shm/-wal；备份目录里不该留下这种垃圾。"""
        self.app.add("sidecar 测试记忆", kind="semantic", provenance="user_explicit")
        for _ in range(3):
            backup.run(self.app.db, config=self.cfg, kind="daily", tag="scheduled")
        leftovers = [p.name for p in self.backup_dir.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftovers, [])
        sidecars = [
            p.name
            for p in self.backup_dir.iterdir()
            if p.name.endswith(("-wal", "-shm", "-journal"))
        ]
        self.assertEqual(sidecars, [])


class BackupCliCase(unittest.TestCase):
    """``pmem backup list/counts`` 要能看见 daily 的保留现状。"""

    def setUp(self) -> None:
        self._dir = temp_dir("backupcli")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)
        self.app.add("备份 CLI 测试记忆", kind="semantic", provenance="user_explicit")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_counts_and_list_show_daily(self) -> None:
        backup.run(self.app.db, kind="daily", tag="scheduled")
        backup.run(self.app.db, kind="daily", tag="scheduled")
        backup.run(self.app.db)  # 默认 manual

        code, out = run_cli(["--db", str(self.path), "--json", "backup", "counts"])
        self.assertEqual(code, 0, out)
        counts = json.loads(out)["data"]["counts"]
        self.assertEqual(counts["daily"], 2)
        self.assertEqual(counts["manual"], 1)
        self.assertEqual(counts["total"], 3)

        code, out = run_cli(["--db", str(self.path), "backup", "list"])
        self.assertEqual(code, 0, out)
        self.assertIn("daily 2", out)
        self.assertIn("manual 1", out)
        self.assertIn("保留策略", out)

        code, out = run_cli(["--db", str(self.path), "backup", "counts"])
        self.assertEqual(code, 0, out)
        self.assertIn("合计 3 份", out)


if __name__ == "__main__":
    unittest.main()
