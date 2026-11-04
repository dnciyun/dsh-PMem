"""公平车道 + 确定性浮现。

两件事守的是同一个东西：**「想不起来」不应该是随机的**。

- 公平车道：只要记忆还在库里，总有一天会被提起来，哪怕它的分数永远够不到门限；
- 确定性：同一份库、同一句话，两次调用必须给出同样的结果——否则评测不可复现。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base import clock
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

T0 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("fair")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.db = self.app.db
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def age(self, days: int) -> None:
        clock.set_now(T0 + timedelta(days=days))
        for _ in range(3):  # 一次 tick 只走一档：active → dormant → archived
            self.app.lifecycle()

    def surface(self, session: str, cue: str = "今天天气不错") -> list[dict]:
        return self.app.surface(session_id=session, cue=cue, mode="turn")["data"]["items"]


class TestFairLane(Case):
    def test_archived_memory_still_gets_recalled(self) -> None:
        mem = self.store.add("旧事：小学门口的文具店", groups=["我/旧事"], grade="B")
        self.age(400)
        row = self.db.conn.execute("SELECT status FROM memory WHERE id=?", (mem["id"],)).fetchone()
        self.assertEqual(row["status"], "archived", "前提：这条已经沉到最深一档")
        items = self.surface("s1")
        self.assertEqual([item["reason"] for item in items], ["fair"])
        self.assertEqual(items[0]["id"], mem["id"])

    def test_we_group_is_never_pushed_by_fair_lane(self) -> None:
        self.store.add("我们/初衷：写这个是为了让上下文清空后还能继续", groups=["我们/初衷"], grade="A")
        self.age(400)
        self.assertEqual(self.surface("s1"), [])

    def test_low_grade_d_is_not_pushed(self) -> None:
        self.store.add("D 级琐事：昨天吃了什么", groups=["我/杂事"], grade="D")
        self.age(400)
        self.assertEqual(self.surface("s1"), [])

    def test_recent_memory_is_not_pushed(self) -> None:
        self.store.add("刚说过的事：明天开会", groups=["工作/待办"], grade="B")
        self.assertEqual(self.surface("s1"), [])

    def test_pinned_is_not_pushed(self) -> None:
        self.store.add("钉住的事：每天要吃药", groups=["我/健康"], grade="B", pinned=True)
        self.age(400)
        self.assertEqual(self.surface("s1"), [])


class TestDeterminism(Case):
    def test_same_input_same_ids(self) -> None:
        for index in range(4):
            self.store.add(f"第 {index} 条：周末去爬山", groups=["我/日常"], grade="B")
        first = [item["id"] for item in self.surface("s1", "爬山")]
        second = [item["id"] for item in self.surface("s2", "爬山")]
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_same_input_after_restart_of_session_state(self) -> None:
        self.store.add("登山杖放在阳台柜子里", groups=["我/物品"], grade="A")
        first = self.surface("s1", "登山杖")
        second = self.surface("s2", "登山杖")
        self.assertEqual([i["id"] for i in first], [i["id"] for i in second])
        self.assertEqual([i["reason"] for i in first], [i["reason"] for i in second])


if __name__ == "__main__":
    unittest.main()
