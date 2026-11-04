"""软删 = 停放：行留着、分组留着、列表里不出现。

I10 已经规定「软删记忆不出现在任何默认召回」；这里守住同一条口径的另外两面：
显示侧不许漏出来，I17 也不许把「停着」当成「没了」。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.gate.sdk import PMem
from pmem.memory import groups
from pmem.ops import invariants

from .support import drop_dir, temp_dir


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("archive")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def checks(self) -> dict[str, dict]:
        return {r["id"]: r for r in invariants.check_all(self.db)}

    def memory_row(self, memory_id: str) -> object:
        return self.db.conn.execute(
            "SELECT status, deleted_at FROM memory WHERE id=?", (memory_id,)
        ).fetchone()

    def group_id(self, path: str) -> str:
        row = self.db.conn.execute("SELECT id FROM grp WHERE path=?", (path,)).fetchone()
        return row["id"]


class TestSoftDeleteParks(Case):
    def test_soft_delete_keeps_membership_and_grade(self) -> None:
        mem = self.store.add("停一笔", groups=["我/停机坪"], grade="B")
        self.store.delete(mem["id"])
        self.assertEqual(groups.groups_for(self.db, "memory", mem["id"]), ["我/停机坪"])
        self.assertEqual(groups.grade_of(self.db, "memory", mem["id"]), "B")

    def test_group_listing_hides_parked_memory(self) -> None:
        mem = self.store.add("停一笔", groups=["我/停机坪"], grade="B")
        self.store.delete(mem["id"])
        self.assertEqual(groups.targets(self.db, "self", "我/停机坪"), [])
        self.assertIsNotNone(self.memory_row(mem["id"])["deleted_at"])  # 停放时间在

    def test_group_count_skips_parked_members(self) -> None:
        self.store.add("留着", groups=["我/停机坪"], grade="B")
        gone = self.store.add("停着", groups=["我/停机坪"], grade="B")
        self.store.delete(gone["id"])
        row = [r for r in groups.tree(self.db, "self") if r["path"] == "我/停机坪"][0]
        self.assertEqual(row["members"], 1)

    def test_i17_tolerates_parked_member(self) -> None:
        mem = self.store.add("停一笔", groups=["我/停机坪"], grade="B")
        self.store.delete(mem["id"])
        check = self.checks()["I17"]
        self.assertTrue(check["ok"], check["detail"])
        self.assertIn("停放", check["detail"])

    def test_i17_still_catches_missing_target(self) -> None:
        groups.ensure(self.db, "self", "我/停机坪")
        self.db.conn.execute(
            "INSERT INTO grp_member(id, grp_id, target_tbl, target_id, assigned_by, created_at) "
            "VALUES ('gm_dangling', ?, 'memory', 'not-a-real-memory', 'ai', '2026-01-01T00:00:00.000Z')",
            (self.group_id("我/停机坪"),),
        )
        check = self.checks()["I17"]
        self.assertFalse(check["ok"])
        self.assertIn("1 条成员指向已消失的目标", check["detail"])

    def test_hard_delete_leaves_nothing_behind(self) -> None:
        mem = self.store.add("真删", groups=["我/停机坪"], grade="B")
        self.store.delete(mem["id"], hard=True)
        self.assertEqual(groups.groups_for(self.db, "memory", mem["id"]), [])
        self.assertTrue(self.checks()["I17"]["ok"])


class TestRestore(Case):
    def test_restore_clears_deleted_at_and_returns_to_group(self) -> None:
        mem = self.store.add("捞回来", groups=["我/停机坪"], grade="B")
        self.store.delete(mem["id"])
        self.store.update(mem["id"], set_fields={"status": "active"})
        row = self.memory_row(mem["id"])
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["deleted_at"])
        listed = groups.targets(self.db, "self", "我/停机坪")
        self.assertEqual([t["id"] for t in listed], [mem["id"]])

    def test_setting_status_deleted_stamps_time_and_keeps_group(self) -> None:
        mem = self.store.add("再停一次", groups=["我/停机坪"], grade="B")
        self.store.update(mem["id"], set_fields={"status": "deleted"})
        row = self.memory_row(mem["id"])
        self.assertEqual(row["status"], "deleted")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(groups.groups_for(self.db, "memory", mem["id"]), ["我/停机坪"])

    def test_plain_update_does_not_touch_deleted_at(self) -> None:
        mem = self.store.add("活着", groups=["我/停机坪"], grade="B")
        self.store.update(mem["id"], set_fields={"importance": 0.5})
        self.assertIsNone(self.memory_row(mem["id"])["deleted_at"])


if __name__ == "__main__":
    unittest.main()
