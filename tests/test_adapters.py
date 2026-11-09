"""M1.5 适配层：会话解压/抽取、作用域键、回填、影子模式。"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

from pmem.adapters import dsh, shadow
from pmem.adapters.legacy import LegacyReader
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

DEMO_SCOPE = "project:cc649eec1dea2a10318e308f4dc662082b76db1691df26d7447d91063728cd6f"

SESSION_LINES = [
    {"type": "session", "id": "sess-1", "cwd": "D:\\work\\demo", "createdAt": 1793627000000, "origin": "root"},
    {"type": "system/message", "seq": 1, "time": 1793627001000, "data": {"content": [{"type": "text", "text": "系统注入"}]}},
    {"type": "user/message", "seq": 8, "time": 1793627320115,
     "data": {"content": [{"type": "text", "text": "我住在杭州，常去西湖"}], "source": {"kind": "user"}}},
    {"type": "user/message", "seq": 9, "time": 1793627321115,
     "data": {"content": [{"type": "text", "text": "/help"}], "source": {"kind": "user"}}},
    {"type": "user/message", "seq": 10, "time": 1793627322115,
     "data": {"content": [{"type": "text", "text": "短"}], "source": {"kind": "user"}}},
    {"type": "assistant/message", "seq": 11, "time": 1793627323115,
     "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "好的，我记下了这句话。"}]}}},
]

SUBAGENT_LINES = [
    {"type": "session", "id": "sess-sub", "cwd": "D:\\work\\demo", "createdAt": 1793627000000, "origin": "subagent"},
    {"type": "user/message", "seq": 3, "time": 1793627320115,
     "data": {"content": [{"type": "text", "text": "子代理的提问不该被回填"}], "source": {"kind": "user"}}},
]


def write_session(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return path


def make_legacy_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE span(id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, session TEXT NOT NULL,
                          seq INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL,
                          text TEXT NOT NULL, bytes INTEGER NOT NULL);
        CREATE VIRTUAL TABLE span_fts USING fts5(text, scope UNINDEXED, session UNINDEXED, seq UNINDEXED);
        """
    )
    con.execute(
        "INSERT INTO span(scope, session, seq, hash, text, bytes) VALUES(?,?,?,?,?,?)",
        (DEMO_SCOPE, "sess-1", 8, "h1", "我住在杭州，常去西湖", 27),
    )
    con.execute(
        "INSERT INTO span_fts(rowid, text, scope, session, seq) VALUES(1,?,?,?,?)",
        ("我住在杭州，常去西湖", DEMO_SCOPE, "sess-1", 8),
    )
    con.commit()
    con.close()
    return path


class TestScope(unittest.TestCase):
    def test_matches_project_scope(self) -> None:
        self.assertEqual(dsh.project_scope("D:\\work\\demo"), DEMO_SCOPE)

    def test_case_and_slash_insensitive(self) -> None:
        self.assertEqual(dsh.project_scope("D:/work/demo/"), DEMO_SCOPE)
        self.assertEqual(dsh.project_scope("d:\\WORK\\demo"), DEMO_SCOPE)


class TestSessionParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("dsh")

    def tearDown(self) -> None:
        drop_dir(self.dir)

    def test_load_jsonl(self) -> None:
        path = write_session(self.dir / "s.jsonl", SESSION_LINES)
        events = dsh.load_events(path)
        self.assertEqual(len(events), len(SESSION_LINES))
        self.assertEqual(dsh.session_meta(events)["id"], "sess-1")

    def test_user_messages_filter(self) -> None:
        events = dsh.load_events(write_session(self.dir / "s.jsonl", SESSION_LINES))
        messages = dsh.user_messages(events)
        self.assertEqual([m["seq"] for m in messages], [8])  # 斜杠命令、过短、系统注入都被过滤
        self.assertIn("杭州", messages[0]["text"])

    def test_assistant_texts(self) -> None:
        events = dsh.load_events(write_session(self.dir / "s.jsonl", SESSION_LINES))
        texts = dsh.assistant_texts(events, min_chars=5)
        self.assertEqual(len(texts), 1)
        self.assertIn("记下了", texts[0]["text"])

    def test_ms_to_iso(self) -> None:
        iso = shadow._ms_to_iso(1793627320115)
        self.assertIsNotNone(iso)
        self.assertTrue(iso.endswith("Z"))
        self.assertIsNone(shadow._ms_to_iso(None))


