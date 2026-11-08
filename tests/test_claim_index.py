"""claim 索引（task-45）：FTS 通道 + RRF + boundary 无条件注入 + 默认关逐字节等价。"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import claim
from pmem.memory.retrieval import claim_index

from .support import drop_dir, temp_dir

OFF_A = Config({"claim": {"index": {"enabled": False}, "enabled": False}})
OFF_B = Config({"claim": {"index": {"enabled": False}, "enabled": True}})
ON = Config({"claim": {"index": {"enabled": True}, "inject": {"boundary_limit": 3}}})
# 原话回查测试用：关掉 raw_fallback，让 journal 原文不自己进 payload，claim 才会补原话。
ON_NO_RAW = Config(
    {
        "retrieval": {"raw_fallback": False},
        "claim": {"index": {"enabled": True}, "inject": {"boundary_limit": 3}},
    }
)


class ClaimIndexCase(unittest.TestCase):
    def setUp(self) -> None:
        clock.set_now("2026-11-08T12:00:00+08:00")
        self._dirs = []
        self._apps: list[PMem] = []
        self._seed_dir = temp_dir("claim-index-seed")
        self._dirs.append(self._seed_dir)
        seed = PMem.open(self._seed_dir / "seed.db", config=Config())
        seed.store.add("2026年11月5日和李静去看了房子", session_id="s1",
                       created_at="2026-11-05T10:00:00+00:00")
        db = seed.store.db
        db.conn.execute(
            "INSERT INTO journal_line(id,subject_id,day_key,ts,role,text,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "line-pref",
                "self",
                "2026-11-08",
                "2026-11-08T10:00:00+08:00",
                "user",
                "今天聊到晚饭，我不吃香菜，所以点了别的菜。",
                "2026-11-08T10:00:00+08:00",
            ),
        )
        db.conn.commit()
        self.preference = claim.create(
            db, kind="preference", predicate="不吃", object="香菜", polarity=0,
            evidence_quote="我不吃香菜", source_line="line-pref",
            mentioned_at="2026-11-08T10:00:00+08:00",
        )
        self.boundary = claim.create(
            db, kind="boundary", predicate="别告诉", object="手术", polarity=1,
            scope="private", evidence_quote="别告诉妈妈手术的事", source_line="line-b1",
            mentioned_at="2026-11-07T20:00:00+08:00",
        )
        self.date_claim = claim.create(
            db, kind="date", predicate="event_on", object="2026-04-19", polarity=1,
            status="happened", event_date="2026-04-19",
            evidence_quote="想着这周先戒咖啡，到4月19号为止；", source_line="line-date",
            mentioned_at="2026-04-13T05:58:00+08:00",
        )
        for index in range(5):
            claim.create(
                db, kind="boundary", predicate="别提", object=f"敏感事{index}", polarity=1,
                scope="private", evidence_quote=f"别提敏感事{index}", source_line=f"line-b{index+2}",
                mentioned_at="2026-11-07T20:00:00+08:00",
            )
        close_path = self._seed_dir / "seed.db"
        seed.close()
        self._seed_path = close_path

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open_copy(self, config: Config) -> PMem:
        path = temp_dir("claim-index")
        self._dirs.append(path)
        db_path = path / "pmem.db"
        shutil.copyfile(self._seed_path, db_path)
        app = PMem.open(db_path, config=config)
        self._apps.append(app)
        return app

    def _search(self, app: PMem, query: str = "香菜"):
        return app.store.search(query, k=8, record=False)


class DisabledEquivalenceTest(ClaimIndexCase):
    def test_off_path_is_byte_identical(self) -> None:
        app_a = self._open_copy(OFF_A)
        app_b = self._open_copy(OFF_B)
        app_a.store.search("香菜", k=8, record=False)  # 预热
        app_b.store.search("香菜", k=8, record=False)
        conn_a, conn_b = app_a.store.db.conn, app_b.store.db.conn
        stmts_a: list[str] = []
        stmts_b: list[str] = []
        conn_a.set_trace_callback(stmts_a.append)
        conn_b.set_trace_callback(stmts_b.append)
        found_a = app_a.store.search("香菜", k=8, record=False)
        found_b = app_b.store.search("香菜", k=8, record=False)
        conn_a.set_trace_callback(None)
        conn_b.set_trace_callback(None)
        for payload in (found_a, found_b):
            payload.pop("latency_ms", None)
            if isinstance(payload.get("meta", {}).get("retrieval"), dict):
                payload["meta"]["retrieval"].pop("latency_ms", None)
        self.assertEqual(
            json.dumps(found_a, ensure_ascii=False, sort_keys=True),
            json.dumps(found_b, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(stmts_a, stmts_b)
        self.assertFalse(
            any(stmt.strip().upper().startswith("CREATE VIRTUAL TABLE") or "claim_fts" in stmt
                for stmt in stmts_a),
            "关闭时不应碰 claim 索引",
        )


class EnabledTest(ClaimIndexCase):
    def test_claim_hits_join_search_with_source_day(self) -> None:
        app = self._open_copy(ON_NO_RAW)
        found = self._search(app, "香菜")
        claims = [
            item for item in found["items"]
            if item.get("source") == "claim" and item.get("claim_kind") == "preference"
        ]
        self.assertTrue(claims, found["items"])
        item = claims[0]
        self.assertEqual(item["claim_kind"], "preference")
        self.assertIn("香菜", item["text"])
        self.assertIn("你说过", item["text"])
        self.assertIn("2026-11-08", item["text"])
        # task-48：claim 只当索引，注入文本必须含回查到的原话。
        self.assertIn("今天聊到晚饭", item["text"])
        self.assertIn("我不吃香菜", item["text"])
        self.assertEqual(item.get("original"), "今天聊到晚饭，我不吃香菜，所以点了别的菜。")
        self.assertEqual(item["event_date"], None)
        self.assertEqual(item["mentioned_at"], "2026-11-08T10:00:00+08:00")

    def test_boundary_claims_are_injected_unconditionally(self) -> None:
        app = self._open_copy(ON)
        found = self._search(app, "今天天气怎么样")
        boundary_items = [
            item for item in found["items"]
            if item.get("source") == "claim" and item.get("claim_kind") == "boundary"
        ]
        self.assertTrue(boundary_items, found["items"])
        self.assertLessEqual(len(boundary_items), 3)
        self.assertTrue(any(item.get("scope") == "private" for item in boundary_items))

    def test_date_forms_are_interoperable_and_human_readable(self) -> None:
        app = self._open_copy(ON)
        for query in ("4月19号", "2026-04-19"):
            found = self._search(app, query)
            dates = [
                item for item in found["items"]
                if item.get("source") == "claim" and item.get("claim_kind") == "date"
            ]
            self.assertTrue(dates, f"query={query} items={found['items']}")
            text = dates[0]["text"]
            self.assertIn("4月19号", text)
            self.assertIn("2026-04-19", text)
            self.assertNotIn("event_on", text)
            self.assertNotIn("event_on", json.dumps(found["items"], ensure_ascii=False))

    def test_boundary_injection_does_not_drop_lexical_hits(self) -> None:
        off_app = self._open_copy(OFF_A)
        on_app = self._open_copy(ON)
        off = off_app.store.search("李静 房子", k=8, record=False)
        on = on_app.store.search("李静 房子", k=8, record=False)
        off_ids = {item["id"] for item in off["items"] if not item.get("source") == "claim"}
        on_ids = {item["id"] for item in on["items"] if not item.get("source") == "claim"}
        self.assertTrue(off_ids)
        self.assertEqual(off_ids, on_ids, "boundary 注入不应该挤掉任何 lexical 命中")
        boundary_items = [
            item for item in on["items"]
            if item.get("source") == "claim" and item.get("claim_kind") == "boundary"
        ]
        self.assertTrue(boundary_items)

    def test_same_source_memory_is_not_duplicated(self) -> None:
        app = self._open_copy(ON)
        app.store.add(
            "今天聊到晚饭，我不吃香菜，所以点了别的菜。",
            session_id="s1",
            source_kind="journal",
            source_ref="line-pref",
        )
        found = app.store.search("香菜", k=8, record=False)
        # 同 source_line 的 memory 已进 top-k → 不再重复注入同来源 claim。
        # boundary 是另一类（后置追加），这里只看同来源偏好 claim。
        self.assertFalse(
            [
                item for item in found["items"]
                if item.get("source") == "claim" and item.get("claim_kind") == "preference"
            ],
            found["items"],
        )
        self.assertTrue(
            any("我不吃香菜" in item.get("text", "") for item in found["items"])
        )

    def test_claim_injection_keeps_memory_count_and_char_cap(self) -> None:
        off_app = self._open_copy(OFF_A)
        on_app = self._open_copy(ON)
        off = off_app.store.search("香菜", k=8, record=False)
        on = on_app.store.search("香菜", k=8, record=False)
        off_texts = [item["text"] for item in off["items"]]
        on_memory = [item for item in on["items"] if item.get("source") != "claim"]
        on_claim = [item for item in on["items"] if item.get("source") == "claim"]
        self.assertEqual(
            off_texts, [item["text"] for item in on_memory],
            "claim 注入不能改变/减少 memory 条目",
        )
        self.assertTrue(on_claim, "claim 命中应在剩余预算内追加")
        self.assertLessEqual(sum(len(item["text"]) for item in on_claim), 600 * 0.25)

    def test_index_rebuild_and_verify(self) -> None:
        app = self._open_copy(ON)
        db = app.store.db
        self.assertTrue(claim_index.ensure(db))
        rebuilt = claim_index.rebuild(db)
        self.assertGreaterEqual(rebuilt, 1)
        self.assertTrue(claim_index.verify(db)["ok"])
        # 新 claim 进来后 search 的 sync 必须能看见（marker 变化 → 重建）。
        claim.create(
            db, kind="preference", predicate="不喝", object="咖啡", polarity=0,
            evidence_quote="我不喝咖啡", source_line="line-new",
            mentioned_at="2026-11-08T11:00:00+08:00",
        )
        found = app.store.search("咖啡", k=8, record=False)
        texts = [item["text"] for item in found["items"] if item.get("source") == "claim"]
        self.assertTrue(any("咖啡" in text for text in texts), texts)


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
