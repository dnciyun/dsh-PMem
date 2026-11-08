"""M9.8b：使用语义 —— 浮现 ≠ 使用。

这一版改的是一个**定义**，不是一个数值：

- ``last_used_at`` / ``use_count``：真的被用到（显式召回 / 浮现回执 / 正面反馈）；
- ``last_surfaced_at``：只是被自动浮现出来过（每轮注入的弱信号）。

为什么非改不可：以前浮现会连 ``use_count`` 一起 +1，于是「多久没用」实际等于
「多久没被自动注入」。而插件每一轮都会调一次浮现——每条记忆天天"被用"，
生命周期永远不动，30 天日历门测出来的东西也就没有意义。

这里守四件事：

1. 浮现只推进 ``last_surfaced_at``，不碰另外两个；
2. 真正被用到（召回 / 回执）才推进 ``last_used_at``；
3. 天天被浮现但没人真用过的记忆，仍然会按时淡忘；
4. 老库（1.4.0）能升上来：补列 + 用 ``last_used_at`` 回填 + 建索引。
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from pmem.base.clock import now_iso
from pmem.gate.sdk import PMem
from pmem.memory import lifecycle, surface, usage

from .support import drop_dir, temp_dir


class UsageCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m98b")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _row(self, memory_id: str) -> dict:
        return dict(self.db.conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone())

    # ---- 浮现 -------------------------------------------------------------

    def test_surfacing_is_not_use(self) -> None:
        item = self.store.add("我们第一次一起改这个项目", groups=["我们/经历"], grade="A")
        self.assertEqual(self._row(item["id"])["use_count"], 0)

        result = self.app.surface(session_id="s1")
        self.assertTrue(result["data"]["items"], "先得真的浮现出东西来")

        row = self._row(item["id"])
        self.assertEqual(row["use_count"], 0, "浮现不许增加 use_count")
        self.assertIsNone(row["last_used_at"], "浮现不许推进 last_used_at")
        self.assertIsNotNone(row["last_surfaced_at"], "浮现要推进 last_surfaced_at")

    def test_recall_is_use(self) -> None:
        item = self.store.add("长跑训练要控制心率区间")
        self.app.search("心率", k=3)
        row = self._row(item["id"])
        self.assertGreaterEqual(row["use_count"], 1, "显式召回才算用")
        self.assertIsNotNone(row["last_used_at"])

    def test_receipt_is_use(self) -> None:
        item = self.store.add("咖啡因半衰期大约五小时")
        self.app.surface(session_id="s1")
        surface.mark_used(self.db, session_id="s1", memory_id=item["id"])
        row = self._row(item["id"])
        self.assertGreaterEqual(row["use_count"], 1, "回执才算用")
        self.assertIsNotNone(row["last_used_at"])

    # ---- 淡忘 -------------------------------------------------------------

    def test_daily_surfacing_does_not_keep_a_memory_alive(self) -> None:
        # 用 A 级 + 非「我们」分组：它够格被浮现出来，又不享受「我们」的免死金牌。
        item = self.store.add(
            "这条天天被浮现，但从来没人真的用到它", groups=["我/其他"], grade="A"
        )
        surface.card(self.db, session_id="s1", subject="self")
        row = self._row(item["id"])
        self.assertIsNotNone(row["last_surfaced_at"], "先确认它真的被浮现过")
        self.assertIsNone(row["last_used_at"])

        # 把"真用"的时间拨到很久以前，浮现时间留在刚刚。
        self.db.conn.execute(
            "UPDATE memory SET last_used_at='2020-01-01T00:00:00.000Z' WHERE id=?",
            (item["id"],),
        )
        stats = lifecycle.tick(self.db, active_dormant_days=90)
        self.assertEqual(stats["by_status"].get("dormant"), 1,
                         "被浮现不等于被使用，该沉就得沉")

    def test_recently_used_memory_stays(self) -> None:
        item = self.store.add("刚刚被认真想过的一条")
        self.db.conn.execute(
            "UPDATE memory SET last_used_at=?, last_surfaced_at=? WHERE id=?",
            (now_iso(), now_iso(), item["id"]),
        )
        stats = lifecycle.tick(self.db, active_dormant_days=90)
        self.assertEqual(stats["moved"], 0)
        self.assertEqual(self.store.get(item["id"])["status"], "active")

    # ---- 轮换（那段 SQL 有个坑） -------------------------------------------

    def test_rotation_orders_null_timestamps_last(self) -> None:
        """两列都 NULL 的老记忆不能被 SQL 的标量 max() 吞掉。

        SQLite 的 ``max(a, b)`` 只要有一个参数是 NULL 就返回 NULL，于是
        ``COALESCE(max(...), created_at)`` 里的 NULL 会让排序全变成 created_at。
        这里同时放"从没被碰过"和"很久以前碰过"的两条，确认顺序是对的。
        """
        fresh = self.store.add("从没被浮现过也没被用过的一条", groups=["我/其他"], grade="A")
        self.db.conn.execute(
            "UPDATE memory SET last_used_at='2021-01-01T00:00:00.000Z' WHERE id=?",
            (fresh["id"],),
        )
        fresh["id"]
        rows = self.db.conn.execute(
            "SELECT m.id FROM memory m WHERE m.status='active' ORDER BY " + surface._LAST_TOUCH + " ASC"
        ).fetchall()
        self.assertTrue(rows, "轮换查询不能返回空")
        self.assertIn(fresh["id"], [row["id"] for row in rows])
        # 2021 的那条比"完全没碰过"的 created_at(今天) 更早，所以排在前面。
        self.assertEqual(rows[0]["id"], fresh["id"])


class ReplayCase(UsageCase):
    """从日志重算「被用过」：派生量算错了就重放，不要手改。"""

    def test_replay_drops_phantom_use_counts(self) -> None:
        item = self.store.add(
            "重放测试：这条要被真正召回一次", groups=["我/其他"], grade="A"
        )
        self.app.search("重放测试", k=5)
        honest = self._row(item["id"])["use_count"]
        self.assertGreaterEqual(honest, 1, "先得真的被召回一次")

        # 模拟旧版本留下的脏数据：浮现也加了计数。
        self.db.conn.execute(
            "UPDATE memory SET use_count=50, last_used_at=? WHERE id=?",
            (now_iso(), item["id"]),
        )
        self.app.surface(session_id="s2")

        stats = usage.replay(self.db)
        self.assertFalse(stats["dry_run"])
        self.assertGreater(stats["changed"], 0)
        after = self._row(item["id"])
        self.assertEqual(after["use_count"], honest, "重放要回到日志能证明的那个数")

    def test_replay_dry_run_changes_nothing(self) -> None:
        item = self.store.add("干跑测试：不许落盘")
        self.db.conn.execute("UPDATE memory SET use_count=7 WHERE id=?", (item["id"],))
        stats = usage.replay(self.db, dry_run=True)
        self.assertTrue(stats["dry_run"])
        self.assertGreaterEqual(stats["changed"], 1)
        self.assertEqual(self._row(item["id"])["use_count"], 7)

    def test_replay_is_idempotent_and_leaves_a_receipt(self) -> None:
        item = self.store.add("重放幂等测试")
        self.db.conn.execute("UPDATE memory SET use_count=3 WHERE id=?", (item["id"],))
        usage.replay(self.db)
        second = usage.replay(self.db)
        self.assertEqual(second["changed"], 0, "重放两次不该有第二次改动")
        events = self.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE op='memory.usage_replay'"
        ).fetchone()["c"]
        self.assertEqual(events, 1, "第一次重放要留一条可追溯的事件")

    def test_replay_only_touches_usage_columns(self) -> None:
        item = self.store.add("这条的正文和状态一个都不许动", groups=["我/其他"], grade="B")
        before = self._row(item["id"])
        self.db.conn.execute("UPDATE memory SET use_count=9 WHERE id=?", (item["id"],))
        usage.replay(self.db)
        after = self._row(item["id"])
        for key in ("text_raw", "kind", "status", "importance", "confidence", "text_sha256", "day_key"):
            self.assertEqual(after[key], before[key], f"{key} 被改了")


class MigrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m98b-mig")
        self.path = Path(self._dir) / "pmem.db"

    def tearDown(self) -> None:
        drop_dir(self._dir)

    def _downgrade_to_140(self) -> None:
        """把库手工退回 1.4.0 的形状：去掉新列和它的索引。"""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP INDEX IF EXISTS idx_mem_last_surfaced")
            conn.execute("ALTER TABLE memory DROP COLUMN last_surfaced_at")
            conn.commit()
        finally:
            conn.close()

    def test_adds_column_and_backfills(self) -> None:
        app = PMem.open(self.path)
        item = app.store.add("要从老库升上来的记忆")
        app.db.conn.execute(
            "UPDATE memory SET last_used_at='2026-10-01T00:00:00.000Z' WHERE id=?",
            (item["id"],),
        )
        app.close()

        self._downgrade_to_140()
        conn = sqlite3.connect(self.path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
        conn.close()
        self.assertNotIn("last_surfaced_at", cols, "退回失败，测不了迁移")

        app = PMem.open(self.path)
        try:
            row = dict(
                app.db.conn.execute(
                    "SELECT last_surfaced_at FROM memory WHERE id=?", (item["id"],)
                ).fetchone()
            )
            self.assertEqual(
                row["last_surfaced_at"],
                "2026-10-01T00:00:00.000Z",
                "迁移要把 last_used_at 回填进新列——那是它历史上的含义",
            )
            indexes = {
                r["name"]
                for r in app.db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memory'"
                )
            }
            self.assertIn("idx_mem_last_surfaced", indexes)
            version = app.db.conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()["value"]
            self.assertEqual(version, "1.12.0")
        finally:
            app.close()

    def test_migration_is_idempotent(self) -> None:
        app = PMem.open(self.path)
        app.store.add("x")
        app.close()
        self._downgrade_to_140()
        for _ in range(3):
            app = PMem.open(self.path)
            app.close()
        conn = sqlite3.connect(self.path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memory)")]
        conn.close()
        self.assertEqual(cols.count("last_surfaced_at"), 1, "反复升级不许叠出两列")


if __name__ == "__main__":
    unittest.main()
