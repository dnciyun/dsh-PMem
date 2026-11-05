"""不变量扩展：I4 扩表 / I21 FTS 行数漂移 / I22 事件链锚点 / I20 认 skip key。

这四条的共同点：**都不许把 doctor 打崩，也都不许静默**。
- I4：坏 JSON 报成 detail，不抛异常；
- I21：只查行数漂移（校验和级损坏归 PRAGMA integrity_check）；
- I22：锚点缺失时 ok + 明说「尚无锚点」，不假装检测过；
- I20：跳过（skipped）与写成（fact）都算交代，真的没交代才红。
"""

from __future__ import annotations

import json
import unittest

from pmem.base import events
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.memory import journal
from pmem.memory.store import MemoryStore
from pmem.ops import invariants

TS = "2026-01-01T00:00:00.000Z"


def checks(db: Database) -> dict:
    return {item["id"]: item for item in invariants.check_all(db)}


def add_memory(db: Database, text: str = "一条测试记忆", **kwargs: object) -> str:
    store = MemoryStore(db, config=Config(), subject="self")
    return store.add(text, provenance="user_explicit", **kwargs)["id"]


def insert_episode(conn, episode_id: str, *, memory_count: int = 0, summary: str = "今天聊了运维"):
    conn.execute(
        "INSERT INTO episode(id, subject_id, channel, started_at, ended_at, day_key, title, "
        "summary, memory_count, derivation, extra_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, "self", "journal", TS, TS, "2026-01-01", "标题", summary, memory_count, "{}", "{}"),
    )


def insert_pattern(conn, ident: str, evidence_json: str):
    conn.execute(
        "INSERT INTO pattern(id, subject_id, kind, key, support, confidence, window_json, "
        "evidence_json, status, first_seen, last_seen, derivation, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident, "self", "frequency", f"k-{ident}", 2, 0.6, "{}", evidence_json, "candidate", TS, TS, "probe", TS),
    )


def insert_experience(conn, ident: str, evidence_json: str):
    conn.execute(
        "INSERT INTO experience(id, subject_id, situation, action, outcome, context_json, "
        "evidence_json, reuse_count, confidence, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ident, "self", "s", "a", "success", "{}", evidence_json, 0, 0.6, "active", TS),
    )


def insert_trait(conn, ident: str, evidence_json: str):
    conn.execute(
        "INSERT INTO trait(id, subject_id, scope, type, key, value_json, strength, confidence, "
        "status, evidence_json, evidence_days, first_seen, last_support_at, version, derivation, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident, "self", "topic", "cognition", f"trait-{ident}", "{}", 0.6, 0.6, "hypothesis",
         evidence_json, 1, TS, TS, 1, "probe", TS, TS),
    )


def insert_conflict(conn, ident: str, left_tbl: str, left_id: str, right_tbl: str, right_id: str):
    conn.execute(
        "INSERT INTO conflict(id, subject_id, kind, left_tbl, left_id, right_tbl, right_id, "
        "detail_json, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ident, "self", "evidence", left_tbl, left_id, right_tbl, right_id, "{}", "open", TS),
    )


def insert_feedback(conn, ident: str, target_tbl: str, target_id: str):
    conn.execute(
        "INSERT INTO feedback(id, ts, subject_id, target_tbl, target_id, signal, actor) "
        "VALUES(?,?,?,?,?,?,?)",
        (ident, TS, "self", target_tbl, target_id, "useful", "ai"),
    )


def insert_idem(conn, key: str, target_id: str = "01TARGET000000000000000000"):
    conn.execute(
        "INSERT INTO idem(key, op, target_id, payload, created_at) VALUES(?,?,?,?,?)",
        (key, "memory.add", target_id, "{}", TS),
    )


