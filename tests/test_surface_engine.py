"""M9.65：三路触发（cue / context / rhythm）+ 浮现回执。"""

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

from .support import InProcessMcp, drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("engine")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _add(self, text: str, grade: str, path: str = "我/其他", days: int = 0) -> str:
        created = to_iso(now() - timedelta(days=days)) if days else None
        return self.store.add(text, groups=[path], grade=grade, created_at=created)["id"]


class TestContextTrigger(Case):
    def test_files_in_context_surface_memory(self) -> None:
        target = self._add("改 surface.py 的时候记得同步 CLI 的 --context", "B", "工作/PMem")
        result = self.app.surface(
            session_id="c1",
            mode="turn",
            context={"files": ["src/pmem/memory/surface.py"], "cwd": "F:/Ai/AWB"},
        )
        self.assertTrue(result["ok"], result)
        items = result["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], target)
        self.assertEqual(items[0]["reason"], "context")

    def test_context_miss_returns_empty(self) -> None:
        self._add("我住在杭州西湖边", "A", "我/基本信息")
        result = self.app.surface(
            session_id="c2", mode="turn", context={"files": ["docs/quantum.md"]}
        )
        self.assertEqual(result["data"]["items"], [])

    def test_cue_wins_over_context(self) -> None:
        self._add("杭州的房子", "A", "我/基本信息")
        self._add("改 surface.py 的时候同步 CLI", "B", "工作/PMem")
        result = self.app.surface(
            session_id="c3",
            mode="turn",
            cue="杭州",
            context={"files": ["src/pmem/memory/surface.py"]},
        )
        items = result["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reason"], "cue")

    def test_turn_items_carry_probability(self) -> None:
        self._add("我住在杭州西湖边", "A", "我/基本信息")
        result = self.app.surface(session_id="c4", cue="杭州", mode="turn")
        item = result["data"]["items"][0]
        self.assertIn("p", item)
        self.assertGreaterEqual(item["p"], 0.25)
        self.assertLessEqual(item["p"], 1.0)


