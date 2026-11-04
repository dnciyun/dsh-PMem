"""日记派生事实的「半截失败 → 补齐」（1.11.0 / I20）。

守的是审计里发现的那个真洞：``digest`` 先提交 episode + 把流水标记成已消费，
再在事务外逐条写派生事实。第 2 条失败之后：

1. 流水全部 ``episode_id IS NOT NULL`` → 重跑 ``digest`` 只得到 ``no_lines``；
2. 缺的事实没有事件、没有体检项 → 永久丢失且全绿。

现在：``_finish`` 自己会按幂等键重放；``journal.repair`` 可重入；``I20`` 会红；
CLI ``journal repair --dry-run`` 只报告不写库。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base import clock
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.ops import invariants

from .support import drop_dir, temp_dir

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)

LINES = [
    ("user", "我昨晚看了石头门第十二集"),
    ("user", "今天中午吃了牛肉面"),
    ("user", "我在杭州做前端开发"),
]


class RepairCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("journal-repair")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)
        self.db = self.app.db
        self.store = self.app.store
        clock.set_now(T0)
        for index, (role, text) in enumerate(LINES):
            journal.append(
                self.db,
                text,
                role=role,
                session_id="s1",
                ts=(T0 + timedelta(minutes=index)).isoformat(),
            )

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    # -- 辅助 --------------------------------------------------------------
    def digest_failing_from(self, fail_from: int, *, restore: bool = True) -> dict:
        """让第 ``fail_from`` 条事实之后的 ``store.add`` 抛错，跑一次 digest。"""
        original = self.store.add
        calls = {"n": 0}

        def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            calls["n"] += 1
            if calls["n"] > fail_from:
                raise RuntimeError("模拟第 2 条事实写入失败")
            return original(*args, **kwargs)

        self.store.add = boom
        try:
            result = journal.digest(self.db, store=self.store, config=self.app.config)
        finally:
            if restore:
                self.store.add = original
        return result

    def counts(self) -> dict:
        return {
            "memories": self.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"],
            "episode": self.db.conn.execute(
                "SELECT memory_count FROM episode ORDER BY rowid DESC LIMIT 1"
            ).fetchone()["memory_count"],
            "idem": self.db.conn.execute("SELECT COUNT(*) c FROM idem").fetchone()["c"],
        }

    def checks(self) -> dict:
        return {item["id"]: item for item in invariants.check_all(self.db)}

    # -- 1. 半截失败 → repair 补齐 → 幂等 ----------------------------------
    def test_partial_digest_then_repair(self) -> None:
        result = self.digest_failing_from(1)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 3)
        # 第 1 条写成，第 2/3 条失败：流水已经标记消费，重跑 digest 只会 no_lines。
        self.assertEqual(self.counts()["memories"], 1)
        self.assertEqual(self.counts()["episode"], 3)
        self.assertEqual(len(result["memory_ids"]), 1)
        self.assertEqual(result["incomplete"], 2)
        self.assertTrue(any("journal repair" in str(err.get("hint", "")) for err in result["errors"]))
        self.assertFalse(self.checks()["I20"]["ok"])

        # 原料还在 → repair 补齐
        fixed = journal.repair(self.db, store=self.store)
        self.assertEqual(fixed["episodes"], 1)
        self.assertEqual(fixed["gaps"], 1)
        self.assertEqual(fixed["missing"], 2)
        self.assertEqual(fixed["added"], 2)
        self.assertEqual(fixed["failed"], 0)
        self.assertEqual(fixed["remaining"], 0)
        self.assertEqual(self.counts()["memories"], 3)
        self.assertTrue(self.checks()["I20"]["ok"])

        # 再跑一次：幂等，什么都不补
        again = journal.repair(self.db, store=self.store)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["missing"], 0)
        self.assertEqual(again["gaps"], 0)
        self.assertEqual(self.counts()["memories"], 3)

    # -- 2. I20：memory_count 被改错就红 -----------------------------------
    def test_i20_catches_wrong_memory_count(self) -> None:
        journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertEqual(self.counts()["memories"], 3)
        self.assertTrue(self.checks()["I20"]["ok"])

        episode_id = self.db.conn.execute("SELECT id FROM episode").fetchone()["id"]
        self.db.conn.execute("UPDATE episode SET memory_count=5 WHERE id=?", (episode_id,))
        check = self.checks()["I20"]
        self.assertFalse(check["ok"])
        self.assertIn("I20", check["id"])
        self.assertIn("缺 2 条", check["detail"])

        # 补回真相 → 又绿
        self.db.conn.execute("UPDATE episode SET memory_count=3 WHERE id=?", (episode_id,))
        self.assertTrue(self.checks()["I20"]["ok"])

    # -- 3. 写过、后来被真删：不是「半截」，而且不复活 ----------------------
    def test_repair_does_not_resurrect_deleted_fact(self) -> None:
        journal.digest(self.db, store=self.store, config=self.app.config)
        row = self.db.conn.execute(
            "SELECT id FROM memory ORDER BY rowid LIMIT 1"
        ).fetchone()
        self.app.delete(row["id"], hard=True, reason="用户要求真删")

        # 幂等键还在 = 派生过（后来被删），不是半截失败 → repair 根本不进这篇
        result = journal.repair(self.db, store=self.store)
        self.assertEqual(result["gaps"], 0)
        self.assertEqual(result["added"], 0)
        self.assertEqual(self.counts()["memories"], 2)  # 没有被写回来
        self.assertTrue(self.checks()["I20"]["ok"])

    # -- 3b. 既缺、又有一条被删：补缺的，但不复活删掉的 -------------------
    def test_deleted_fact_is_not_resurrected_while_repairing_gap(self) -> None:
        self.digest_failing_from(1)  # 1 条写成，2 条失败
        row = self.db.conn.execute(
            "SELECT id FROM memory ORDER BY rowid LIMIT 1"
        ).fetchone()
        self.app.delete(row["id"], hard=True, reason="用户要求真删")

        fixed = journal.repair(self.db, store=self.store)
        self.assertEqual(fixed["gaps"], 1)
        self.assertEqual(fixed["dropped"], 1)  # 写过、被删：记一笔但不复活
        self.assertEqual(fixed["missing"], 2)
        self.assertEqual(fixed["added"], 2)
        self.assertEqual(fixed["remaining"], 0)
        self.assertEqual(self.counts()["memories"], 2)  # 补的 2 条，删的没回来
        self.assertTrue(self.checks()["I20"]["ok"])

    # -- 4. 完整 digest 不该产生 repair/incomplete 噪音 --------------------
    def test_complete_digest_is_quiet(self) -> None:
        result = journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertNotIn("errors", result)
        self.assertNotIn("incomplete", result)
        self.assertNotIn("repair", result)
        self.assertNotIn("repaired_previous", result)
        self.assertEqual(len(result["memory_ids"]), 3)
        self.assertTrue(self.checks()["I20"]["ok"])

    # -- 5. CLI repair --dry-run：只报告、不写库 ---------------------------
    def test_cli_repair_dry_run(self) -> None:
        self.digest_failing_from(1)  # 留下 1/3 的半截日记
        before = self.counts()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(
                ["--db", str(self.path), "--json", "journal", "repair", "--dry-run"]
            )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        data = payload["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["episodes"], 1)
        self.assertEqual(data["gaps"], 1)
        self.assertEqual(data["missing"], 2)
        self.assertEqual(data["added"], 0)
        self.assertEqual(data["remaining"], 1)
        self.assertEqual(self.counts(), before)  # 干跑一个字没写

        # 人读输出也 smoke 一下
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--db", str(self.path), "journal", "repair", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("查了 1 篇日记", out.getvalue())


if __name__ == "__main__":
    unittest.main()
