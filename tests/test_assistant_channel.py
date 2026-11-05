"""assistant 原话通道：只在「我（助手）说过什么」的问句上开，且要标清说话人。

为什么单独测这一路：assistant 的行只在 journal_line/journal_fts，事实层不能收；
如果为了补「助手自己说过的话」把这路放宽，最先坏掉的是「用户事实」的可信度。
所以这里同时钉住「线索命中才召回」和「撤回行不能回来」两条边界。
"""

from __future__ import annotations

import unittest

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.memory.retrieval import channels

from .support import drop_dir, temp_dir

ASSISTANT_TS = "2026-04-14T09:32:00+08:00"
USER_TS = "2026-04-14T09:30:00+08:00"
ASSISTANT_TEXT = "薄荷别浇太多水，土干了再浇"
CLUE_QUERY = "你上次说薄荷浇水的事，具体怎么说的？"


class AssistantVoiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("assistant-voice")
        self.app = PMem.open(
            self._dir / "pmem.db",
            config=Config({"retrieval": {"vector": {"enabled": False}}}),
        )
        self.app.db.init()
        journal.append(self.app.db, "今天随便聊聊薄荷", role="user", ts=USER_TS)
        self.line = journal.append(
            self.app.db, ASSISTANT_TEXT, role="assistant", ts=ASSISTANT_TS
        )

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _voice(self, query: str) -> list[dict]:
        return channels.assistant_voice(self.app.store, q=query, terms=[])

    def test_clue_hits_assistant_line_with_provenance_fields(self) -> None:
        items = self._voice(CLUE_QUERY)
        self.assertTrue(items, "有线索且库里有 assistant 行，却没召回")
        item = items[0]
        self.assertEqual(item["role"], "assistant")
        self.assertEqual(item["author"], "assistant")
        self.assertEqual(item["source"], "journal")
        self.assertEqual(item["kind"], "journal")
        self.assertEqual(item["valid_from"], ASSISTANT_TS)
        self.assertEqual(item["day"], "2026-04-14")
        self.assertEqual(item["provenance"], "observed")
        self.assertEqual(item["channels"], ["journal"])
        self.assertLessEqual(item["score"], 0.8)

    def test_no_clue_returns_empty(self) -> None:
        self.assertEqual(self._voice("薄荷怎么浇水"), [], "没有『你说过』线索时不能召助手的话")
        self.assertTrue(self._voice(CLUE_QUERY), "反向对照：线索问句仍应命中")

    def test_retracted_assistant_line_is_excluded(self) -> None:
        journal.retract(self.app.db, self.line["id"], reason="这句不算数了")
        self.assertEqual(self._voice(CLUE_QUERY), [], "撤回过的 assistant 行不允许再被召回")

    def test_render_context_marks_assistant_but_not_user(self) -> None:
        user_item = {
            "id": "u1",
            "text": "用户说自己喜欢薄荷",
            "role": "user",
            "day": "2026-04-14",
            "valid_from": USER_TS,
        }
        assistant_item = self._voice(CLUE_QUERY)[0]
        rendered = PMem.render_context([user_item, assistant_item], budget=1000)
        lines = [line for line in rendered.splitlines() if line.startswith("- ")]
        user_line = next(line for line in lines if "薄荷" in line and "用户说" in line)
        assistant_line = next(line for line in lines if ASSISTANT_TEXT in line)
        self.assertNotIn("我说过的", user_line)
        self.assertIn("我说过的", assistant_line, "assistant 条目没标出『是我说的』")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
