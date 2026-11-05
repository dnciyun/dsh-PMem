"""恢复（A.3）：维护窗口、安全前置备份、原子替换、校验失败不碰活库。"""

from __future__ import annotations

import shutil
import sqlite3
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.gate.sdk import PMem
from pmem.ops import backup, maintenance, restore

from .support import drop_dir, temp_dir


class RestoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("restore")
        self.db_path = Path(self._dir) / "pmem.db"
        self.cfg = Config(
            {
                "runtime": {
                    "backup_dir": str(Path(self._dir) / "backups"),
                    "maintenance_file": str(Path(self._dir) / "maintenance.json"),
                }
            }
        )
        app = PMem.open(self.db_path)
        try:
            for index in range(3):
                app.store.add(f"恢复测试记忆 {index}")
            self.backup = backup.run(app.db, config=self.cfg)
        finally:
            app.close()

    def tearDown(self) -> None:
        drop_dir(self._dir)

    # -- helpers -----------------------------------------------------------
    def _open(self) -> PMem:
        app = PMem.open(self.db_path)
        self.addCleanup(app.close)
        return app

    def _memory_count(self) -> int:
        app = self._open()
        try:
            return int(app.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"])
        finally:
            app.close()

    def _add_one(self) -> None:
        app = PMem.open(self.db_path)
        try:
            app.store.add("备份之后新增的临时记忆")
        finally:
            app.close()

    def _mark_window(self, minutes: int = 5) -> dict:
        db = Database(str(self.db_path))
        try:
            return maintenance.mark(db, config=self.cfg, reason="restore test", minutes=minutes)
        finally:
            db.close()

    # -- tests -------------------------------------------------------------
    def test_requires_maintenance_window(self) -> None:
        self._add_one()
        before = self._memory_count()
        result = restore.restore_backup(self.db_path, self.backup["path"], config=self.cfg)
        self.assertFalse(result["ok"], result)
        self.assertIn("维护窗口", result["error"])
        self.assertEqual(self._memory_count(), before, "拒绝时不能动活库")

    def test_force_skips_window_and_replaces(self) -> None:
        self._add_one()
        result = restore.restore_backup(self.db_path, self.backup["path"], config=self.cfg, force=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["replaced"])
        self.assertEqual(result["restored_counts"]["memory"], 3)
        self.assertEqual(self._memory_count(), 3, "恢复后应回到备份时的条数")
        self.assertIsNotNone(result["pre_restore"])
        self.assertTrue(Path(result["pre_restore"]).exists(), "恢复前安全备份必须留下")
        pre = Database(str(result["pre_restore"]), read_only=True)
        try:
            self.assertTrue(pre.integrity_ok())
        finally:
            pre.close()
        self.assertTrue(Path(self.backup["path"]).exists(), "恢复不应吃掉备份文件本身")
        names = {item["name"] for item in result["checks"]}
        self.assertIn("pre_restore_backup", names)
        self.assertIn("restored_integrity", names)
        self.assertIn("restored_event_chain", names)

    def test_marked_window_allows_restore(self) -> None:
        self._add_one()
        window = self._mark_window()
        self.assertTrue(window["active"])
        result = restore.restore_backup(self.db_path, self.backup["path"], config=self.cfg)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._memory_count(), 3)

    def test_missing_backup_is_refused(self) -> None:
        result = restore.restore_backup(self.db_path, Path(self._dir) / "nope.db", config=self.cfg, force=True)
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_corrupt_backup_is_refused(self) -> None:
        corrupt = Path(self._dir) / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")
        result = restore.restore_backup(self.db_path, corrupt, config=self.cfg, force=True)
        self.assertFalse(result["ok"])
        self.assertIn("不可读", result["error"])
        self.assertEqual(self._memory_count(), 3, "坏备份不能动活库")

    def test_newer_schema_backup_is_refused(self) -> None:
        newer = Path(self._dir) / "newer.db"
        shutil.copy2(self.backup["path"], newer)
        conn = sqlite3.connect(str(newer))
        try:
            conn.execute("UPDATE schema_meta SET value='99.0.0' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        result = restore.restore_backup(self.db_path, newer, config=self.cfg, force=True)
        self.assertFalse(result["ok"], result)
        self.assertIn("高于", result["error"])
        self.assertEqual(self._memory_count(), 3, "拒绝时不能动活库")

    def test_inspect_backup_reports_chain_and_counts(self) -> None:
        info = restore.inspect_backup(self.backup["path"])
        self.assertTrue(info["integrity_ok"])
        self.assertTrue(info["chain_ok"])
        self.assertEqual(info["counts"]["memory"], 3)
        self.assertGreater(info["counts"]["event"], 0)
        self.assertTrue(info["schema_version"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