class TestBackfill(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("bf")
        self.app = PMem.open(self.dir / "pmem.db")
        self.session = write_session(self.dir / "s.jsonl", SESSION_LINES)

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self.dir)

    def test_backfill_adds_user_messages(self) -> None:
        stats = shadow.backfill(self.app, [self.session], scope_key=DEMO_SCOPE)
        self.assertEqual(stats["messages"], 1)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(self.app.store.stats()["memory_total"], 1)

    def test_backfill_is_idempotent(self) -> None:
        shadow.backfill(self.app, [self.session], scope_key=DEMO_SCOPE)
        second = shadow.backfill(self.app, [self.session], scope_key=DEMO_SCOPE)
        self.assertEqual(second["replay"], 1)
        self.assertEqual(self.app.store.stats()["memory_total"], 1)

    def test_backfill_keeps_original_day(self) -> None:
        shadow.backfill(self.app, [self.session], scope_key=DEMO_SCOPE)
        row = self.app.db.conn.execute("SELECT day_key, scope_key, source_ref FROM memory").fetchone()
        self.assertEqual(row["scope_key"], DEMO_SCOPE)
        self.assertRegex(row["day_key"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(row["source_ref"].startswith("dsh:sess-1:"))

    def test_backfill_skips_subagent(self) -> None:
        sub = write_session(self.dir / "sub.jsonl", SUBAGENT_LINES)
        stats = shadow.backfill(self.app, [sub], scope_key=DEMO_SCOPE)
        self.assertEqual(stats["skipped_sessions"], 1)
        self.assertEqual(stats["added"], 0)

    def test_backfill_dry_run(self) -> None:
        stats = shadow.backfill(self.app, [self.session], scope_key=DEMO_SCOPE, dry_run=True)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(self.app.store.stats()["memory_total"], 0)


class TestLegacyReader(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("soul")
        self.db = make_legacy_db(self.dir / "evolve.sqlite")

    def tearDown(self) -> None:
        drop_dir(self.dir)

    def test_missing_file_is_unavailable(self) -> None:
        reader = LegacyReader(self.dir / "nope.sqlite")
        self.assertFalse(reader.available)
        self.assertEqual(reader.search("杭州", DEMO_SCOPE), [])
        reader.close()

    def test_available_and_count(self) -> None:
        with LegacyReader(self.db) as reader:
            self.assertTrue(reader.available)
            self.assertEqual(reader.span_count(DEMO_SCOPE), 1)

    def test_fts_cannot_match_chinese(self) -> None:
        """这是实测结论，不是 bug：unicode61 把整段中文当一个 token。"""
        with LegacyReader(self.db) as reader:
            self.assertEqual(reader.search("杭州", DEMO_SCOPE, mode="fts"), [])

    def test_like_matches_chinese(self) -> None:
        with LegacyReader(self.db) as reader:
            hits = reader.search("杭州", DEMO_SCOPE, mode="like")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["seq"], 8)


class TestOverlapAndSummary(unittest.TestCase):
    def test_overlap_containment(self) -> None:
        items = [{"text": "我住在杭州"}]
        soul = [{"text": "我住在杭州，常去西湖"}]
        self.assertEqual(shadow._overlap(items, soul), 1.0)

    def test_overlap_rejects_unrelated(self) -> None:
        items = [{"text": "我住在杭州"}]
        soul = [{"text": "明天要交报告"}]
        self.assertEqual(shadow._overlap(items, soul), 0.0)

    def test_overlap_empty_sides(self) -> None:
        self.assertEqual(shadow._overlap([], [{"text": "x"}]), 0.0)
        self.assertEqual(shadow._overlap([{"text": "x"}], []), 0.0)

    def test_summarize(self) -> None:
        records = [
            {"query": "q1", "awb": {"n": 2, "latency_ms": 10, "tokens": 100}, "legacy_fts": {"n": 0},
             "legacy_like": {"n": 1}, "overlap_fts": 0.0, "overlap_like": 0.5},
            {"query": "q2", "awb": {"n": 0, "latency_ms": 20, "tokens": 0}, "legacy_fts": {"n": 0},
             "legacy_like": {"n": 0}, "overlap_fts": 0.0, "overlap_like": 0.0},
        ]
        summary = shadow.summarize(records)
        self.assertEqual(summary["queries"], 2)
        self.assertEqual(summary["awb_hit_rate"], 0.5)
        self.assertEqual(summary["tokens_mean"], 50.0)
        self.assertEqual(summary["awb_empty_queries"], 1)


class TestShadowRun(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("shadow")
        self.app = PMem.open(self.dir / "pmem.db")
        self.session = write_session(self.dir / "s.jsonl", SESSION_LINES)
        self.legacy_db = make_legacy_db(self.dir / "evolve.sqlite")
        self.out = self.dir / "shadow.jsonl"

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self.dir)

    def test_run_records_and_reports(self) -> None:
        shadow.backfill(self.app, [self.session], scope_key=DEMO_SCOPE)
        with LegacyReader(self.legacy_db) as legacy:
            summary = shadow.run_shadow(
                self.app, [self.session], scope=DEMO_SCOPE, out_path=self.out, legacy=legacy
            )
        self.assertEqual(summary["queries"], 1)
        self.assertEqual(summary["awb_hit_rate"], 1.0)
        self.assertEqual(summary["legacy_fts_hit_rate"], 0.0)  # 中文 FTS 不通
        self.assertEqual(summary["legacy_like_hit_rate"], 1.0)

        records = shadow.load_records(self.out)
        self.assertEqual(len(records), 1)
        self.assertIn("杭州", records[0]["query"])
        self.assertGreater(records[0]["overlap_like"], 0)

        report = self.dir / "report.md"
        shadow.write_report(summary, records, report)
        text = report.read_text(encoding="utf-8")
        self.assertIn("影子模式报告", text)
        self.assertIn("重合率", text)

    def test_run_skips_other_scopes(self) -> None:
        other = write_session(
            self.dir / "other.jsonl",
            [{**SESSION_LINES[0], "id": "sess-x", "cwd": "D:\\Other"}, SESSION_LINES[2]],
        )
        with LegacyReader(self.legacy_db) as legacy:
            summary = shadow.run_shadow(
                self.app, [other], scope=DEMO_SCOPE, out_path=self.out, legacy=legacy
            )
        self.assertEqual(summary["queries"], 0)


class TestNodeDiscovery(unittest.TestCase):
    def test_find_node_or_skip(self) -> None:
        node = dsh.find_node()
        if node is None:
            self.skipTest("本机没有 node，跳过（回填需要 node 解压 .zstd）")
        self.assertTrue(Path(node).exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
