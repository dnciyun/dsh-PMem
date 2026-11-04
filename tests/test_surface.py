"""M3.6：按等级淡忘 + 低成本浮现（session_start / turn）。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from pmem.base.clock import now, to_iso
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.memory import groups, lifecycle, surface
from pmem.net import entities

from .support import InProcessMcp, drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("surface")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _backdate(self, memory_id: str, days: int) -> None:
        stamp = to_iso(now() - timedelta(days=days))
        self.db.conn.execute(
            "UPDATE memory SET last_used_at=? WHERE id=?", (stamp, memory_id)
        )

    def _add(self, text: str, grade: str, path: str = "我/其他") -> str:
        result = self.store.add(text, groups=[path], grade=grade)
        return result["id"]


class TestGradeLifecycle(Case):
    def test_grade_changes_dormant_speed(self) -> None:
        s = self._add("我叫小安", "S", "我/姓名")
        a = self._add("我长期偏好先做规划", "A")
        b = self._add("PMem 用 SQLite", "B")
        c = self._add("上周去了医院", "C")
        d = self._add("今天天气不错", "D")
        self._backdate(s, 1000)
        self._backdate(a, 100)
        self._backdate(b, 100)
        self._backdate(c, 40)
        self._backdate(d, 10)
        result = lifecycle.tick(self.db)
        status = {
            row["id"]: row["status"]
            for row in self.db.conn.execute("SELECT id, status FROM memory")
        }
        self.assertEqual(status[s], "active")  # S 永不自动下沉
        self.assertEqual(status[a], "active")  # A 180 天
        self.assertEqual(status[b], "dormant")  # B 90 天
        self.assertEqual(status[c], "dormant")  # C 30 天
        self.assertEqual(status[d], "dormant")  # D 7 天
        self.assertEqual(result["by_dormant_grade"]["B"], 1)

    def test_relationship_group_never_fades(self) -> None:
        shared = self._add("我们第一次一起熬夜改这个项目", "D", "我们/经历")
        self._backdate(shared, 500)
        lifecycle.tick(self.db)
        row = self.db.conn.execute("SELECT status FROM memory WHERE id=?", (shared,)).fetchone()
        self.assertEqual(row["status"], "active")

    def test_dry_run_writes_nothing(self) -> None:
        d = self._add("琐碎", "D")
        self._backdate(d, 10)
        result = lifecycle.tick(self.db, dry_run=True)
        self.assertGreaterEqual(result["moved"], 1)
        row = self.db.conn.execute("SELECT status FROM memory WHERE id=?", (d,)).fetchone()
        self.assertEqual(row["status"], "active")


class TestSessionCard(Case):
    def setUp(self) -> None:
        super().setUp()
        self.memory = self.store.add(
            "你名字叫小安，英文叫An", groups=["我/姓名"], grade="S"
        )
        self.entity = entities.resolve(
            self.db, subject="self", name="小安", type_hint="person", confidence=1.0
        )["entity"]
        groups.assign(
            self.db,
            target_tbl="entity",
            target_id=self.entity["id"],
            path="我/姓名",
            grade="S",
            rationale="身份锚点",
        )

    def test_session_start_has_identity_and_budget(self) -> None:
        result = self.app.surface(session_id="s1")
        self.assertTrue(result["ok"])
        data = result["data"]
        kinds = {(item["tbl"], item.get("name") or item["id"]) for item in data["items"]}
        self.assertIn(("entity", "小安"), kinds)
        self.assertIn(("memory", self.memory["id"]), kinds)
        self.assertLessEqual(data["tokens"], 220)
        self.assertFalse(data["repeat"])
        text = PMem.render_surface(data)
        self.assertIn("<<<PMEM_SURFACE", text)
        self.assertIn("小安", text)

    def test_same_session_does_not_repeat(self) -> None:
        first = self.app.surface(session_id="s1")
        self.assertTrue(first["data"]["items"])
        second = self.app.surface(session_id="s1")
        self.assertEqual(second["data"]["items"], [])
        self.assertTrue(second["data"]["repeat"])
        third = self.app.surface(session_id="s2")
        self.assertTrue(third["data"]["items"])

    def test_surfacing_is_not_use(self) -> None:
        """浮现 ≠ 使用（M9.8b）。

        以前浮现会连 use_count 一起 +1，于是「多久没用」等于「多久没被自动注入」——
        插件每轮都浮现一次，每条记忆就天天"被用"，生命周期永远不动。
        现在浮现只推进 last_surfaced_at，细节见 tests/test_usage_semantics.py。
        """
        before = self.store.get(self.memory["id"])
        self.app.surface(session_id="s1")
        after = self.store.get(self.memory["id"])
        self.assertEqual(after["use_count"], before["use_count"], "浮现不许增加 use_count")
        self.assertEqual(after["last_used_at"], before["last_used_at"], "浮现不许推进 last_used_at")
        self.assertIsNotNone(after["last_surfaced_at"], "浮现要推进 last_surfaced_at")

    def test_budget_limits_items(self) -> None:
        result = self.app.surface(session_id="s1", budget=40)
        self.assertLessEqual(result["data"]["tokens"], 40)
        self.assertTrue(result["data"]["items"])

    def test_dormant_memory_not_surfaced(self) -> None:
        self.db.conn.execute("UPDATE memory SET status='dormant' WHERE id=?", (self.memory["id"],))
        result = self.app.surface(session_id="s3")
        ids = [item["id"] for item in result["data"]["items"] if item["tbl"] == "memory"]
        self.assertNotIn(self.memory["id"], ids)

    def test_session_start_after_turn_still_cards(self) -> None:
        turn = self.app.surface(session_id="s6", cue="小安", mode="turn")
        self.assertTrue(turn["data"]["items"])
        card = self.app.surface(session_id="s6")
        self.assertFalse(card["data"]["repeat"])
        self.assertTrue(card["data"]["items"])

    def test_relationship_memory_comes_first(self) -> None:
        shared = self.store.add(
            "你写这套记忆，是为了上下文清空后我们还能继续相处",
            groups=["我们/初衷"],
            grade="S",
            grade_reason="这是这套系统存在的理由",
        )
        result = self.app.surface(session_id="s7")
        memory_items = [i for i in result["data"]["items"] if i["tbl"] == "memory"]
        self.assertEqual(memory_items[0]["id"], shared["id"])
        self.assertEqual(memory_items[0]["reason"], "relationship")
        text = PMem.render_surface(result["data"])
        self.assertIn("我们", text)

    def test_due_reminder_for_b_grade(self) -> None:
        due = self._add("阶段性项目细节", "B", "工作/项目")
        self._backdate(due, 60)
        result = self.app.surface(session_id="s4")
        reasons = {item["id"]: item["reason"] for item in result["data"]["items"]}
        self.assertEqual(reasons.get(due), "due")


class TestTurnCue(Case):
    def setUp(self) -> None:
        super().setUp()
        self.hangzhou = self.store.add("我住在杭州西湖边", groups=["我/基本信息"], grade="A")
        self.beijing = self.store.add("我去过北京出差", groups=["我/行程"], grade="C")

    def test_cue_hit_returns_one(self) -> None:
        result = self.app.surface(session_id="s1", cue="杭州", mode="turn")
        self.assertEqual(len(result["data"]["items"]), 1)
        self.assertEqual(result["data"]["items"][0]["id"], self.hangzhou["id"])
        self.assertEqual(result["data"]["items"][0]["reason"], "cue")
        self.assertLessEqual(result["data"]["tokens"], 80)

    def test_cue_miss_returns_empty(self) -> None:
        result = self.app.surface(session_id="s1", cue="量子计算", mode="turn")
        self.assertEqual(result["data"]["items"], [])
        self.assertEqual(result["data"]["tokens"], 0)

    def test_cue_not_repeated_in_session(self) -> None:
        first = self.app.surface(session_id="s1", cue="杭州", mode="turn")
        self.assertTrue(first["data"]["items"])
        second = self.app.surface(session_id="s1", cue="杭州", mode="turn")
        self.assertEqual(second["data"]["items"], [])

    def test_turn_without_cue_is_empty(self) -> None:
        result = self.app.surface(session_id="s1", mode="turn")
        self.assertEqual(result["data"]["items"], [])

    def test_cue_prefers_active_over_dormant(self) -> None:
        self.db.conn.execute("UPDATE memory SET status='dormant' WHERE id=?", (self.hangzhou["id"],))
        result = self.app.surface(session_id="s5", cue="杭州", mode="turn")
        self.assertEqual(result["data"]["items"], [])


class TestApi(Case):
    def test_mcp_surface(self) -> None:
        client = InProcessMcp(self.db_path)
        try:
            added = client.tool(
                "memory_add",
                {"text": "我叫小安", "groups": ["我/姓名"], "grade": "S"},
            )
            self.assertTrue(added["ok"], added)
            result = client.tool("memory_surface", {"session_id": "mcp-1"})
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["data"]["items"])
            repeat = client.tool("memory_surface", {"session_id": "mcp-1"})
            self.assertEqual(repeat["data"]["items"], [])
        finally:
            client.close()

    def test_cli_surface_and_lifecycle(self) -> None:
        run_cli(
            ["--db", str(self.db_path), "add", "我叫小安", "--group", "我/姓名", "--grade", "S"]
        )
        code, out = run_cli(
            ["--db", str(self.db_path), "surface", "--session", "cli-1"]
        )
        self.assertEqual(code, 0)
        self.assertIn("小安", out)
        code, out = run_cli(
            ["--db", str(self.db_path), "--json", "lifecycle", "tick", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_render_empty(self) -> None:
        self.assertEqual(surface.render({"items": []}), "")


class TestConfig(Case):
    def test_capabilities_mentions_schema(self) -> None:
        from pmem import SCHEMA_VERSION

        caps = self.app.capabilities()["data"]
        self.assertEqual(caps["schema_version"], SCHEMA_VERSION)
