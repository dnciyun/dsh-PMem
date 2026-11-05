"""鲸眠（0.35.0）：夜间 LLM 会话写日记。

测的是「会话 → 校验 → 落库 → 转录」这条流水线，不联网：
用假模型（实现 ``complete`` 协议）喂固定的回复，检查：
1. 正常 JSON → 写出一篇 episode，派生事实进库，转录落在指定目录且标着「鲸眠」；
2. 编造专名 → validate 打回，整篇回落模板（mode=template，problems 非空）；
3. 工具调用 → memory_search 走只读通道，工具结果进转录，再出 JSON；
4. 没有原文 → 直接 no_lines，不建转录；
5. dry-run → 不落库。
"""

from __future__ import annotations

import json
import unittest

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.bench.model import Reply, Usage
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.memory.store import MemoryStore
from pmem.sleep import whale

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})
DAY = "2026-05-03"


class FakeModel:
    name = "fake-whale"
    paid = False

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, messages, tools=None, *, hint=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if self.replies:
            return self.replies.pop(0)
        return Reply(content="{}", usage=Usage(10, 5))


def ok_reply(payload: dict) -> Reply:
    return Reply(content=json.dumps(payload, ensure_ascii=False), usage=Usage(120, 40))


class WhaleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("whale")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")
        self.app = PMem(self.db, config=CFG)
        self.transcripts = self.dir / "whale-sleep"

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    def _ingest(self, texts: list[str], day: str = DAY) -> None:
        for index, text in enumerate(texts):
            journal.append(
                self.db,
                text,
                subject="self",
                session_id="s-1",
                role="user",
                ts=f"{day}T1{index}:00:00Z",
            )

    def _run(self, model, **kwargs):
        return whale.run(
            self.db,
            subject="self",
            day_key=DAY,
            store=self.store,
            app=self.app,
            config=CFG,
            model=model,
            transcript_dir=self.transcripts,
            **kwargs,
        )

    def _episodes(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]

    def test_writes_episode_and_transcript(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞", "我先去开会了"])
        model = FakeModel([
            ok_reply({
                "title": "加班到十点",
                "lines": ["你今晚要加班到十点", "你明天记得带伞"],
                "loops": ["明天带伞"],
                "notes": "明天要带伞",
            })
        ])
        result = self._run(model)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "json")
        self.assertEqual(result["session"], "鲸眠")
        self.assertEqual(self._episodes(), 1)
        self.assertTrue(result["diary"]["facts"], "日记应该派生出事实")
        self.assertEqual(result["diary"]["mode"], "model")

        transcript = self.transcripts / f"{DAY}.jsonl"
        self.assertTrue(transcript.exists())
        rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[0]["type"], "meta")
        self.assertEqual(rows[0]["session"], "鲸眠")
        self.assertEqual(rows[0]["session_id"], f"whale-sleep:{DAY}")
        self.assertTrue(any(row.get("type") == "result" for row in rows))

    def test_invented_name_falls_back_to_template(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞"])
        model = FakeModel([
            ok_reply({"title": "看了一本书", "lines": ["你读完了《海边的卡夫卡》"], "loops": []})
        ])
        result = self._run(model)
        self.assertTrue(result["ok"])
        self.assertEqual(result["diary"]["mode"], "template")
        self.assertTrue(result["diary"]["problems"], "编造的专名应该被校验记下来")

    def test_tool_call_then_json(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞", "我先去开会了"])
        call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "memory_search", "arguments": json.dumps({"query": "加班"})},
        }
        model = FakeModel([
            Reply(content="", tool_calls=[call], usage=Usage(80, 20)),
            ok_reply({"title": "加班到十点", "lines": ["你今晚要加班到十点"], "loops": []}),
        ])
        result = self._run(model)
        self.assertTrue(result["ok"])
        self.assertIn("memory_search", result["tool_calls"])
        self.assertEqual(len(model.calls), 2)
        transcript = (self.transcripts / f"{DAY}.jsonl").read_text(encoding="utf-8")
        self.assertIn('"type": "tool"', transcript)

    def test_no_lines_returns_reason(self) -> None:
        result = self._run(FakeModel([]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_lines")
        self.assertFalse((self.transcripts / f"{DAY}.jsonl").exists())

    def test_dry_run_does_not_write_episode(self) -> None:
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞"])
        model = FakeModel([ok_reply({"title": "加班", "lines": ["你今晚要加班到十点"], "loops": []})])
        result = self._run(model, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self._episodes(), 0)

    def test_scheduler_tick_uses_whale_when_enabled(self) -> None:
        """daemon 路径：开了 sleep.whale.enabled，跨天流水由鲸眠写，不再等模板。"""
        from datetime import datetime, timedelta
        from unittest.mock import patch

        from pmem.base.config import Config as Cfg
        from pmem.ops import scheduler

        yesterday = (datetime.now().astimezone() - timedelta(days=1)).date().isoformat()
        self._ingest(["今晚要加班到十点", "明天可能下雨，记得带伞", "我先去开会了"], day=yesterday)
        cfg = Cfg({
            "retrieval": {"vector": {"enabled": False}},
            "net": {"auto_extract": False},
            "journal": {"model": ""},
            "sleep": {"whale": {"enabled": True, "model": "fake", "transcript_dir": str(self.transcripts)}},
        })
        model = FakeModel([ok_reply({"title": "加班到十点", "lines": ["你今晚要加班到十点"], "loops": []})])
        with patch("pmem.sleep.whale.build_model", return_value=model):
            note = scheduler.journal_tick(self.db, cfg, "self")
        self.assertTrue(note.get("whale"), note)
        self.assertTrue(note["whale"]["ok"])
        self.assertEqual(note["whale"]["verdict"], "json")
        self.assertEqual(self._episodes(), 1)
        self.assertTrue((self.transcripts / f"{yesterday}.jsonl").exists())
        self.assertIn('"鲸眠"', (self.transcripts / f"{yesterday}.jsonl").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
