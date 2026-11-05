"""repair 过滤（0.29.1，任务 C）+ 日记索引可观测（任务 D）。

C：repair 重放前要跳过两类原料——``_strip_runtime`` 判为噪声的行、
``journal_retraction`` 已撤回的行。它们不写成事实，只写一条
``journal:{episode_id}:{index}:skipped`` 幂等键 + ``journal.fact_skipped`` 事件，
让 I20（W2 那边）能把这些"声称过但明确跳过"的条数算作已交代。

D：``journal.stats()`` 要能看到 ``unindexed`` / ``fts_rows``；
``append`` 的索引失败不能丢原文，也不能静默。
"""

from __future__ import annotations

import json
import unittest

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.base.ids import ulid
from pmem.memory import journal
from pmem.memory.store import MemoryStore

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})
NOW = "2026-11-04T10:00:00.000Z"
DAY = "2026-11-04"

# 两个运行时标记 → ``_strip_runtime`` 判为噪声（返回空串）。
NOISE_TEXT = "【当前时间】2026-11-04 10:00\n【当前角色】我是小鲸鲸\n今天吃了面"


class JournalRepairFilterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("journal-repair-filter")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    # -- 造数据 -------------------------------------------------------------
    def _insert_line(self, text: str, *, role: str = "user") -> str:
        line_id = ulid()
        self.db.conn.execute(
            "INSERT INTO journal_line(id, subject_id, session_id, day_key, ts, role, text, created_at) "
            "VALUES(?, 'self', 's1', ?, ?, ?, ?, ?)",
            (line_id, DAY, NOW, role, text, NOW),
        )
        return line_id

    def _insert_episode(
        self,
        line_ids: list[str],
        *,
        claimed: int,
        title: str = "测试日记",
        summary: str = "今天的经过",
    ) -> str:
        episode_id = ulid()
        derivation = json.dumps({"lines": line_ids, "line_count": len(line_ids), "mode": "template"})
        self.db.conn.execute(
            "INSERT INTO episode(id, subject_id, channel, started_at, ended_at, day_key, title, "
            "summary, memory_count, derivation, extra_json) "
            "VALUES(?, 'self', 'journal', ?, ?, ?, ?, ?, ?, ?, '{}')",
            (episode_id, NOW, NOW, DAY, title, summary, claimed, derivation),
        )
        return episode_id

    def _count(self, sql: str, params: tuple = ()) -> int:
        row = self.db.conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    # -- C1：噪声行 → 跳过，不写事实 ---------------------------------------
    def test_noise_line_is_skipped_with_exact_idem_key(self) -> None:
        line_id = self._insert_line(NOISE_TEXT)
        episode_id = self._insert_episode([line_id], claimed=1)

        result = journal.repair(self.db, store=self.store)
        self.assertEqual(result["skipped"], 1, result)
        self.assertEqual(result["added"], 0, result)
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory"), 0, "噪声行不该变成事实")

        key = f"journal:{episode_id}:0:skipped"
        row = self.db.conn.execute(
            "SELECT op, target_id FROM idem WHERE key=?", (key,)
        ).fetchone()
        self.assertIsNotNone(row, f"缺少跳过的幂等键 {key}")
        self.assertEqual(row["op"], "journal.fact_skipped")
        self.assertEqual(row["target_id"], episode_id)

        events = self.db.conn.execute(
            "SELECT payload_json FROM event_log WHERE op='journal.fact_skipped'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["episode_id"], episode_id)
        self.assertEqual(payload["index"], 0)
        self.assertEqual(payload["reason"], "noise")

        # 幂等：再跑一次不进 gap（跳过键已算"交代过"），事件不重复。
        again = journal.repair(self.db, store=self.store)
        self.assertEqual(again["gaps"], 0, again)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM event_log WHERE op='journal.fact_skipped'"), 1
        )

    # -- C2：已撤回的行 → 跳过 ---------------------------------------------
    def test_retracted_line_is_skipped(self) -> None:
        appended = journal.append(self.db, "这句话后来被撤回了，不该再变成事实。", session_id="s1")
        line_id = appended["id"]
        journal.retract(self.db, line_id, subject="self", reason="用户说这句别记")
        episode_id = self._insert_episode([line_id], claimed=1)

        result = journal.repair(self.db, store=self.store)
        self.assertEqual(result["skipped"], 1, result)
        self.assertEqual(result["added"], 0, result)
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory"), 0)

        key = f"journal:{episode_id}:0:skipped"
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM idem WHERE key=?", (key,)), 1
        )
        payload = json.loads(
            self.db.conn.execute(
                "SELECT payload_json FROM event_log WHERE op='journal.fact_skipped'"
            ).fetchone()["payload_json"]
        )
        self.assertEqual(payload["reason"], "retracted")

    # -- 回归：正常原料仍然要补出来 ----------------------------------------
    def test_normal_line_is_still_repaired(self) -> None:
        line_id = self._insert_line("今天去看了海，风很大。")
        episode_id = self._insert_episode([line_id], claimed=1)

        result = journal.repair(self.db, store=self.store)
        self.assertEqual(result["added"], 1, result)
        self.assertEqual(result["skipped"], 0, result)
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory"), 1)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM idem WHERE key=?", (f"journal:{episode_id}:0",)), 1
        )
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM idem WHERE key=?", (f"journal:{episode_id}:0:skipped",)
            ),
            0,
        )

    # -- D：stats 可观测 unindexed / fts_rows ------------------------------
    def test_stats_expose_unindexed_and_fts_rows(self) -> None:
        journal.append(self.db, "第一句原话", session_id="s1")
        journal.append(self.db, "第二句原话", session_id="s1")
        stats = journal.stats(self.db)
        self.assertEqual(stats["lines"], 2)
        self.assertEqual(stats["fts_rows"], 2)
        self.assertEqual(stats["unindexed"], 0)

        # 模拟一条索引丢失（老库/写失败）：原文还在，索引行没了 → 必须看得见。
        self.db.conn.execute("DELETE FROM journal_fts WHERE rowid=(SELECT MIN(rowid) FROM journal_fts)")
        stats = journal.stats(self.db)
        self.assertEqual(stats["lines"], 2)
        self.assertEqual(stats["fts_rows"], 1)
        self.assertEqual(stats["unindexed"], 1)

    # -- D2：episode_fts 官方修法（delete + 全量重灌，rowid 映射）-----------
    def test_rebuild_episode_index_keeps_episode_rowid_mapping(self) -> None:
        episode_ids = []
        for index in range(3):
            line_id = self._insert_line(f"第 {index} 天的一句原话，用来造日记索引。")
            episode_ids.append(
                self._insert_episode(
                    [line_id],
                    claimed=1,
                    title=f"第{index}天",
                    summary=f"第 {index} 天的经过",
                )
            )
        self.assertEqual(journal.ensure_episode_index(self.db), 3)

        # 弄坏索引：缺一行 + 多一行孤儿 rowid
        self.db.conn.execute(
            "DELETE FROM episode_fts WHERE rowid=(SELECT MIN(rowid) FROM episode_fts)"
        )
        self.db.conn.execute("INSERT INTO episode_fts(rowid, text_seg) VALUES(999999, 'orphan')")

        dry = journal.rebuild_episode_index(self.db, dry_run=True)
        self.assertEqual(dry, {"episodes": 3, "indexed": 0, "dry_run": True})
        # dry_run 不写库
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM episode_fts WHERE rowid=999999"), 1
        )

        stats = journal.rebuild_episode_index(self.db)
        self.assertEqual(stats, {"episodes": 3, "indexed": 3, "dry_run": False})
        fts_rowids = [
            int(row["rowid"])
            for row in self.db.conn.execute("SELECT rowid FROM episode_fts ORDER BY rowid")
        ]
        episode_rowids = [
            int(row["rowid"])
            for row in self.db.conn.execute("SELECT rowid FROM episode ORDER BY rowid")
        ]
        self.assertEqual(fts_rowids, episode_rowids, "rowid 映射必须与 episode.rowid 一致")
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM event_log WHERE op='journal.episode_reindex'"), 1
        )

        # 幂等：再重建一次结果不变，不多事件
        again = journal.rebuild_episode_index(self.db)
        self.assertEqual(again, {"episodes": 3, "indexed": 3, "dry_run": False})
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM episode_fts"), 3
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
