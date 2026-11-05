"""鲸眠·宿主模式（0.35.2）：DSH 里的会话用 MCP 工具做夜间整理，不需要 API key。

和 CLI 模式（pmem sleep whale）的区别：
- CLI 模式：PMem 进程自己调模型 → 要凭证；
- 宿主模式：模型由宿主（DSH）提供 → PMem 只给 prep/finish 两个工具，一个 token 都不花在凭证上。

覆盖：prep 材料、finish 落库+转录+8 阶段、编造被打回、无原文、MCP 分发。
"""

from __future__ import annotations

import io
import json
import unittest

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.gate.mcp_server import McpServer
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.memory.store import MemoryStore

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})
DAY = "2026-05-03"


class WhaleHostCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("whale-host")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")
        self.cfg = Config({
            "retrieval": {"vector": {"enabled": False}},
            "net": {"auto_extract": False},
            "sleep": {"whale": {"transcript_dir": str(self.dir / "whale-sleep")}},
        })
        self.app = PMem(self.db, config=self.cfg)

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    def _ingest(self, texts: list[str]) -> None:
        for index, text in enumerate(texts):
            journal.append(
                self.db, text, subject="self", session_id="s-1",
                role="user", ts=f"{DAY}T1{index}:00:00Z",
            )

    def _count(self, table: str) -> int:
        return self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_prep_returns_materials_without_key(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞"])
        result = self.app.whale_prep(day_key=DAY)
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["session"], "鲸眠")
        self.assertEqual(data["pending"], 2)
        self.assertIn("你：今晚要加班到十点", data["materials"])
        self.assertIn("鲸眠", data["rules"])
        self.assertEqual(set(data["output"]), {"title", "lines", "loops"})

    def test_prep_without_lines_is_honest(self) -> None:
        result = self.app.whale_prep(day_key=DAY)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["pending"], 0)
        self.assertIn("没有留下原文", result["data"]["materials"])

    def test_finish_writes_diary_transcript_and_sleep(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞", "我先去开会了"])
        result = self.app.whale_finish(
            day_key=DAY,
            title="加班到十点",
            lines=["你今晚要加班到十点", "你明天记得带伞"],
            loops=["明天带伞"],
            session_id="dsh-session-123",
        )
        self.assertTrue(result["ok"])
        diary = result["data"]["diary"]
        self.assertTrue(diary["episode_id"])
        self.assertEqual(diary["mode"], "model")
        self.assertTrue(diary["facts"])
        self.assertEqual(diary["sleep"]["status"], "ok")
        self.assertEqual(self._count("episode"), 1)
        self.assertEqual(self._count("sleep_run"), 1)

        transcript = self.dir / "whale-sleep" / f"{DAY}.jsonl"
        self.assertTrue(transcript.exists())
        text = transcript.read_text(encoding="utf-8")
        self.assertIn("dsh-session-123", text)
        self.assertIn('"source": "dsh_host"', text)

    def test_finish_invented_name_falls_back(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞"])
        result = self.app.whale_finish(
            day_key=DAY,
            title="看书",
            lines=["你读完了《海边的卡夫卡》"],
        )
        self.assertTrue(result["ok"])
        diary = result["data"]["diary"]
        self.assertEqual(diary["mode"], "template")
        self.assertTrue(diary["problems"])

    def test_mcp_dispatch_exposes_host_tools(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞"])
        server = McpServer(self.app, out=io.StringIO())
        prep = server.dispatch("memory_whale_prep", {"day_key": DAY})
        self.assertTrue(prep["ok"])
        finish = server.dispatch(
            "memory_whale_finish",
            {"day_key": DAY, "title": "加班到十点", "lines": ["你今晚要加班到十点"], "run_sleep": False},
        )
        self.assertTrue(finish["ok"])
        self.assertEqual(self._count("episode"), 1)

    def test_unknown_mcp_tool_rejected(self) -> None:
        server = McpServer(self.app, out=io.StringIO())
        self.assertIsNone(__import__("pmem.gate.schemas", fromlist=["by_name"]).by_name("memory_whale_nope"))


if __name__ == "__main__":
    unittest.main()
