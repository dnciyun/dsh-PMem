"""浮现不能端出「撤回」或「被去重吸收」的记忆。

审计发现：节律车道 / 公平车道的候选查询把 ``archived`` 也算作可浮现状态
（刻意如此——沉睡太久的老记忆该被捞一次），但 ``retract`` 的落地状态正是
``archived``，去重吸收的行也是 ``archived`` + ``canonical_id``。于是撤回过的
A 级记忆、被合并掉的重复行都能从这两条车道"复活"。

这里守两条：

1. 撤回态（``archived`` + ``memory.retract`` 事件）不进节律/公平车道；
2. 被吸收行（``canonical_id`` 非空）不进节律/公平车道；
   代表行照常浮现，恢复成 active 的撤回记忆也照常浮现。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from pmem.base.clock import now, to_iso
from pmem.gate.sdk import PMem
from pmem.memory import dup

from .support import drop_dir, temp_dir


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("surface-retracted")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _add(self, text: str, grade: str = "A", *, days: int = 120) -> str:
        created = to_iso(now() - timedelta(days=days))
        return self.store.add(text, groups=["工作/PMem"], grade=grade, created_at=created)["id"]

    def _turn(self, session: str) -> list[str]:
        result = self.app.surface(session_id=session, mode="turn")
        self.assertTrue(result["ok"], result)
        return [item["id"] for item in result["data"]["items"]]

    def _status(self, memory_id: str) -> str:
        row = self.db.conn.execute("SELECT status FROM memory WHERE id=?", (memory_id,)).fetchone()
        return row["status"]


class TestRetractedNotSurfaced(Case):
    def test_retracted_not_in_rhythm_lane(self) -> None:
        # 撤回的那条更旧（R 更低）→ 修复前它会被节律车道第一个挑中，测试才咬得住
        retracted = self._add("旧的部署约定：先跑 doctor 再上", days=121)
        control = self._add("另一条旧约定：备份目录别放库里", days=120)
        self.store.retract(retracted, reason="这条不算数了")
        self.assertEqual(self._status(retracted), "archived")

        ids = self._turn("ret-rythm")
        self.assertNotIn(retracted, ids, "撤回的记忆不该被节律车道提出来")
        self.assertIn(control, ids, "节律车道本身要照常工作（别把整条车道关掉）")

    def test_retracted_not_in_fair_lane(self) -> None:
        # 400 天 → R < 0.10，节律窗口不收；只剩公平车道（闲置 ≥30 天强制提一次）
        retracted = self._add("三年前的一条琐事：那次跑基线用的是旧脚本", days=401)
        control = self._add("三年前的另一条琐事：验收前先清缓存", days=400)
        self.store.retract(retracted)
        self.assertEqual(self._status(retracted), "archived")

        ids = self._turn("ret-fair")
        self.assertNotIn(retracted, ids, "撤回的记忆不该被公平车道提出来")
        self.assertIn(control, ids, "公平车道本身要照常工作")

    def test_all_retracted_means_empty_card(self) -> None:
        only = self._add("唯一一条旧记忆，已经撤回", days=120)
        self.store.retract(only)
        self.assertEqual(self._turn("ret-only"), [])

    def test_restored_retraction_surfaces_again(self) -> None:
        """撤回是 archived+事件；update --status active 恢复后事件还在，但该重新可见。"""
        memory = self._add("旧的部署约定：先跑 doctor 再上", days=120)
        self.store.retract(memory)
        self.assertNotIn(memory, self._turn("ret-restore-1"))
        self.store.update(memory, set_fields={"status": "active"})
        self.assertIn(memory, self._turn("ret-restore-2"))

    def test_plain_archived_still_surfaces(self) -> None:
        """普通归档（没有撤回事件）不归这条过滤管：沉睡的老记忆本来就该被捞一次。"""
        memory = self._add("沉下去但没撤回的旧约定：上线前先清缓存", days=121)
        self.store.update(memory, set_fields={"status": "archived"})
        self.assertIn(memory, self._turn("plain-archived"))


class TestAbsorbedNotSurfaced(Case):
    def _absorb(self, keep_id: str, drop_id: str) -> None:
        stats = dup.merge_groups(
            self.db,
            [{"representative": keep_id, "members": [drop_id], "reason": "near"}],
            actor="test",
        )
        self.assertEqual(stats["merged"], 1)

    def test_absorbed_not_in_rhythm_lane(self) -> None:
        keeper = self._add("部署前先跑 doctor，别直接上", days=120)
        absorbed = self._add("部署前一定要先跑 doctor 再上，别直接推", days=121)
        self._absorb(keeper, absorbed)
        row = self.db.conn.execute(
            "SELECT canonical_id, status FROM memory WHERE id=?", (absorbed,)
        ).fetchone()
        self.assertEqual(row["canonical_id"], keeper)
        self.assertEqual(row["status"], "archived")

        ids = self._turn("abs-rhythm")
        self.assertNotIn(absorbed, ids, "被吸收行不该再露头（同一条事实只由代表行承担）")
        self.assertIn(keeper, ids, "代表行要照常浮现")

    def test_absorbed_not_in_fair_lane(self) -> None:
        keeper = self._add("三年前的一条琐事：那次跑基线用的是旧脚本", days=400)
        absorbed = self._add("三年前的一条琐事：那次跑基线用的是旧脚本，记一下", days=401)
        self._absorb(keeper, absorbed)
        ids = self._turn("abs-fair")
        self.assertNotIn(absorbed, ids, "被吸收行不该从公平车道出来")
        self.assertIn(keeper, ids, "代表行要照常浮现")


if __name__ == "__main__":
    unittest.main()
