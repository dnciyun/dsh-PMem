"""醒来卡：只读、四段、预算内。"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.memory import wake

from .support import drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class TestWake(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("wake")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _seed(self) -> dict[str, str]:
        return {
            "name": self.store.add("我叫小安，英文叫 An", groups=["我/姓名"], grade="S")["id"],
            "why": self.store.add(
                "写 PMem 是为了上下文清空后我们还能继续相处",
                groups=["我们/初衷"],
                grade="S",
            )["id"],
            "now": self.store.add("现在在把记忆接进 DSH", groups=["我们/现在"], grade="A")["id"],
            "next": self.store.add("下一步写插件并重启验证", groups=["我们/下次"], grade="A")["id"],
        }

    def test_four_sections_in_order(self) -> None:
        ids = self._seed()
        data = wake.build(self.db, subject="self")
        labels = [section["label"] for section in data["sections"]]
        self.assertEqual(labels, ["我是谁", "我们", "现在", "下次"])
        self.assertEqual([s["id"] for s in data["sections"]], [ids["name"], ids["why"], ids["now"], ids["next"]])
        self.assertIn("<<<PMEM_WAKE", data["text"])
        self.assertIn("我们还能继续相处", data["text"])

    def test_handoff_section_between_now_and_next(self) -> None:
        self._seed()
        handoff = self.store.add(
            "上次停在哪：正在重启 DSH 验证醒来卡。",
            groups=["我们/停在哪"],
            grade="B",
        )["id"]
        data = wake.build(self.db, subject="self")
        labels = [section["label"] for section in data["sections"]]
        self.assertEqual(labels, ["我是谁", "我们", "现在", "上次", "下次"])
        self.assertEqual(data["sections"][3]["id"], handoff)
        self.assertIn("上次：正在重启", data["text"])
        self.assertNotIn("上次：上次停在哪", data["text"])

    def test_important_memory_without_group_still_in_card(self) -> None:
        """「用户叫林野」被丢进任意分组时，也得进卡——重要的东西不该靠分组起名。"""
        self.app.store.add("用户叫林野，希望被称呼为「林野」", grade="A")
        self.app.store.add("用户最近在准备面试", grade="A")
        card = self.app.wake()["data"]
        self.assertIn("林野", card["text"])
        self.assertLessEqual(card["tokens"], card["budget"])

    def test_constant_ids_are_listed(self) -> None:
        self.app.store.add("用户叫林野，希望被称呼为「林野」", grade="A")
        card = self.app.wake()["data"]
        self.assertTrue(card["constant_ids"], "卡里有的记忆必须回传 id，检索才知道哪些不用再找")

    def test_read_only(self) -> None:
        ids = self._seed()
        before = self.store.get(ids["why"])["use_count"]
        rows_before = self.db.conn.execute("SELECT COUNT(*) AS n FROM surface_log").fetchone()["n"]
        wake.build(self.db, subject="self")
        after = self.store.get(ids["why"])["use_count"]
        rows_after = self.db.conn.execute("SELECT COUNT(*) AS n FROM surface_log").fetchone()["n"]
        self.assertEqual(after, before)
        self.assertEqual(rows_after, rows_before)
        self.assertEqual(rows_after, 0)

    def test_budget_keeps_first_section(self) -> None:
        self._seed()
        data = wake.build(self.db, subject="self", budget=80)
        self.assertLessEqual(data["tokens"], 80)
        self.assertTrue(data["sections"])
        self.assertEqual(data["sections"][0]["label"], "我是谁")
        self.assertNotIn("下次", [s["label"] for s in data["sections"]])

    def test_cli_prints_wake_card(self) -> None:
        self._seed()
        code, out = run_cli(["--db", str(self.db_path), "wake"])
        self.assertEqual(code, 0)
        self.assertIn("<<<PMEM_WAKE", out)
        self.assertIn("我是谁", out)

    def test_empty_db_is_empty_card(self) -> None:
        data = wake.build(self.db, subject="self")
        self.assertEqual(data["sections"], [])
        self.assertEqual(data["text"], "")


if __name__ == "__main__":
    unittest.main()
