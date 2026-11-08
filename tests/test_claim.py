"""claim 内核：表/迁移/合并/版本链/软删/人物卡（全部显式 API，默认关不起作用）。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.errors import ValidationError
from pmem.gate.sdk import PMem
from pmem.memory import claim

from .support import drop_dir, temp_dir


class ClaimCase(unittest.TestCase):
    def setUp(self) -> None:
        clock.set_now("2026-11-08T12:00:00+08:00")
        self._dirs = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, config: Config | None = None) -> PMem:
        path = temp_dir("claim")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=config)
        self._apps.append(app)
        return app

    def _create(self, app: PMem, **kwargs) -> dict:
        payload = {
            "kind": "fact",
            "predicate": "住",
            "object": "杭州",
            "evidence_quote": "我住在杭州",
            "source_line": "line-1",
            "extractor": "rule:v1",
        }
        payload.update(kwargs)
        return claim.create(app.store.db, **payload)


class ClaimSchemaTest(ClaimCase):
    def test_fresh_db_has_claim_tables_and_version(self) -> None:
        app = self._open()
        conn = app.store.db.conn
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('claim','claim_link','person_card')"
            )
        }
        self.assertEqual(tables, {"claim", "claim_link", "person_card"})
        self.assertEqual(
            conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
            "1.11.0",
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        self.assertIn("claim_group", columns)

    def test_old_db_is_migrated_idempotently(self) -> None:
        app = self._open()
        app.store.add("旧记忆不能丢", session_id="s1")
        path = Path(app.store.db.path)
        app.close()
        raw = sqlite3.connect(path)
        try:
            raw.execute("ALTER TABLE memory DROP COLUMN claim_group")
            raw.execute("DROP TABLE claim")
            raw.execute("DROP TABLE claim_link")
            raw.execute("DROP TABLE person_card")
            raw.execute("UPDATE schema_meta SET value='1.10.0' WHERE key='schema_version'")
            raw.commit()
        finally:
            raw.close()
        reopened = PMem.open(path)
        self._apps.append(reopened)
        conn = reopened.store.db.conn
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        self.assertIn("claim_group", columns)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('claim','claim_link','person_card')"
            )
        }
        self.assertEqual(tables, {"claim", "claim_link", "person_card"})
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM memory WHERE text_raw='旧记忆不能丢'").fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
            "1.11.0",
        )

    def test_claim_switch_off_keeps_tables_empty_and_search_unchanged(self) -> None:
        app = self._open(Config({"claim": {"enabled": False}}))
        app.store.add("2026年11月5日和李静去看了房子", session_id="s1")
        before = app.store.search("李静 房子", k=8, record=False)
        for table in ("claim", "claim_link", "person_card"):
            count = app.store.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, table)
        after = app.store.search("李静 房子", k=8, record=False)
        self.assertEqual(
            json.dumps(before["items"], ensure_ascii=False, sort_keys=True),
            json.dumps(after["items"], ensure_ascii=False, sort_keys=True),
        )


class ClaimStructureGuardTest(ClaimCase):
    def test_bad_predicate_object_are_rejected(self) -> None:
        app = self._open()
        db = app.store.db
        for kwargs in (
            {"predicate": "别"},  # 单个副词
            {"predicate": "一" * 9},  # 超长谓词
            {"object": "很长" * 16},  # >30 字
            {"object": "我住在杭州。"},  # 含句读
        ):
            payload = {
                "kind": "fact",
                "predicate": "住",
                "object": "杭州",
                "evidence_quote": "我住在杭州",
                "source_line": "line-1",
            }
            payload.update(kwargs)
            with self.assertRaises(ValidationError, msg=str(kwargs)):
                claim.create(db, **payload)
        self.assertEqual(claim.list_claims(db, subject_id="self"), [])


class ClaimStoreTest(ClaimCase):
    def test_merge_same_fact_counts_use(self) -> None:
        app = self._open()
        first = self._create(app)
        second = self._create(app, source_line="line-2", evidence_quote="我就住杭州")
        self.assertEqual(first["action"], "inserted")
        self.assertEqual(second["action"], "merged")
        rows = claim.list_claims(app.store.db, subject_id="self")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["use_count"], 2)
        self.assertEqual(rows[0]["evidence_quote"], "我就住杭州")

    def test_idempotent_same_source_line(self) -> None:
        app = self._open()
        self._create(app)
        again = self._create(app)
        self.assertEqual(again["action"], "updated")
        rows = claim.list_claims(app.store.db, subject_id="self")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["use_count"], 2)

    def test_polarity_and_status_are_part_of_merge_key(self) -> None:
        app = self._open()
        self._create(app)
        self._create(app, polarity=0, source_line="line-2", evidence_quote="我不住杭州")
        self._create(app, status="planned", source_line="line-3", evidence_quote="我打算住杭州")
        rows = claim.list_claims(app.store.db, subject_id="self")
        self.assertEqual(len(rows), 3)

    def test_version_chain_supersedes_old(self) -> None:
        app = self._open()
        first = self._create(app, version=True, object="杭州", evidence_quote="我住在杭州")
        second = self._create(
            app,
            version=True,
            object="上海",
            evidence_quote="我搬到上海了",
            source_line="line-2",
            mentioned_at="2026-11-08T13:00:00+08:00",
        )
        self.assertEqual(first["action"], "inserted")
        self.assertEqual(second["action"], "versioned")
        self.assertEqual(second["prev_id"], first["claim_id"])
        old = claim.get(app.store.db, first["claim_id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], second["claim_id"])
        live = claim.list_claims(app.store.db, subject_id="self")
        self.assertEqual([row["object"] for row in live], ["上海"])
        link = app.store.db.conn.execute(
            "SELECT * FROM claim_link WHERE claim_id=?", (second["claim_id"],)
        ).fetchone()
        self.assertEqual(link["prev_id"], first["claim_id"])
        self.assertEqual(link["reason"], "object_changed")

    def test_list_and_search(self) -> None:
        app = self._open()
        self._create(app, kind="preference", predicate="不吃", object="香菜", polarity=0,
                     evidence_quote="我不吃香菜")
        self._create(app, kind="fact", predicate="住", object="杭州", evidence_quote="我住在杭州")
        db = app.store.db
        self.assertEqual(len(claim.list_claims(db, kind="preference")), 1)
        hits = claim.search(db, "香菜")
        self.assertEqual([row["object"] for row in hits], ["香菜"])
        self.assertEqual(claim.search(db, "不存在的词"), [])

    def test_soft_delete_keeps_tombstone_without_text(self) -> None:
        app = self._open()
        created = self._create(app, object="香菜", predicate="不吃", kind="preference",
                               polarity=0, evidence_quote="我不吃香菜")
        claim.soft_delete(app.store.db, created["claim_id"], reason="误抽")
        row = claim.get(app.store.db, created["claim_id"])
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(claim.list_claims(app.store.db, subject_id="self"), [])
        events = app.store.db.conn.execute(
            "SELECT payload_json FROM event_log WHERE op='claim.delete'"
        ).fetchall()
        self.assertTrue(events)
        payload = json.loads(events[-1]["payload_json"])
        self.assertNotIn("香菜", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload.get("reason"), "误抽")

    def test_person_card_aggregates_and_marks_contested(self) -> None:
        app = self._open()
        db = app.store.db
        claim.create(db, subject_id="mom", kind="third_party", predicate="住", object="杭州",
                     evidence_quote="我妈住在杭州", source_line="l1")
        claim.create(db, subject_id="mom", kind="third_party", predicate="住", object="上海",
                     evidence_quote="我妈搬到上海了", source_line="l2")
        claim.create(db, subject_id="mom", kind="boundary", predicate="别告诉", object="手术",
                     scope="private", evidence_quote="别告诉妈妈手术的事", source_line="l3")
        result = claim.refresh_person_card(db, "mom", relation="妈妈")
        self.assertEqual(result["facts"], 2)
        self.assertEqual(result["boundary"], 1)
        self.assertGreaterEqual(result["contested"], 1)
        card = claim.person_card(db, "mom")
        self.assertEqual(card["relation"], "妈妈")
        self.assertTrue(any(item["predicate"] == "住" for item in card["facts"]))
        contested = card["contested"]
        self.assertTrue(any(item["predicate"] == "住" for item in contested))


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