class TestRhythmTrigger(Case):
    def test_faded_memory_surfaces_without_cue(self) -> None:
        old = self._add("旧的部署约定：先跑 doctor 再上", "A", "工作/PMem", days=120)
        result = self.app.surface(session_id="r1", mode="turn")
        items = result["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], old)
        self.assertEqual(items[0]["reason"], "rhythm")

    def test_fresh_memory_does_not_nag(self) -> None:
        self._add("刚写下的偏好：先做规划", "A", "我/其他")
        result = self.app.surface(session_id="r2", mode="turn")
        self.assertEqual(result["data"]["items"], [])

    def test_more_faded_comes_first(self) -> None:
        self._add("六十天前的项目细节", "A", "工作/PMem", days=60)
        ancient = self._add("两百天前的项目细节", "A", "工作/PMem", days=200)
        result = self.app.surface(session_id="r3", mode="turn")
        self.assertEqual(result["data"]["items"][0]["id"], ancient)

    def test_d_grade_never_nags(self) -> None:
        self._add("随手记的天气", "D", "我/其他", days=200)
        result = self.app.surface(session_id="r4", mode="turn")
        self.assertEqual(result["data"]["items"], [])

    def test_rhythm_is_capped_per_session(self) -> None:
        for index in range(6):
            self._add(f"旧细节 {index}", "A", "工作/PMem", days=60 + index * 30)
        seen = 0
        for _turn in range(6):
            result = self.app.surface(session_id="r5", mode="turn")
            if result["data"]["items"]:
                seen += 1
        self.assertEqual(seen, 3)


class TestReceipts(Case):
    def test_used_receipt_counts(self) -> None:
        memory = self._add("我住在杭州西湖边", "A", "我/基本信息")
        self.app.surface(session_id="u1", cue="杭州", mode="turn")
        receipt = self.app.surface_used(session_id="u1", memory_id=memory)
        self.assertTrue(receipt["ok"], receipt)
        self.assertTrue(receipt["data"]["surfaced"])
        stats = self.app.surface_receipts(session_id="u1")["data"]
        self.assertEqual(stats["surfaced"], 1)
        self.assertEqual(stats["used"], 1)
        self.assertEqual(stats["hit_rate"], 1.0)

    def test_duplicate_receipt_is_idempotent(self) -> None:
        memory = self._add("我住在杭州西湖边", "A", "我/基本信息")
        self.app.surface(session_id="u2", cue="杭州", mode="turn")
        self.app.surface_used(session_id="u2", memory_id=memory)
        again = self.app.surface_used(session_id="u2", memory_id=memory)
        self.assertTrue(again["data"]["duplicate"])
        self.assertEqual(self.app.surface_receipts(session_id="u2")["data"]["used"], 1)

    def test_unused_is_visible(self) -> None:
        self._add("我住在杭州西湖边", "A", "我/基本信息")
        self.app.surface(session_id="u3", cue="杭州", mode="turn")
        stats = self.app.surface_receipts(session_id="u3")["data"]
        self.assertEqual(stats["surfaced"], 1)
        self.assertEqual(stats["used"], 0)
        self.assertEqual(stats["unused"], 1)
        self.assertEqual(stats["hit_rate"], 0.0)

    def test_unknown_memory_is_rejected(self) -> None:
        result = self.app.surface_used(session_id="u4", memory_id="mem_nope")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_NOT_FOUND")

    def test_unsurfaced_memory_still_records(self) -> None:
        memory = self._add("没浮现过但确实用到了", "B", "工作/PMem")
        result = self.app.surface_used(session_id="u5", memory_id=memory, signal="recall")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["data"]["surfaced"])

    def test_receipt_revives_dormant(self) -> None:
        memory = self._add("旧的部署约定：先跑 doctor 再上", "A", "工作/PMem", days=60)
        self.db.conn.execute("UPDATE memory SET status='dormant' WHERE id=?", (memory,))
        self.app.surface(session_id="u6", mode="turn")
        result = self.app.surface_used(session_id="u6", memory_id=memory)
        self.assertTrue(result["data"]["promoted"])
        row = self.db.conn.execute("SELECT status FROM memory WHERE id=?", (memory,)).fetchone()
        self.assertEqual(row["status"], "active")


class TestCliAndMcp(Case):
    def test_cli_context_json(self) -> None:
        self._add("改 surface.py 的时候同步 CLI 的 --context", "B", "工作/PMem")
        code, out = run_cli(
            [
                "--db",
                str(self.db_path),
                "surface",
                "--session",
                "cli-c",
                "--mode",
                "turn",
                "--context",
                json.dumps({"files": ["src/pmem/memory/surface.py"]}),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("情境", out)

    def test_cli_bad_context_json(self) -> None:
        code, _out = run_cli(
            ["--db", str(self.db_path), "surface", "--session", "cli-b", "--context", "{oops"]
        )
        self.assertEqual(code, 2)

    def test_cli_used_and_receipts(self) -> None:
        memory = self._add("我住在杭州西湖边", "A", "我/基本信息")
        run_cli(
            ["--db", str(self.db_path), "surface", "--session", "cli-u", "--cue", "杭州", "--mode", "turn"]
        )
        code, out = run_cli(
            ["--db", str(self.db_path), "--json", "surface", "--session", "cli-u", "--used", memory]
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["data"]["surfaced"])
        code, out = run_cli(
            ["--db", str(self.db_path), "--json", "surface", "--session", "cli-u", "--receipts"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["used"], 1)

    def test_mcp_accepts_context(self) -> None:
        self._add("改 surface.py 的时候同步 CLI 的 --context", "B", "工作/PMem")
        client = InProcessMcp(self.db_path)
        try:
            result = client.tool(
                "memory_surface",
                {
                    "session_id": "mcp-c",
                    "mode": "turn",
                    "context": {"files": ["src/pmem/memory/surface.py"]},
                },
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["items"][0]["reason"], "context")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
