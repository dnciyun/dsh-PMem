"""备份卫生（A 物理擦除）：备份里不能残留已删内容的字节。

两个断言来源不同：
1. ``VACUUM INTO`` 生成的备份文件**字节里**搜不到 hard delete 掉的密语；
2. ``Database.vacuum()`` 之后主库文件字节里也搜不到（物理擦除），freelist 归零。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.gate.sdk import PMem
from pmem.ops import backup

from .support import drop_dir, temp_dir

SECRET = "唯一密语-ZEBRA-243917-物理擦除"
KEEP = "这条普通记忆要保留下来"


class BackupHygieneCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("backup-hygiene")
        self.db_path = Path(self._dir) / "pmem.db"
        self.cfg = Config({"runtime": {"backup_dir": str(Path(self._dir) / "backups")}})
        self.app = PMem.open(self.db_path)

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _seed_secret_then_delete(self) -> str:
        self.app.store.add(KEEP)
        saved = self.app.store.add(f"这是秘密内容：{SECRET}")
        self.app.delete(saved["id"], hard=True, reason="测试物理擦除")
        return saved["id"]

    def _backup(self, **kwargs: object) -> dict:
        return backup.run(self.app.db, config=self.cfg, **kwargs)

    def test_backup_file_has_no_deleted_bytes(self) -> None:
        self._seed_secret_then_delete()
        result = self._backup()
        self.assertTrue(result["ok"], result)
        path = Path(result["path"])
        raw = path.read_bytes()
        self.assertNotIn(SECRET.encode("utf-8"), raw, "备份文件里还能翻出已删密语")
        self.assertEqual(result["memory_count"], 1)
        self.assertTrue(result["verified"])
        names = {item["name"] for item in result["checks"]}
        self.assertIn("integrity_check", names)
        self.assertIn("event_chain", names)

    def test_vacuum_erases_deleted_bytes_and_freelist(self) -> None:
        self._seed_secret_then_delete()
        before = self.app.db.vacuum()
        self.assertTrue(before["ok"], before)
        for key in (
            "before_bytes",
            "after_bytes",
            "before_pages",
            "after_pages",
            "freelist_before",
            "freelist_after",
            "wal_before_bytes",
            "wal_after_bytes",
            "duration_ms",
        ):
            self.assertIn(key, before)
        self.assertEqual(before["freelist_after"], 0, "VACUUM 后 freelist 应归零")
        self.assertTrue(self.app.db.integrity_ok())
        raw = self.db_path.read_bytes()
        self.assertNotIn(SECRET.encode("utf-8"), raw, "VACUUM 后主库文件里仍能翻出密语")
        # 保留的那条还在，检索也还活着
        self.assertIn(KEEP, self.app.export())

    def test_read_only_vacuum_is_refused(self) -> None:
        app = PMem.open(self.db_path, read_only=True)
        try:
            result = app.db.vacuum()
        finally:
            app.close()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "read-only")

    def test_backup_kind_is_passed_through(self) -> None:
        self.app.store.add(KEEP)
        daily = self._backup(kind="daily")
        self.assertEqual(daily["kind"], "daily")
        manual = self._backup(kind="nonsense")
        self.assertEqual(manual["kind"], "manual")
        row = self.app.db.conn.execute(
            "SELECT kind FROM snapshot WHERE id=?", (daily["snapshot_id"],)
        ).fetchone()
        self.assertEqual(row["kind"], "daily")

    def test_backup_anchor_is_optional_and_harmless(self) -> None:
        self.app.store.add(KEEP)
        result = self._backup()
        # W2 的 anchors 可能还没接线：返回 None 或 dict 都允许，但不能让备份失败。
        self.assertTrue(result["ok"])
        self.assertIn("anchor", result)
        self.assertTrue(Path(result["path"]).exists())


class VacuumMetricsCase(unittest.TestCase):
    """vacuum 的返回值语义：内存库可用、前后页数可读、analyze 开关生效。"""

    def test_memory_database_vacuum(self) -> None:
        db = Database(":memory:")
        db.init()
        result = db.vacuum()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["before_bytes"], 0)
        self.assertEqual(result["after_bytes"], 0)
        self.assertIn("before_pages", result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
