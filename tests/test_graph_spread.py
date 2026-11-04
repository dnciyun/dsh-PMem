"""1.8.0 / 0.12.0：关联扩散通道（稀有词共现图 + PPR）。

三条要守住的东西：

1. 图是**派生**的：同样的记忆进来，rebuild 必须给出同样的边（可重放）；
2. 增量连边和整表重建**拓扑一致**（权重允许因为 df 变化有小数差）；
3. 扩散只能**补候选/提邻居**，词法强覆盖时不许动排序。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import graph, query
from pmem.ops import invariants


def _edges(conn) -> set[tuple[int, int]]:
    rows = conn.execute("SELECT a_seq, b_seq FROM memory_edge").fetchall()
    return {(int(r["a_seq"]), int(r["b_seq"])) for r in rows}


class TestGraphBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        self.app = PMem.open(self.db_path, config=Config())
        self.app.db.init()
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _add(self, text: str) -> dict:
        return self.store.add(text, kind="episodic", provenance="user_explicit")

    def test_related_memories_get_edge(self) -> None:
        self._add("用户周末骑自行车去修车摊，师傅说后闸松了要换线管")
        self._add("用户第一次修自行车花十五块换线管，修车摊就在楼下")
        self._add("用户今天在公司评审需求，改了三版原型")
        edges = _edges(self.app.db.conn)
        self.assertTrue(edges, "共享稀有词的记忆之间应该有边")
        self.assertEqual(len(edges), 1, f"不相关的记忆不该连边：{edges}")

    def test_unrelated_memories_have_no_edge(self) -> None:
        # 库太小的时候「稀有」没有参照系（共享「用户」两个字也能连边），
        # 所以先垫到 8 条无关记忆再整表重建，按当前库规模判定稀有度。
        for text in (
            "今天在公司评审需求，需求文档改了三版原型",
            "养的猫叫年糕，半夜踩人脸叫起床",
            "周末跟朋友去打球，输了三百块",
            "工资的一半存了定期，剩下当生活费",
            "最近在追一部日剧，看到第八集",
            "楼下新开了家面馆，豌杂面很香",
            "年终奖算下来还是不够换电脑",
            "早上坐地铁上班，单程五十分钟",
        ):
            self._add(text)
        graph.rebuild(self.app.db)
        self.assertFalse(_edges(self.app.db.conn), f"不相干的记忆不该连边：{_edges(self.app.db.conn)}")

    def test_incremental_matches_rebuild_topology(self) -> None:
        for text in (
            "用户周末骑自行车去修车摊，师傅说后闸松了要换线管",
            "用户第一次修自行车花十五块换线管，修车摊就在楼下",
            "用户把自行车内胎换了，花了二十块",
            "用户今天在公司评审需求，改了三版原型",
        ):
            self._add(text)
        before = _edges(self.app.db.conn)
        graph.rebuild(self.app.db)
        after = _edges(self.app.db.conn)
        self.assertEqual(before, after, "增量连边和整表重建的拓扑必须一致")

    def test_rebuild_is_idempotent(self) -> None:
        for text in ("用户骑自行车去修车摊换线管", "用户修自行车换了后闸", "用户给猫换了猫砂"):
            self._add(text)
        first = graph.rebuild(self.app.db)
        rows1 = sorted(
            (int(r["a_seq"]), int(r["b_seq"]), round(float(r["w"]), 6))
            for r in self.app.db.conn.execute("SELECT * FROM memory_edge")
        )
        second = graph.rebuild(self.app.db)
        rows2 = sorted(
            (int(r["a_seq"]), int(r["b_seq"]), round(float(r["w"]), 6))
            for r in self.app.db.conn.execute("SELECT * FROM memory_edge")
        )
        self.assertEqual(rows1, rows2)
        self.assertEqual(first["edges"], second["edges"])

    def test_ensure_backfills_old_store(self) -> None:
        for text in ("用户骑自行车去修车摊换线管", "用户修自行车换了后闸"):
            self._add(text)
        self.app.db.conn.execute("DELETE FROM memory_edge")
        self.app.db.conn.execute("DELETE FROM schema_meta WHERE key='graph_covered'")
        info = graph.ensure(self.app.db)
        self.assertTrue(info["rebuilt"])
        self.assertTrue(_edges(self.app.db.conn))

    def test_doctor_flags_noncanonical_edge(self) -> None:
        self._add("用户骑自行车去修车摊换线管")
        self._add("用户修自行车换了后闸")
        conn = self.app.db.conn
        conn.execute("DELETE FROM memory_edge")
        seqs = [int(r["seq"]) for r in conn.execute("SELECT seq FROM memory ORDER BY seq")]
        conn.execute(
            "INSERT INTO memory_edge(a_seq, b_seq, rel, w, built_at) VALUES(?,?,?,?,?)",
            (seqs[1], seqs[0], "related", 0.5, "2026-01-01T00:00:00"),
        )
        result = {r["id"]: r["ok"] for r in invariants.check_all(self.app.db)}
        self.assertFalse(result["I18"])


class TestSpread(unittest.TestCase):
    def test_ppr_reaches_two_hops(self) -> None:
        adj = {1: [(2, 1.0)], 2: [(1, 1.0), (3, 1.0)], 3: [(2, 1.0)]}
        mass = graph.spread(adj, [(1, 1.0)], alpha=0.5, iterations=3)
        self.assertAlmostEqual(mass[1], 1.0, places=6)
        self.assertGreater(mass[3], 0.05, "两跳之外的节点也应该拿到质量")
        self.assertLess(mass[3], mass[2])

    def test_spread_without_edges_is_empty(self) -> None:
        self.assertEqual(graph.spread({}, [(1, 1.0)]), {})
        self.assertEqual(graph.spread({1: []}, [(1, 1.0)]), {1: 1.0})

    def test_hop_distance(self) -> None:
        adj = {1: [(2, 1.0)], 2: [(1, 1.0), (3, 1.0)]}
        self.assertEqual(graph.hop_distance(adj, [1], 2), 1)
        self.assertEqual(graph.hop_distance(adj, [1], 3), 2)
        self.assertIsNone(graph.hop_distance(adj, [1], 9))


class TestCoverageTerms(unittest.TestCase):
    def test_content_terms_keep_compounds(self) -> None:
        terms = query.content_terms("我早上不挤那趟车之后改成了什么")
        self.assertIn("早上", terms, "单字虚词不能把「早上」拆成「早」")
        self.assertNotIn("早", terms)

    def test_global_weights_skip_absent_terms(self) -> None:
        terms = ["早上", "不挤", "趟车"]
        target = query.bigrams("用户早上挤地铁上班，到公司一身汗")
        # 「不挤/趟车」库里没有（df=0），不该进分母
        weights = {"早上": 5.0}
        cov = query.coverage_idf_sets(terms, [target], target, weights)
        self.assertAlmostEqual(cov, 1.0, places=6)

    def test_candidate_set_idf_still_works(self) -> None:
        terms = ["养鱼", "插件"]
        target = query.bigrams("B 站养鱼插件 0.4.6")
        cov = query.coverage_idf_sets(terms, [target], target)
        self.assertAlmostEqual(cov, 1.0, places=6)


class TestGraphChannel(unittest.TestCase):
    """端到端：图通道只补候选，词法强覆盖时不插手。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self, app: PMem) -> None:
        store = app.store
        for text in (
            "用户周末骑自行车去修车摊，师傅说后闸松了要换线管",
            "用户第一次修自行车花十五块换线管，修车摊就在楼下",
            "用户今天在公司评审，把需求改了三版",
            "用户晚上在家煮面，加了个鸡蛋",
            "用户养的猫叫年糕，半夜踩用户脸叫起床",
            "用户最近在追《夏日重现》，一口气看了六集",
            "用户把工资的一半存进了定期，剩下当生活费",
            "用户周末跟朋友打球，输了三百块",
        ):
            store.add(text, kind="episodic", provenance="user_explicit")

    def test_append_helper_picks_unshown_neighbour(self) -> None:
        app = PMem.open(self.db_path, config=Config())
        try:
            self._seed(app)
            conn = app.db.conn
            seqs = [int(r["seq"]) for r in conn.execute("SELECT seq FROM memory ORDER BY seq")]
            # 前两条是自行车相关；mass 里给「第二条」高分（模拟扩散漂过去）
            items, info = app.store._graph_append(
                mass={seqs[1]: 0.9},
                picked=[(seqs[0], 0.5), (seqs[2], 0.4)],
                graph_cfg={"min_share": 0.30, "max_items": 2},
            )
            self.assertEqual(info["added"], 1)
            self.assertEqual(items[0]["source"], "graph")
            self.assertIn("修车摊", items[0]["text"])
            # 已经在结果里出现过的不会再补一遍
            items2, info2 = app.store._graph_append(
                mass={seqs[1]: 0.9},
                picked=[(seqs[0], 0.5), (seqs[1], 0.4)],
                graph_cfg={"min_share": 0.30, "max_items": 2},
            )
            self.assertEqual(info2["added"], 0)
            self.assertEqual(items2, [])
        finally:
            app.close()

    def test_disabled_graph_never_contributes(self) -> None:
        app = PMem.open(self.db_path, config=Config({"retrieval": {"graph": {"enabled": False}}}))
        try:
            self._seed(app)
            result = app.store.search("自行车送去修到底是哪个零件坏了", k=3, record=False)
            self.assertFalse(any(item.get("source") == "graph" for item in result["items"]))
            self.assertEqual(result["spread"], {})
        finally:
            app.close()

    def test_strong_coverage_skips_graph(self) -> None:
        cfg = Config({"retrieval": {"graph": {"enabled": True, "trigger_coverage": 0.0}}})
        app = PMem.open(self.db_path, config=cfg)
        try:
            self._seed(app)
            result = app.store.search("用户养的猫叫什么", k=3, record=False)
            self.assertEqual(result["spread"].get("skipped"), "coverage")
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