class I4ExpandedTest(unittest.TestCase):
    """I4：证据指针不能指向已不存在（真删）的记忆 —— 覆盖全部引用落点。"""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init()
        self.conn = self.db.conn
        self.memory_id = add_memory(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def _evidence(self, *memory_ids: str) -> str:
        return json.dumps([{"memory_id": memory_id} for memory_id in memory_ids], ensure_ascii=False)

    def test_valid_references_all_green(self) -> None:
        good = self._evidence(self.memory_id)
        insert_pattern(self.conn, "p1", good)
        insert_experience(self.conn, "e1", good)
        insert_trait(self.conn, "t1", good)
        insert_conflict(self.conn, "c1", "memory", self.memory_id, "memory", self.memory_id)
        insert_feedback(self.conn, "f1", "memory", self.memory_id)
        self.assertTrue(checks(self.db)["I4"]["ok"])

    def test_dangling_in_pattern_and_experience(self) -> None:
        missing = "01MISSING00000000000000000"
        insert_pattern(self.conn, "p1", self._evidence(missing))
        insert_experience(self.conn, "e1", self._evidence(missing))
        check = checks(self.db)["I4"]
        self.assertFalse(check["ok"])
        self.assertIn("pattern 1", check["detail"])
        self.assertIn("experience 1", check["detail"])

    def test_dangling_in_conflict_and_feedback(self) -> None:
        missing = "01MISSING00000000000000001"
        insert_conflict(self.conn, "c1", "memory", missing, "memory", self.memory_id)
        insert_feedback(self.conn, "f1", "memory", missing)
        check = checks(self.db)["I4"]
        self.assertFalse(check["ok"])
        self.assertIn("conflict 1", check["detail"])
        self.assertIn("feedback 1", check["detail"])

    def test_bad_json_reported_not_crash(self) -> None:
        insert_pattern(self.conn, "p1", "{这不是 JSON")
        insert_experience(self.conn, "e1", "[[[")
        check = checks(self.db)["I4"]  # 不抛异常
        self.assertFalse(check["ok"])
        self.assertIn("JSON 解析失败", check["detail"])
        self.assertIn("2 条", check["detail"])

    def test_non_memory_pointers_ignored(self) -> None:
        insert_conflict(self.conn, "c1", "entity", "missing-entity", "memory", self.memory_id)
        insert_feedback(self.conn, "f1", "entity", "missing-entity")
        self.assertTrue(checks(self.db)["I4"]["ok"])

    def test_dict_evidence_form_supported(self) -> None:
        missing = "01MISSING00000000000000002"
        insert_pattern(self.conn, "p1", json.dumps({"evidence": [{"memory_id": missing}]}))
        self.assertFalse(checks(self.db)["I4"]["ok"])


class I21FtsDriftTest(unittest.TestCase):
    """I21：基表行数 vs FTS 文档数（只查漂移，不查校验和）。"""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init()
        self.conn = self.db.conn
        add_memory(self.db, "第一条记忆，用来建索引")
        add_memory(self.db, "第二条记忆，也建索引")
        journal.append(self.db, "一句普通原话", role="user", session_id="s1")
        self.episode_id = "01EP000000000000000000001"
        insert_episode(self.conn, self.episode_id, summary="今天聊了运维")
        journal.index_episode(self.db, self.episode_id)

    def tearDown(self) -> None:
        self.db.close()

    def test_clean_indexes_green(self) -> None:
        check = checks(self.db)["I21"]
        self.assertTrue(check["ok"], check["detail"])
        self.assertIn("索引同步", check["detail"])

    def test_soft_deleted_memory_keeps_index_green(self) -> None:
        # 软删只改 status，不触发 FTS 更新触发器；索引按设计保留（恢复不用重建）。
        store = MemoryStore(self.db, config=Config(), subject="self")
        memory_id = store.add("软删也要留着索引", provenance="user_explicit")["id"]
        store.delete(memory_id)  # 软删
        check = checks(self.db)["I21"]
        self.assertTrue(check["ok"], check["detail"])

    def test_journal_drift_red(self) -> None:
        self.conn.execute("DELETE FROM journal_fts")
        check = checks(self.db)["I21"]
        self.assertFalse(check["ok"])
        self.assertIn("journal_line 1", check["detail"])
        self.assertIn("journal reindex", check["detail"])

    def test_memory_fts_tok_drift_red(self) -> None:
        row = self.conn.execute("SELECT seq, text_seg FROM memory ORDER BY seq LIMIT 1").fetchone()
        self.conn.execute(
            "INSERT INTO memory_fts_tok(memory_fts_tok, rowid, text_seg) VALUES('delete', ?, ?)",
            (row[0], row[1]),
        )
        check = checks(self.db)["I21"]
        self.assertFalse(check["ok"])
        self.assertIn("memory_fts_tok", check["detail"])

    def test_memory_fts_tri_drift_red(self) -> None:
        row = self.conn.execute("SELECT seq, text_norm FROM memory ORDER BY seq LIMIT 1").fetchone()
        self.conn.execute(
            "INSERT INTO memory_fts_tri(memory_fts_tri, rowid, text_norm) VALUES('delete', ?, ?)",
            (row[0], row[1]),
        )
        check = checks(self.db)["I21"]
        self.assertFalse(check["ok"])
        self.assertIn("memory_fts_tri", check["detail"])

    def test_episode_drift_red(self) -> None:
        self.conn.execute("DELETE FROM episode_fts")
        check = checks(self.db)["I21"]
        self.assertFalse(check["ok"])
        self.assertIn("episode 1", check["detail"])


class I22AnchorTest(unittest.TestCase):
    """I22：链锚点 —— 只允许追加；砍尾巴/改尾巴（即使 hash 重算得自洽）也要红。"""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init()
        self.conn = self.db.conn

    def tearDown(self) -> None:
        self.db.close()

    def _append_events(self, count: int) -> None:
        with self.db.tx() as conn:
            for index in range(count):
                events.append(conn, actor="test", op=f"op.{index}", payload={"i": index})

    def test_no_anchor_is_ok_but_says_so(self) -> None:
        ok, detail = events.check_anchor(self.conn)
        self.assertTrue(ok)
        self.assertIn("尚无", detail)
        check = checks(self.db)["I22"]
        self.assertTrue(check["ok"])
        self.assertIn("尚无", check["detail"])

    def test_anchor_then_append_still_green(self) -> None:
        self._append_events(3)
        events.write_anchor(self.conn)
        self._append_events(2)
        ok, detail = events.check_anchor(self.conn)
        self.assertTrue(ok, detail)
        self.assertIn("只追加", detail)
        self.assertTrue(checks(self.db)["I22"]["ok"])

    def test_tail_truncation_red(self) -> None:
        self._append_events(3)
        events.write_anchor(self.conn)
        self.conn.execute("DELETE FROM event_log WHERE seq=(SELECT MAX(seq) FROM event_log)")
        ok, detail = events.check_anchor(self.conn)
        self.assertFalse(ok)
        self.assertIn("少于锚点", detail)
        self.assertFalse(checks(self.db)["I22"]["ok"])

    def test_tail_tamper_with_recomputed_hash_red(self) -> None:
        # 只改尾事件的 payload 并把 hash 一起重算：verify_chain 会认为链仍然自洽，
        # 锚点必须能发现「锚点那条事件的 hash 变了」。
        self._append_events(3)
        events.write_anchor(self.conn)
        row = self.conn.execute(
            "SELECT seq, prev_hash, ts, actor, op, target_tbl, target_id FROM event_log "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        payload = '{"tampered":1}'
        target = f"{row['target_tbl'] or ''}:{row['target_id'] or ''}"
        new_hash = events._digest(row["prev_hash"], row["ts"], row["actor"], row["op"], target, payload)
        self.conn.execute(
            "UPDATE event_log SET payload_json=?, hash=? WHERE seq=?", (payload, new_hash, row["seq"])
        )
        chain_ok, _ = events.verify_chain(self.conn)
        self.assertTrue(chain_ok)  # 哈希链本身看不出来 —— 正是锚点要补的洞
        ok, detail = events.check_anchor(self.conn)
        self.assertFalse(ok)
        self.assertIn("hash 变了", detail)
        self.assertFalse(checks(self.db)["I22"]["ok"])

    def test_middle_delete_red(self) -> None:
        self._append_events(4)
        events.write_anchor(self.conn)
        mid = self.conn.execute("SELECT MIN(seq) FROM event_log").fetchone()[0]
        self.conn.execute("DELETE FROM event_log WHERE seq=?", (mid,))
        ok, detail = events.check_anchor(self.conn)
        self.assertFalse(ok)
        self.assertIn("少于锚点", detail)

    def test_write_anchor_idempotent(self) -> None:
        self._append_events(2)
        first = events.write_anchor(self.conn)
        self.assertEqual(first["count"], 2)
        second = events.write_anchor(self.conn)
        self.assertEqual(second["count"], 2)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM schema_meta WHERE key IN (?,?,?)", events.ANCHOR_KEYS
        ).fetchone()[0]
        self.assertEqual(rows, 3)  # 覆盖写，不产生重复键

    def test_empty_chain_anchor_then_append_green(self) -> None:
        anchor = events.write_anchor(self.conn)
        self.assertEqual(anchor["count"], 0)
        self.assertIsNone(anchor["last_seq"])
        self._append_events(1)
        ok, detail = events.check_anchor(self.conn)
        self.assertTrue(ok, detail)


class I20SkipKeyTest(unittest.TestCase):
    """I20：``journal:{ep}:{i}:skipped`` 与写成的 ``journal:{ep}:{i}`` 都算交代过。"""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.db.init()
        self.conn = self.db.conn
        self.episode_id = "01EP0000000000000000000A1"

    def tearDown(self) -> None:
        self.db.close()

    def _check(self) -> dict:
        return checks(self.db)["I20"]

    def test_skip_key_counts_as_accounted(self) -> None:
        insert_episode(self.conn, self.episode_id, memory_count=1)
        insert_idem(self.conn, f"journal:{self.episode_id}:0:skipped", target_id=None)
        check = self._check()
        self.assertTrue(check["ok"], check["detail"])

    def test_fact_key_counts_as_accounted(self) -> None:
        insert_episode(self.conn, self.episode_id, memory_count=1)
        insert_idem(self.conn, f"journal:{self.episode_id}:0")
        self.assertTrue(self._check()["ok"])

    def test_real_derived_row_counts_as_accounted(self) -> None:
        insert_episode(self.conn, self.episode_id, memory_count=1)
        add_memory(self.db, "日记派生出来的一条事实", episode_id=self.episode_id)
        self.assertTrue(self._check()["ok"])

    def test_nothing_accounted_is_red(self) -> None:
        insert_episode(self.conn, self.episode_id, memory_count=1)
        check = self._check()
        self.assertFalse(check["ok"])
        self.assertIn("缺 1 条", check["detail"])

    def test_malformed_journal_keys_do_not_launder_the_gap(self) -> None:
        insert_episode(self.conn, self.episode_id, memory_count=1)
        insert_idem(self.conn, f"journal:{self.episode_id}:oops")  # index 不是整数
        insert_idem(self.conn, f"journal:{self.episode_id}:0:other")  # 后缀不是 skipped
        insert_idem(self.conn, f"journal:{self.episode_id}")  # 少了 index
        self.assertFalse(self._check()["ok"])


if __name__ == "__main__":
    unittest.main()
