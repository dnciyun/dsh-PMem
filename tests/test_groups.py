"""M3.5：AI 自建分组与 S/A/B/C/D 分级。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.base.errors import PmemError
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.memory import groups
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
        self._dir = temp_dir("group")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)


class TestPaths(Case):
    def test_ensure_is_idempotent_and_creates_hierarchy(self) -> None:
        leaf = groups.ensure(self.db, "self", "我/姓名")
        again = groups.ensure(self.db, "self", "我/姓名")
        self.assertEqual(leaf["id"], again["id"])
        rows = groups.tree(self.db, "self")
        self.assertEqual([r["path"] for r in rows], ["我", "我/姓名"])
        self.assertEqual([r["depth"] for r in rows], [1, 2])

    def test_normalize_rejects_bad_paths(self) -> None:
        for bad in ("", "  ", "a/../b", "/".join("x" for _ in range(5))):
            with self.assertRaises(PmemError):
                groups.normalize_path(bad)

    def test_normalize_folds_fullwidth_and_spaces(self) -> None:
        self.assertEqual(groups.normalize_path(" 我 ／ 姓名 "), "我/姓名")

    def test_rename_updates_descendants(self) -> None:
        groups.ensure(self.db, "self", "我/姓名")
        groups.ensure(self.db, "self", "我/姓名/别名")
        result = groups.rename(self.db, "我/姓名", "称呼")
        self.assertEqual(result["path"], "我/称呼")
        paths = {r["path"] for r in groups.tree(self.db, "self")}
        self.assertIn("我/称呼/别名", paths)
        self.assertNotIn("我/姓名/别名", paths)

    def test_merge_moves_members_and_children(self) -> None:
        mem = self.store.add("项目笔记一")
        groups.assign(self.db, target_tbl="memory", target_id=mem["id"], path="工作/项目")
        groups.ensure(self.db, "self", "工作/项目/PMem")
        result = groups.merge(self.db, "工作/项目", "工作")
        self.assertEqual(result["members"], 1)
        self.assertEqual(result["children"], 1)
        self.assertEqual(groups.groups_for(self.db, "memory", mem["id"]), ["工作"])
        paths = {r["path"] for r in groups.tree(self.db, "self")}
        self.assertIn("工作/PMem", paths)

    def test_gc_archives_empty_groups(self) -> None:
        groups.ensure(self.db, "self", "空/组")
        result = groups.gc(self.db)
        self.assertEqual(result["archived"], 2)
        self.assertEqual(groups.tree(self.db, "self"), [])


class TestAssign(Case):
    def test_memory_assignment_and_grade(self) -> None:
        mem = self.store.add("我住在杭州")
        result = groups.assign(
            self.db,
            target_tbl="memory",
            target_id=mem["id"],
            path="我/基本信息",
            grade="A",
            rationale="长期住址",
        )
        self.assertEqual(result["groups"], ["我/基本信息"])
        self.assertEqual(result["grade"], "A")
        item = self.store.get(mem["id"])
        self.assertEqual(item["groups"], ["我/基本信息"])
        self.assertEqual(item["grade"], "A")

    def test_add_accepts_groups_and_grade(self) -> None:
        result = self.store.add(
            "我叫小安", groups=["我/姓名"], grade="S", grade_reason="身份锚点"
        )
        self.assertEqual(result["groups"], ["我/姓名"])
        self.assertEqual(result["grade"], "S")
        detail = groups.grade_detail(self.db, "memory", result["id"])
        self.assertEqual(detail["grade"], "S")
        self.assertEqual(detail["rationale"], "身份锚点")
        self.assertEqual(detail["assigned_by"], "ai")

    def test_grade_change_keeps_one_row_and_history(self) -> None:
        mem = self.store.add("今天天气不错")
        groups.assign(self.db, target_tbl="memory", target_id=mem["id"], grade="C")
        groups.assign(
            self.db, target_tbl="memory", target_id=mem["id"], grade="B", rationale="后来发现有用"
        )
        self.assertEqual(groups.grade_of(self.db, "memory", mem["id"]), "B")
        count = self.db.conn.execute(
            "SELECT COUNT(*) c FROM target_grade WHERE target_tbl='memory' AND target_id=?",
            (mem["id"],),
        ).fetchone()["c"]
        self.assertEqual(count, 1)
        ops = [
            r["op"]
            for r in self.db.conn.execute(
                "SELECT op FROM event_log WHERE target_id=? ORDER BY seq", (mem["id"],)
            )
        ]
        self.assertEqual(ops.count("group.grade"), 2)

    def test_entity_assignment_like_my_name(self) -> None:
        created = entities.resolve(
            self.db, subject="self", name="小安", type_hint="person", confidence=1.0
        )["entity"]
        groups.assign(
            self.db,
            target_tbl="entity",
            target_id=created["id"],
            path="我/姓名",
            grade="S",
            rationale="AI 自己的名字：身份锚点",
        )
        self.assertEqual(groups.grade_of(self.db, "entity", created["id"]), "S")
        targets = groups.targets(self.db, "self", "我/姓名")
        self.assertEqual(targets[0]["name"], "小安")
        self.assertEqual(targets[0]["grade"], "S")

    def test_bad_grade_rejected(self) -> None:
        mem = self.store.add("随便")
        with self.assertRaises(PmemError):
            groups.assign(self.db, target_tbl="memory", target_id=mem["id"], grade="X")

    def test_assign_missing_target_is_not_found(self) -> None:
        with self.assertRaises(PmemError):
            groups.assign(self.db, target_tbl="memory", target_id="01JCV0000000000000000000000", path="我/姓名")


class TestSearchFilter(Case):
    def setUp(self) -> None:
        super().setUp()
        self.hangzhou = self.store.add(
            "我住在杭州西湖边", groups=["我/基本信息"], grade="A"
        )
        self.shanghai = self.store.add("我住在上海浦东", groups=["我/行程"], grade="C")

    def test_group_filter_includes_descendants(self) -> None:
        result = self.store.search("杭州", filters={"group": "我"})
        ids = [item["id"] for item in result["items"]]
        self.assertIn(self.hangzhou["id"], ids)
        self.assertNotIn(self.shanghai["id"], ids)
        first = result["items"][0]
        self.assertEqual(first["groups"], ["我/基本信息"])
        self.assertEqual(first["grade"], "A")

    def test_group_filter_leaf(self) -> None:
        result = self.store.search("我", filters={"group": "我/基本信息"})
        ids = [item["id"] for item in result["items"]]
        self.assertIn(self.hangzhou["id"], ids)
        self.assertNotIn(self.shanghai["id"], ids)

    def test_grade_filter(self) -> None:
        result = self.store.search("我", filters={"grade": ["A"]})
        ids = [item["id"] for item in result["items"]]
        self.assertEqual(ids, [self.hangzhou["id"]])


class TestApi(Case):
    def test_mcp_group_assign_and_tree(self) -> None:
        client = InProcessMcp(self.db_path)
        try:
            added = client.tool("memory_add", {"text": "我叫小安"})
            mem_id = added["data"]["id"]
            assigned = client.tool(
                "memory_group",
                {
                    "action": "assign",
                    "target_tbl": "memory",
                    "target_id": mem_id,
                    "path": "我/姓名",
                    "grade": "S",
                    "rationale": "身份锚点",
                },
            )
            self.assertTrue(assigned["ok"], assigned)
            tree = client.tool("memory_group", {"action": "tree"})
            self.assertEqual([i["path"] for i in tree["data"]["items"]], ["我", "我/姓名"])
            targets = client.tool(
                "memory_group", {"action": "targets", "path": "我/姓名"}
            )
            self.assertEqual(targets["data"]["items"][0]["grade"], "S")
        finally:
            client.close()

    def test_cli_group_tree_and_assign(self) -> None:
        code, out = run_cli(["--db", str(self.db_path), "add", "我叫小安", "--group", "我/姓名", "--grade", "S"])
        self.assertEqual(code, 0)
        code, out = run_cli(["--db", str(self.db_path), "--json", "group", "tree"])
        self.assertEqual(code, 0)
        paths = [i["path"] for i in json.loads(out)["data"]["items"]]
        self.assertEqual(paths, ["我", "我/姓名"])
        code, out = run_cli(["--db", str(self.db_path), "--json", "group", "stats"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["grades"], {"S": 1})

    def test_cli_search_filters_by_group(self) -> None:
        run_cli(["--db", str(self.db_path), "add", "我叫小安", "--group", "我/姓名", "--grade", "S"])
        code, out = run_cli(
            ["--db", str(self.db_path), "--json", "search", "小安", "--group", "我"]
        )
        self.assertEqual(code, 0)
        items = json.loads(out)["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["grade"], "S")

    def test_hard_delete_clears_membership_and_grade(self) -> None:
        mem = self.store.add("临时记录", groups=["我/临时"], grade="B")
        self.store.delete(mem["id"], hard=True)
        self.assertEqual(groups.groups_for(self.db, "memory", mem["id"]), [])
        self.assertIsNone(groups.grade_of(self.db, "memory", mem["id"]))

    def test_entity_merge_moves_group_and_grade(self) -> None:
        keep = entities.resolve(
            self.db, subject="self", name="李雷", type_hint="person", confidence=1.0
        )["entity"]
        drop = entities.resolve(
            self.db, subject="self", name="李雷雷", type_hint="person", confidence=1.0
        )["entity"]
        groups.assign(self.db, target_tbl="entity", target_id=keep["id"], grade="B")
        groups.assign(
            self.db,
            target_tbl="entity",
            target_id=drop["id"],
            path="我/姓名",
            grade="S",
            rationale="别名更强",
        )
        entities.merge(self.db, keep["id"], drop["id"])
        self.assertEqual(groups.groups_for(self.db, "entity", keep["id"]), ["我/姓名"])
        self.assertEqual(groups.grade_of(self.db, "entity", keep["id"]), "S")

    def test_invariant_detects_bad_group_path(self) -> None:
        groups.ensure(self.db, "self", "我/姓名")
        self.db.conn.execute("UPDATE grp SET path='我/错/路径' WHERE path='我/姓名'")
        checks = {r["id"]: r for r in invariants.check_all(self.db)}
        self.assertFalse(checks["I17"]["ok"])
