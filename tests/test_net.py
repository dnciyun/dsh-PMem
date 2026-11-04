"""M3：实体识别、关系抽取、同名消歧、别名、图查询、休眠、接口。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.net import entities
from pmem.ops import invariants

from .support import InProcessMcp, drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m3")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _edges(self, sql: str = "SELECT * FROM relation", params: tuple = ()) -> list:
        return list(self.db.conn.execute(sql, params).fetchall())

    def _entity(self, name: str):
        rows = entities.find(self.db, subject="self", name=name)
        return rows[0] if rows else None


class TestExtraction(Case):
    def test_mentions_are_typed_and_non_overlapping(self) -> None:
        mentions = entities.extract_mentions("陈默在字节工作，喜欢咖啡")
        got = {(m["surface"], m["type"]) for m in mentions}
        self.assertIn(("陈默", "person"), got)
        self.assertIn(("字节", "org"), got)
        self.assertIn(("咖啡", "concept"), got)
        spans = sorted((m["start"], m["end"]) for m in mentions)
        for left, right in zip(spans, spans[1:]):
            self.assertLessEqual(left[1], right[0])

    def test_three_char_name_not_eaten_by_verb(self) -> None:
        mentions = entities.extract_mentions("王小明住在杭州")
        self.assertIn("王小明", {m["surface"] for m in mentions})
        self.assertNotIn("王小明住", {m["surface"] for m in mentions})

    def test_two_char_name_before_negative(self) -> None:
        mentions = entities.extract_mentions("陈默不喜欢做饭")
        self.assertIn("陈默", {m["surface"] for m in mentions})

    def test_alias_templates(self) -> None:
        self.assertIn(
            ("张伟", "小伟"), entities.extract_aliases("张伟（小伟）")
        )
        self.assertIn(
            ("李雷", "小李"), entities.extract_aliases("李雷也叫小李")
        )

    def test_context_slots_are_typed(self) -> None:
        slots = entities._context_slots("陈默在字节工作")
        self.assertIn("works_at:字节", slots)
        slots = entities._context_slots("我住在杭州")
        self.assertIn("lives_in:杭州", slots)
        # 未知宾语不参与消歧
        self.assertFalse(entities._context_slots("我在考虑换工作"))


class TestRelationIngest(Case):
    def test_basic_facts_create_edges(self) -> None:
        self.store.add("陈默在字节工作，陈默喜欢咖啡，陈默住在杭州")
        triples = {
            (src, pred, dst)
            for src, pred, dst in self.db.conn.execute(
                "SELECT s.name, r.predicate, d.name FROM relation r "
                "JOIN entity s ON s.id=r.src_id JOIN entity d ON d.id=r.dst_id"
            )
        }
        self.assertIn(("陈默", "works_at", "字节"), triples)
        self.assertIn(("陈默", "likes", "咖啡"), triples)
        self.assertIn(("陈默", "lives_in", "杭州"), triples)

    def test_self_is_the_default_subject(self) -> None:
        self.store.add("我住在杭州")
        row = self.db.conn.execute(
            "SELECT s.name FROM relation r JOIN entity s ON s.id=r.src_id WHERE r.predicate='lives_in'"
        ).fetchone()
        self.assertEqual(row["name"], "我")

    def test_question_is_not_a_fact(self) -> None:
        self.store.add("我喜欢咖啡吗？")
        self.assertEqual(self._edges(), [])

    def test_third_person_without_name_is_skipped(self) -> None:
        self.store.add("他喜欢咖啡")
        self.assertEqual(self._edges(), [])

    def test_unknown_object_creates_no_edge(self) -> None:
        self.store.add("陈默喜欢frobnicate")
        self.assertEqual(self._edges(), [])

    def test_domain_mismatch_is_skipped(self) -> None:
        self.store.add("我在咖啡工作")
        works = [e for e in self._edges() if e["predicate"] == "works_at"]
        self.assertEqual(works, [])

    def test_evidence_points_to_memory(self) -> None:
        result = self.store.add("我住在杭州")
        edge = self._edges()[0]
        evidence = json.loads(edge["evidence_json"])
        self.assertEqual(evidence[0]["memory_id"], result["id"])

    def test_repeat_reinforces_same_edge(self) -> None:
        self.store.add("我住在杭州", session_id="s1")
        first = self._edges()[0]
        self.store.add("我现在住在杭州", session_id="s2")
        edges = self._edges()
        self.assertEqual(len(edges), 1)
        evidence = json.loads(edges[0]["evidence_json"])
        self.assertEqual(len(evidence), 2)
        self.assertGreater(edges[0]["confidence"], first["confidence"])

    def test_same_memory_twice_does_not_duplicate_evidence(self) -> None:
        self.store.add("我住在杭州")
        self.store.add("我住在杭州")
        edges = self._edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(len(json.loads(edges[0]["evidence_json"])), 1)


    def test_hard_delete_cleans_evidence(self) -> None:
        result = self.store.add("我住在杭州")
        self.store.delete(result["id"], hard=True)
        self.assertEqual(self._edges(), [])
        checks = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertTrue(checks["I4"]["ok"], checks["I4"])


class TestResolution(Case):
    def test_same_name_different_jobs_splits_identity(self) -> None:
        self.store.add("小王在字节工作")
        self.store.add("小王在美团工作")
        rows = entities.find(self.db, subject="self", name="小王")
        self.assertEqual(len(rows), 2)
        works = [
            r for r in self.db.conn.execute(
                "SELECT s.id, d.name dst FROM relation r JOIN entity s ON s.id=r.src_id "
                "JOIN entity d ON d.id=r.dst_id WHERE r.predicate='works_at'"
            )
        ]
        self.assertEqual({r["dst"] for r in works}, {"字节", "美团"})
        self.assertEqual(len({r["id"] for r in works}), 2)

    def test_context_free_name_is_ambiguous(self) -> None:
        self.store.add("小王在字节工作")
        self.store.add("小王在美团工作")
        result = entities.resolve(self.db, subject="self", name="小王", type_hint="person")
        self.assertTrue(result["ambiguous"])
        self.assertIsNone(result["entity"])
        self.assertGreaterEqual(len(result["candidates"]), 2)

    def test_alias_resolves_to_original(self) -> None:
        self.store.add("李默也叫小舟")
        self.store.add("小舟在华为工作")
        rows = entities.find(self.db, subject="self", name="小舟")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "李默")
        edge = self.db.conn.execute(
            "SELECT s.name FROM relation r JOIN entity s ON s.id=r.src_id WHERE r.predicate='works_at'"
        ).fetchone()
        self.assertEqual(edge["name"], "李默")

    def test_alias_after_entity_merges(self) -> None:
        # 别名晚于第一次出现：小舟先被当成独立人物，显式别名要把它并回来
        self.store.add("小舟在华为工作")
        self.store.add("李默也叫小舟")
        rows = entities.find(self.db, subject="self", name="小舟")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "李默")

    def test_merge_moves_relations(self) -> None:
        self.store.add("小王在字节工作")
        self.store.add("小王在美团工作")
        rows = entities.find(self.db, subject="self", name="小王")
        keep, drop = rows[0]["id"], rows[1]["id"]
        result = entities.merge(self.db, keep, drop)
        self.assertEqual(result["merged"], drop)
        status = self.db.conn.execute("SELECT status FROM entity WHERE id=?", (drop,)).fetchone()["status"]
        self.assertEqual(status, "merged")
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM relation WHERE src_id=? AND status='active'", (keep,)
            ).fetchone()["c"],
            2,
        )

    def test_ambiguous_query_returns_conflict(self) -> None:
        self.store.add("小王在字节工作")
        self.store.add("小王在美团工作")
        result = self.app.relation(action="query", src="小王")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_CONFLICT")


class TestGraph(Case):
    def setUp(self) -> None:
        super().setUp()
        self.store.add("我住在杭州")
        self.store.add("我去了中国")  # 只为建出"中国"实体，不产生边
        added = self.app.relation(
            action="add", src="杭州", dst="中国", predicate="located_in", weight=0.8
        )
        self.assertTrue(added["ok"], added)

    def test_one_hop(self) -> None:
        result = self.app.relation(action="query", src="我", depth=1)
        self.assertTrue(result["ok"])
        items = result["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["dst"]["name"], "杭州")
        self.assertEqual(items[0]["hops"], 1)

    def test_two_hop(self) -> None:
        result = self.app.relation(action="query", src="我", depth=2)
        names = {item["dst"]["name"] for item in result["data"]["items"]}
        self.assertIn("杭州", names)
        self.assertIn("中国", names)
        two = [i for i in result["data"]["items"] if i["hops"] == 2][0]
        self.assertEqual(two["path"], ["我", "杭州", "中国"])

    def test_between_endpoints(self) -> None:
        result = self.app.relation(action="query", src="我", dst="中国", depth=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["items"][0]["hops"], 2)

    def test_predicate_filter(self) -> None:
        result = self.app.relation(action="query", src="我", predicate="located_in", depth=2)
        self.assertEqual(result["data"]["items"], [])

    def test_depth_validation(self) -> None:
        result = self.app.relation(action="query", src="我", depth=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_VALIDATION")

    def test_missing_src_is_not_found(self) -> None:
        result = self.app.relation(action="query", src="不存在的人")
        self.assertEqual(result["data"]["items"], [])


class TestDormancy(Case):
    def test_dormant_then_reactivated(self) -> None:
        self.store.add("我住在杭州")
        self.store.add("我去了中国")
        self.app.relation(action="add", src="我", dst="中国", predicate="related_to", weight=0.2)
        self.db.conn.execute(
            "UPDATE relation SET last_support_at='2020-01-01T00:00:00.000Z' "
            "WHERE predicate='related_to'"
        )
        tick = self.app.net_dormant(days=90)
        self.assertTrue(tick["ok"])
        self.assertGreaterEqual(tick["data"]["dormant"], 1)
        hidden = self.app.relation(action="query", src="我", predicate="related_to", depth=1)
        self.assertEqual(hidden["data"]["items"], [])
        shown = self.app.relation(
            action="query", src="我", predicate="related_to", depth=1, include_dormant=True
        )
        self.assertEqual(len(shown["data"]["items"]), 1)
        # 新证据唤醒（不新增重复行）
        self.app.relation(action="add", src="我", dst="中国", predicate="related_to", weight=0.3)
        revived = self.app.relation(action="query", src="我", predicate="related_to", depth=1)
        self.assertEqual(len(revived["data"]["items"]), 1)
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM relation WHERE predicate='related_to'"
            ).fetchone()["c"],
            1,
        )


class TestInvariants(Case):
    def test_weight_cap_detected(self) -> None:
        self.store.add("我住在杭州")
        self.db.conn.execute("UPDATE relation SET weight=0.99 WHERE predicate='lives_in'")
        result = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertFalse(result["I14"]["ok"])

    def test_mention_span_detected(self) -> None:
        self.store.add("我住在杭州")
        self.db.conn.execute("UPDATE entity_mention SET span_end=999 WHERE seq=1")
        result = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertFalse(result["I15"]["ok"])

    def test_manual_edge_has_evidence_marker(self) -> None:
        self.store.add("我住在杭州")
        self.store.add("我去了中国")
        self.app.relation(action="add", src="杭州", dst="中国", predicate="located_in")
        checks = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertTrue(checks["I3"]["ok"], checks["I3"])
        self.assertTrue(checks["I4"]["ok"], checks["I4"])


class TestGate(Case):
    def test_capabilities_include_net(self) -> None:
        caps = self.app.capabilities()["data"]
        self.assertTrue(caps["features"]["net"])

    def test_cli_net_stats(self) -> None:
        run_cli(["--db", str(self.db_path), "add", "我住在杭州"])
        code, out = run_cli(["--db", str(self.db_path), "--json", "net", "stats"])
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertGreaterEqual(data["entities"], 2)
        self.assertGreaterEqual(data["relations"], 1)

    def test_cli_net_query(self) -> None:
        run_cli(["--db", str(self.db_path), "add", "我住在杭州"])
        code, out = run_cli(["--db", str(self.db_path), "--json", "net", "query", "我"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["items"][0]["dst"]["name"], "杭州")

    def test_mcp_relation_tool(self) -> None:
        client = InProcessMcp(self.db_path)
        try:
            client.tool("memory_add", {"text": "我住在杭州"})
            result = client.tool("memory_relation", {"action": "query", "src": "我"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["items"][0]["dst"]["name"], "杭州")
        finally:
            client.close()

    def test_mcp_relation_add(self) -> None:
        client = InProcessMcp(self.db_path)
        try:
            client.tool("memory_add", {"text": "我住在杭州"})
            client.tool("memory_add", {"text": "我去了中国"})
            added = client.tool(
                "memory_relation",
                {"action": "add", "src": "杭州", "dst": "中国", "predicate": "located_in"},
            )
            self.assertTrue(added["ok"], added)
            self.assertEqual(added["data"]["predicate"], "located_in")
        finally:
            client.close()


class TestEvalNet(unittest.TestCase):
    def test_synthetic_gold_is_disjoint_and_labeled(self) -> None:
        from pmem.eval import net_synthetic

        data = net_synthetic.build(3, people=6)
        names = set(data["relation_names"])
        self.assertTrue(names.isdisjoint({item["name"] for item in data["disambig"]}))
        self.assertTrue(names.isdisjoint({item["name"] for item in data["aliases"]}))
        self.assertTrue(data["gold"])
        self.assertTrue(all(len(item) == 3 for item in data["gold"]))

    def test_hard_set_has_gold(self) -> None:
        from pmem.eval import net_synthetic

        hard = net_synthetic.hard()
        self.assertTrue(hard["memories"])
        self.assertTrue(hard["gold"])
