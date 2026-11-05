"""「我说过的话」通道的接线（0.32.0 ③）。

通道本身在 `channels.assistant_voice`，这里只守接线三件事：

1. 查询里有自指线索 + 库里有 assistant 原话 → 结果里出现它，且带 `role="assistant"`；
2. 没线索 / 配置关闭 → 一条都不加（不能拿助手的话去顶用户事实）；
3. 接线不破坏条数与预算：payload 长度不超过 k。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.memory import journal
from pmem.memory.store import MemoryStore

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})
CFG_CUE_ONLY = Config(
    {
        "retrieval": {
            "vector": {"enabled": False},
            "raw_fallback": False,
        },
        "net": {"auto_extract": False},
    }
)
CFG_OFF = Config(
    {
        "retrieval": {
            "vector": {"enabled": False},
            "assistant_voice": {"enabled": False},
            # 原话兜底也不按 role 过滤，关掉它才能证明「看不见助理原话」是
            # 本通道关掉的功劳，而不是被别的通道顺手捞上来的。
            "raw_fallback": False,
        },
        "net": {"auto_extract": False},
    }
)
TS = "2026-11-03T10:00:00Z"
DAY = clock.occurred_day(TS) or "2026-11-03"

ASSISTANT_LINE = "我给自己安了个夜班台账员的设定：只记你说出口的，不猜你心里，免得假聪明。"
USER_LINE = "今晚杯堆清完了，手都泡皱"


class AssistantVoiceWiringCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("assistant-wiring")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        clock.set_now(datetime.fromisoformat(TS))
        journal.append(
            self.db, USER_LINE, role="user", ts=TS, session_id="s1"
        )
        journal.append(
            self.db, ASSISTANT_LINE, role="assistant", ts=TS, session_id="s1"
        )

    def tearDown(self) -> None:
        self.db.close()
        clock.reset_now()
        drop_dir(self.dir)

    def _store(self, cfg: Config) -> MemoryStore:
        return MemoryStore(self.db, config=cfg, subject="self", actor="test")

    def _find(self, payload: list, needle: str) -> dict | None:
        for item in payload:
            if needle in str(item.get("text") or ""):
                return item
        return None

    def test_cue_query_recalls_assistant_utterance(self) -> None:
        """关掉原话兜底，确保这条是 assistant 通道自己捞回来的（并留下元信息）。"""
        result = self._store(CFG_CUE_ONLY).search("你上次说自己晚上记东西那个身份叫什么", k=5)
        item = self._find(result["items"], "夜班台账员")
        self.assertIsNotNone(item, result["items"])
        self.assertEqual(item["role"], "assistant")
        self.assertEqual(item["source"], "journal")
        self.assertGreaterEqual(result.get("assistant_voice", {}).get("added", 0), 1, result)

    def test_default_config_keeps_role_marker(self) -> None:
        """默认配置下（原话兜底也开）同一句话也是 assistant 角色，不重复出现。"""
        result = self._store(CFG).search("你上次说自己晚上记东西那个身份叫什么", k=5)
        hits = [i for i in result["items"] if "夜班台账员" in str(i.get("text") or "")]
        self.assertEqual(len(hits), 1, result["items"])
        self.assertEqual(hits[0]["role"], "assistant")

    def test_no_cue_keeps_it_out(self) -> None:
        result = self._store(CFG).search("今晚杯堆清完了吗", k=5)
        self.assertIsNone(self._find(result["items"], "夜班台账员"))
        self.assertNotIn("assistant_voice", result)

    def test_config_off_disables_channel(self) -> None:
        result = self._store(CFG_OFF).search("你上次说自己晚上记东西那个身份叫什么", k=5)
        self.assertIsNone(self._find(result["items"], "夜班台账员"))
        self.assertNotIn("assistant_voice", result)

    def test_payload_respects_k(self) -> None:
        result = self._store(CFG).search("你上次说自己晚上记东西那个身份叫什么", k=1)
        self.assertLessEqual(len(result["items"]), 1, result["items"])


if __name__ == "__main__":
    unittest.main()
