"""M2：去重、冲突、反馈、生命周期、不变量、评测台。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.base.db import Database
from pmem.eval import runner, synthetic
from pmem.gate.sdk import PMem
from pmem.memory import conflict as C
from pmem.memory import dup as D
from pmem.memory import feedback as F
from pmem.memory import lifecycle as L
from pmem.ops import invariants

from .support import drop_dir, temp_dir


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("m2")
        self.app = PMem.open(self.dir / "pmem.db")
        self.store = self.app.store
        self.db = self.app.db

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self.dir)


class TestSimilarity(unittest.TestCase):
    def test_identical(self) -> None:
        self.assertEqual(D.similarity("我住在杭州", "我住在杭州"), 1.0)

    def test_near(self) -> None:
        # 短句上"插入一个副词"的相似度并不高（0.38），所以这类重复靠 claim_sig 而非 similarity
        self.assertGreater(D.similarity("我住在杭州", "我现在住在杭州"), 0.3)
        self.assertLess(D.similarity("我住在杭州", "我现在住在杭州"), 0.92)

    def test_disjoint(self) -> None:
        self.assertLess(D.similarity("我住在杭州", "明天要交报告"), 0.2)


class TestDedup(Case):
    def test_sig_duplicate_detected(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我现在住在杭州")
        groups = D.find_duplicate_groups(self.db)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["members"]), {a["id"], b["id"]})
        self.assertEqual(groups[0]["reason"], "sig")

    def test_different_value_not_duplicate(self) -> None:
        self.store.add("我住在杭州")
        self.store.add("我住在上海")
        self.assertEqual(D.find_duplicate_groups(self.db), [])

    def test_merge_points_to_representative(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我现在住在杭州")
        stats = D.consolidate(self.db)
        self.assertEqual(stats["merged"], 1)
        row = self.db.conn.execute("SELECT canonical_id, status FROM memory WHERE id=?", (b["id"],)).fetchone()
        self.assertEqual(row["canonical_id"], a["id"])
        self.assertEqual(row["status"], "archived")

    def test_merge_keeps_raw_text(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我现在住在杭州")
        D.consolidate(self.db)
        item = self.store.get(b["id"], include_deleted=True)
        self.assertEqual(item["text"], "我现在住在杭州")
        self.assertIsNone(item["superseded_by"])

    def test_consolidate_is_idempotent(self) -> None:
        self.store.add("我住在杭州")
        self.store.add("我现在住在杭州")
        D.consolidate(self.db)
        second = D.consolidate(self.db)
        self.assertEqual(second["merged"], 0)

    def test_dry_run_changes_nothing(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我现在住在杭州")
        stats = D.consolidate(self.db, dry_run=True)
        self.assertTrue(stats["dry_run"])
        row = self.db.conn.execute("SELECT canonical_id FROM memory WHERE id=?", (b["id"],)).fetchone()
        self.assertIsNone(row["canonical_id"])


class TestConflictSig(unittest.TestCase):
    def test_parse_slot(self) -> None:
        parsed = C.parse_sig("slot:self|lives_in|杭州|yes")
        self.assertEqual(parsed["pred"], "lives_in")
        self.assertEqual(parsed["obj"], "杭州")

    def test_parse_non_slot(self) -> None:
        self.assertIsNone(C.parse_sig("def:a|b"))
        self.assertIsNone(C.parse_sig("txt:abc"))


class TestConflict(Case):
    def test_slot_conflict(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我住在上海")
        found = C.detect(self.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "slot")
        self.assertEqual({found[0]["left_id"], found[0]["right_id"]}, {a["id"], b["id"]})

    def test_multi_valued_predicate_not_conflict(self) -> None:
        self.store.add("我喜欢咖啡")
        self.store.add("我喜欢茶")
        self.assertEqual(C.detect(self.db), [])

    def test_negation_conflict(self) -> None:
        self.store.add("我喜欢喝酒")
        self.store.add("我不喜欢喝酒")
        found = C.detect(self.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "negation")

    def test_temporal_disjoint_not_conflict(self) -> None:
        self.store.add(
            "我住在杭州",
            valid_from="2020-01-01T00:00:00.000Z",
            valid_to="2025-12-31T00:00:00.000Z",
        )
        self.store.add("我住在上海", valid_from="2026-01-01T00:00:00.000Z")
        self.assertEqual(C.detect(self.db), [])

    def test_persist_marks_contested(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我住在上海")
        stats = C.run(self.db)
        self.assertEqual(stats["created"], 1)
        for memory_id in (a["id"], b["id"]):
            self.assertEqual(self.store.get(memory_id)["status"], "contested")

    def test_persist_is_idempotent(self) -> None:
        self.store.add("我住在杭州")
        self.store.add("我住在上海")
        C.run(self.db)
        second = C.run(self.db)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped"], 1)

    def test_resolve_prefer_left(self) -> None:
        a = self.store.add("我住在杭州")
        b = self.store.add("我住在上海")
        C.run(self.db)
        conflict_id = C.list_conflicts(self.db)[0]["id"]
        C.resolve(self.db, conflict_id, "prefer_left")
        self.assertEqual(self.store.get(a["id"])["status"], "active")
        self.assertEqual(self.store.get(b["id"])["status"], "archived")

    def test_resolve_dismissed_restores(self) -> None:
        a = self.store.add("我住在杭州")
        self.store.add("我住在上海")
        C.run(self.db)
        conflict_id = C.list_conflicts(self.db)[0]["id"]
        C.resolve(self.db, conflict_id, "dismissed")
        self.assertEqual(self.store.get(a["id"])["status"], "active")

    def test_resolve_unknown_id(self) -> None:
        with self.assertRaises(KeyError):
            C.resolve(self.db, "01JCV0000000000000000000000", "prefer_left")


class TestFeedback(Case):
    def test_useful_bumps_importance(self) -> None:
        item = self.store.add("有用测试")
        before = self.store.get(item["id"])["importance"]
        F.apply(self.db, item["id"], "useful")
        self.assertGreater(self.store.get(item["id"])["importance"], before)

    def test_wrong_halves_confidence_and_contests(self) -> None:
        item = self.store.add("我住在杭州")
        before = self.store.get(item["id"])["confidence"]
        F.apply(self.db, item["id"], "wrong")
        after = self.store.get(item["id"])
        self.assertAlmostEqual(after["confidence"], round(before * 0.5, 4), places=3)
        self.assertEqual(after["status"], "contested")

    def test_confirm_restores_active(self) -> None:
        item = self.store.add("我住在杭州")
        F.apply(self.db, item["id"], "wrong")
        F.apply(self.db, item["id"], "confirm")
        self.assertEqual(self.store.get(item["id"])["status"], "active")

    def test_pin_and_unpin(self) -> None:
        item = self.store.add("置顶测试")
        F.apply(self.db, item["id"], "pin")
        self.assertTrue(self.store.get(item["id"])["pinned"])
        F.apply(self.db, item["id"], "unpin")
        self.assertFalse(self.store.get(item["id"])["pinned"])

    def test_feedback_recorded(self) -> None:
        item = self.store.add("反馈记录")
        F.apply(self.db, item["id"], "irrelevant", note="不相关")
        rows = F.recent(self.db)
        self.assertEqual(rows[0]["signal"], "irrelevant")
        self.assertEqual(rows[0]["note"], "不相关")

    def test_invalid_signal(self) -> None:
        item = self.store.add("x")
        with self.assertRaises(ValueError):
            F.apply(self.db, item["id"], "nonsense")


class TestLifecycle(Case):
    def test_stale_candidate_discarded(self) -> None:
        item = self.store.add("用户周三常问财报", provenance="observed", created_at="2020-01-01T00:00:00.000Z")
        self.assertEqual(self.store.get(item["id"])["status"], "candidate")
        stats = L.tick(self.db, candidate_discard_days=30)
        self.assertEqual(stats["by_status"].get("discarded"), 1)
        self.assertEqual(self.store.get(item["id"], include_deleted=True)["status"], "discarded")

    def test_used_candidate_promoted(self) -> None:
        item = self.store.add("用户周三常问财报", provenance="observed")
        self.db.conn.execute("UPDATE memory SET use_count=1 WHERE id=?", (item["id"],))
        L.tick(self.db)
        self.assertEqual(self.store.get(item["id"])["status"], "active")

    def test_idle_active_becomes_dormant(self) -> None:
        item = self.store.add("很久没用的记忆")
        self.db.conn.execute(
            "UPDATE memory SET last_used_at='2020-01-01T00:00:00.000Z' WHERE id=?", (item["id"],)
        )
        stats = L.tick(self.db, active_dormant_days=90)
        self.assertEqual(stats["by_status"].get("dormant"), 1)

    def test_pinned_never_moves(self) -> None:
        item = self.store.add("钉住的记忆", pinned=True)
        self.db.conn.execute(
            "UPDATE memory SET last_used_at='2020-01-01T00:00:00.000Z' WHERE id=?", (item["id"],)
        )
        L.tick(self.db, active_dormant_days=1)
        self.assertEqual(self.store.get(item["id"])["status"], "active")

    def test_dry_run(self) -> None:
        item = self.store.add("x", provenance="observed", created_at="2020-01-01T00:00:00.000Z")
        stats = L.tick(self.db, candidate_discard_days=30, dry_run=True)
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(self.store.get(item["id"])["status"], "candidate")


class TestInvariants(Case):
    def test_clean_db_passes(self) -> None:
        self.store.add("干净数据")
        results = invariants.check_all(self.db)
        failed = [r for r in results if not r["ok"]]
        self.assertEqual(failed, [], failed)

    def test_bad_scope_detected(self) -> None:
        item = self.store.add("坏作用域")
        self.db.conn.execute("UPDATE memory SET scope_key='bogus' WHERE id=?", (item["id"],))
        results = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertFalse(results["I13"]["ok"])

    def test_tampered_chain_detected(self) -> None:
        self.store.add("篡改测试")
        self.db.conn.execute("UPDATE event_log SET payload_json='{}' WHERE seq=1")
        results = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertFalse(results["I6"]["ok"])

    def test_summary_shape(self) -> None:
        summary = invariants.summary(invariants.check_all(self.db))
        self.assertEqual(summary["total"], 19)  # 1.11.0 加了 I20；0.30.0 加 I21/I22（I7/I10/I11 不在 check_all）
        self.assertTrue(summary["ok"])


class TestEvalHarness(Case):
    def test_synthetic_has_labels(self) -> None:
        data = synthetic.build(3, max_groups=4)
        self.assertTrue(data["memories"])
        self.assertTrue(data["dup_pairs"])
        self.assertTrue(data["conflict_pairs"])
        self.assertTrue(data["queries"])

    def test_runner_passes_gates(self) -> None:
        metrics = runner.run(seed=5, out_dir=self.dir / "eval", max_groups=8)
        self.assertTrue(metrics["ok"], metrics["gates"])
        self.assertEqual(metrics["poison"]["count"], 0)
        self.assertGreaterEqual(metrics["recall"]["lexical_recall_at_k"], 0.8)
        self.assertGreaterEqual(metrics["dedup"]["precision"], 0.9)
        self.assertGreaterEqual(metrics["conflict"]["f1"], 0.75)

    def test_paraphrase_recall_is_documented_gap(self) -> None:
        metrics = runner.run(seed=5, out_dir=self.dir / "eval2", max_groups=8)
        # 没有向量就几乎召回不到"换了说法"的查询：这不是 bug，是要在 M2b 补的缺口
        self.assertLessEqual(metrics["recall"]["paraphrase_recall_at_k"], 0.34)

    def test_deterministic(self) -> None:
        first = runner.run(seed=5, out_dir=self.dir / "eval3", max_groups=6)
        second = runner.run(seed=5, out_dir=self.dir / "eval4", max_groups=6)
        self.assertEqual(first["dedup"]["precision"], second["dedup"]["precision"])
        self.assertEqual(first["conflict"]["f1"], second["conflict"]["f1"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
