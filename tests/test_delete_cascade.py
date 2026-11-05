"""硬删级联（0.29.1，任务 B）：``memory_delete(hard=True)`` 后全库不能留悬空引用。

覆盖审计 B 项要求的清理面：
- ``trait/pattern/experience.evidence_json`` 里的记忆指针（摘空则删派生行）；
- ``conflict.left_id/right_id``（schema 是 NOT NULL，改为删冲突行）；
- ``feedback.target_id``（NOT NULL，删反馈行）；
- ``idem.target_id``（``journal:*`` 键保留：repair 靠它判断"写过但被删、不复活"）；
- ``memory.superseded_by``（手动置 NULL；``supersedes_id`` 由 FK SET NULL 自动清）；
- 受影响实体的 ``mention_count`` 重算 = ``COUNT(entity_mention)``。
"""

from __future__ import annotations

import json
import unittest

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.base.ids import ulid
from pmem.memory.store import MemoryStore

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})
NOW = "2026-11-04T10:00:00.000Z"


class DeleteCascadeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("delete-cascade")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")
        self.memory = self.store.add(
            "硬删级联测试记忆：trait/pattern/experience/conflict/feedback 都指向它。",
            provenance="observed",
            idem_key="contract:hard:1",
        )
        self.other = self.store.add("另一条还活着的记忆", provenance="observed")
        self.seq = int(
            self.db.conn.execute(
                "SELECT seq FROM memory WHERE id=?", (self.memory["id"],)
            ).fetchone()["seq"]
        )
        self.entity_id = self._seed_entity_mention()
        self._seed_derived_rows()
        self._seed_conflict_feedback_idem()

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    # -- 造数据 -------------------------------------------------------------
    def _seed_entity_mention(self) -> str:
        entity_id = ulid()
        self.db.conn.execute(
            "INSERT INTO entity(id, subject_id, type, name, name_norm, status, confidence, "
            "provenance, first_seen, last_seen, mention_count, extra_json) "
            "VALUES(?, 'self', 'person', '级联测试实体', '级联测试实体', 'active', 0.8, "
            "'observed', ?, ?, 1, '{}')",
            (entity_id, NOW, NOW),
        )
        self.db.conn.execute(
            "INSERT INTO entity_mention(entity_id, memory_seq, span_start, span_end, surface, "
            "confidence, extractor) VALUES(?,?,0,6,'硬删级联测试',0.8,'template')",
            (entity_id, self.seq),
        )
        return entity_id

    def _insert_trait(
        self, key: str, evidence: list[dict[str, str]], *, prev_id: str | None = None
    ) -> str:
        trait_id = ulid()
        self.db.conn.execute(
            "INSERT INTO trait(id, subject_id, scope, type, key, value_json, strength, confidence, "
            "evidence_json, evidence_days, first_seen, last_support_at, version, prev_id, "
            "derivation, created_at, updated_at) "
            "VALUES(?, 'self', 'self', 'interest', ?, '{}', 0.6, 0.6, ?, 1, ?, ?, 1, ?, 'test', ?, ?)",
            (
                trait_id,
                key,
                json.dumps(evidence, ensure_ascii=False),
                NOW,
                NOW,
                prev_id,
                NOW,
                NOW,
            ),
        )
        return trait_id

    def _seed_derived_rows(self) -> None:
        self.trait_only = self._insert_trait(
            "only-deleted", [{"memory_id": self.memory["id"]}]
        )
        self.trait_mixed = self._insert_trait(
            "mixed", [{"memory_id": self.memory["id"]}, {"memory_id": self.other["id"]}]
        )
        # 版本链子行：prev_id 指向会被删的 trait_only（自引用外键，覆盖 FK 清理）。
        self.trait_child = self._insert_trait(
            "child", [{"memory_id": self.other["id"]}], prev_id=self.trait_only
        )
        self.pattern_id = ulid()
        self.db.conn.execute(
            "INSERT INTO pattern(id, subject_id, kind, key, support, confidence, window_json, "
            "evidence_json, first_seen, last_seen, derivation, created_at) "
            "VALUES(?, 'self', 'frequency', 'p:only', 3, 0.7, '{}', ?, ?, ?, 'test', ?)",
            (
                self.pattern_id,
                json.dumps([{"memory_id": self.memory["id"]}], ensure_ascii=False),
                NOW,
                NOW,
                NOW,
            ),
        )
        self.experience_id = ulid()
        self.db.conn.execute(
            "INSERT INTO experience(id, subject_id, situation, action, outcome, evidence_json, created_at) "
            "VALUES(?, 'self', '情境', '动作', 'success', ?, ?)",
            (
                self.experience_id,
                json.dumps([{"memory_id": self.memory["id"]}], ensure_ascii=False),
                NOW,
            ),
        )

    def _seed_conflict_feedback_idem(self) -> None:
        self.conflict_id = ulid()
        self.db.conn.execute(
            "INSERT INTO conflict(id, subject_id, kind, left_tbl, left_id, right_tbl, right_id, "
            "slot, severity, detail_json, created_at) "
            "VALUES(?, 'self', 'slot', 'memory', ?, 'memory', ?, 'slot-x', 0.5, '{}', ?)",
            (self.conflict_id, self.memory["id"], self.other["id"], NOW),
        )
        self.feedback_id = ulid()
        self.db.conn.execute(
            "INSERT INTO feedback(id, ts, subject_id, target_tbl, target_id, signal, actor) "
            "VALUES(?, ?, 'self', 'memory', ?, 'useful', 'test')",
            (self.feedback_id, NOW, self.memory["id"]),
        )
        # 模拟 journal 派生的幂等键：真删后必须保留（repair 靠它判断不复活）。
        self.db.conn.execute(
            "INSERT INTO idem(key, op, target_id, payload, created_at) VALUES(?, 'memory.add', ?, '{}', ?)",
            (f"journal:{ulid()}:0", self.memory["id"], NOW),
        )
        self.db.conn.execute(
            "UPDATE memory SET superseded_by=? WHERE id=?", (self.memory["id"], self.other["id"])
        )

    # -- 测试 ---------------------------------------------------------------
    def _count(self, sql: str, params: tuple = ()) -> int:
        row = self.db.conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def test_hard_delete_clears_all_dangling_refs(self) -> None:
        memory_id = self.memory["id"]
        result = self.store.delete(memory_id, hard=True, reason="级联测试")

        self.assertEqual(result["deleted"], 1)
        self.assertTrue(result["hard"])
        # 1) 记忆行没了
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memory WHERE id=?", (memory_id,)), 0
        )
        # 2) trait/pattern/experience 的 evidence_json 不再指向它
        for table in ("trait", "pattern", "experience"):
            self.assertEqual(
                self._count(f"SELECT COUNT(*) FROM {table} WHERE evidence_json LIKE ?", (f"%{memory_id}%",)),
                0,
                f"{table} 还留着悬空证据指针",
            )
        # 只指向它的派生行被删；混合证据的那条保留、只摘掉它
        self.assertEqual(self._count("SELECT COUNT(*) FROM trait WHERE id=?", (self.trait_only,)), 0)
        self.assertEqual(self._count("SELECT COUNT(*) FROM trait WHERE id=?", (self.trait_mixed,)), 1)
        # 版本链子行的 prev_id 必须断开（自引用外键，否则硬删会被 FK 卡住）
        child = self.db.conn.execute(
            "SELECT prev_id FROM trait WHERE id=?", (self.trait_child,)
        ).fetchone()
        self.assertIsNotNone(child)
        self.assertIsNone(child["prev_id"])
        mixed = self.db.conn.execute(
            "SELECT evidence_json FROM trait WHERE id=?", (self.trait_mixed,)
        ).fetchone()
        self.assertEqual(
            [item["memory_id"] for item in json.loads(mixed["evidence_json"])], [self.other["id"]]
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM pattern WHERE id=?", (self.pattern_id,)), 0)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM experience WHERE id=?", (self.experience_id,)), 0
        )
        # 3) conflict / feedback 不再指向它
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM conflict WHERE "
                "(left_tbl='memory' AND left_id=?) OR (right_tbl='memory' AND right_id=?)",
                (memory_id, memory_id),
            ),
            0,
        )
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM feedback WHERE target_tbl='memory' AND target_id=?",
                (memory_id,),
            ),
            0,
        )
        # 4) idem：普通键清掉，journal:* 键保留（repair 的"不复活"依据）
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM idem WHERE target_id=? AND key NOT LIKE 'journal:%'",
                (memory_id,),
            ),
            0,
        )
        self.assertEqual(
            self._count(
                "SELECT COUNT(*) FROM idem WHERE target_id=? AND key LIKE 'journal:%'",
                (memory_id,),
            ),
            1,
        )
        # 5) superseded_by 清空
        row = self.db.conn.execute(
            "SELECT superseded_by FROM memory WHERE id=?", (self.other["id"],)
        ).fetchone()
        self.assertIsNone(row["superseded_by"])
        # 6) 实体热度 = 现存 mention 数
        self.assertEqual(
            self._count("SELECT mention_count FROM entity WHERE id=?", (self.entity_id,)), 0
        )
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM entity_mention WHERE memory_seq=?", (self.seq,)), 0
        )
        # 7) 只记一条 memory.hard_delete，payload 带各项清理计数
        rows = self.db.conn.execute(
            "SELECT payload_json FROM event_log WHERE op='memory.hard_delete' AND target_id=?",
            (memory_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertTrue(payload["tombstone"])
        cascaded = payload["cascaded"]
        for key in (
            "entity_mention",
            "trait_refs",
            "trait_deleted",
            "pattern_refs",
            "pattern_deleted",
            "experience_refs",
            "experience_deleted",
            "conflicts",
            "feedback",
            "idem",
            "journal_idem_kept",
            "superseded_cleared",
            "entities_recounted",
        ):
            self.assertIn(key, cascaded)
        self.assertEqual(cascaded["trait_refs"], 2)
        self.assertEqual(cascaded["trait_deleted"], 1)
        self.assertEqual(cascaded["pattern_deleted"], 1)
        self.assertEqual(cascaded["experience_deleted"], 1)
        self.assertEqual(cascaded["conflicts"], 1)
        self.assertEqual(cascaded["feedback"], 1)
        self.assertEqual(cascaded["idem"], 1)
        self.assertEqual(cascaded["journal_idem_kept"], 1)
        self.assertEqual(cascaded["superseded_cleared"], 1)
        self.assertEqual(cascaded["entities_recounted"], 1)
        # 8) 事件链仍然连续
        from pmem.base import events

        ok, detail = events.verify_chain(self.db.conn)
        self.assertTrue(ok, detail)

    def test_hard_delete_does_not_touch_other_memories(self) -> None:
        self.store.delete(self.memory["id"], hard=True)
        self.assertEqual(
            self._count("SELECT COUNT(*) FROM memory WHERE id=?", (self.other["id"],)), 1
        )
        self.assertEqual(self._count("SELECT COUNT(*) FROM memory"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
