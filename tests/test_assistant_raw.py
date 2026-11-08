"""assistant 原话进 raw 通道：能力默认可用（P2-15 已判定为伪需求，不设开关）。

背景：P2-15 原本要加 ``retrieval.assistant_in_raw`` 开关，核实后发现 0.36.1 起
``raw_fallback`` 的 SQL 就**不按 role 过滤**——assistant 行的原话一直能被原文兜底
召回（零记忆 + 一条 assistant 行即可复现）。这个开关没有任何可观察语义，所以删键、
不改默认（改成 user-only 是行为变化且无收益证据）。

本文件只留正向断言：**默认配置下 assistant 原话必须能回来**。将来若有人给
``raw_fallback`` 加 ``role='user'`` 过滤，这条测试会立刻变红，提醒「能力被悄悄弄丢了」。
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal

from .support import drop_dir, temp_dir

BASE_OVERRIDES: dict = {
    "retrieval": {
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}

USER_LINE = "我昨天在河边钓到一条大鲤鱼，回家炖了汤"
ASSISTANT_LINE = "我答应你在周五之前把钥匙放在门卫那里"


def _config() -> Config:
    return Config(copy.deepcopy(BASE_OVERRIDES))


class AssistantInRawTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("assistant-raw")
        self.app = PMem.open(Path(self._dir) / "pmem.db", config=_config())
        journal.append(
            self.app.db, USER_LINE, role="user", session_id="s1",
            ts="2026-11-05T09:00:00+00:00",
        )
        journal.append(
            self.app.db, ASSISTANT_LINE, role="assistant", session_id="s1",
            ts="2026-11-05T10:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_assistant_line_is_recalled_by_raw_fallback(self) -> None:
        """默认配置（零记忆）下，assistant 原话必须能被原文兜底召回。"""
        result = self.app.store.search("门卫 钥匙", record=False)
        items = [item for item in result["items"] if str(item.get("role") or "") == "assistant"]
        self.assertTrue(items, "assistant 原话被 raw 兜底过滤掉了：P2-15 的能力丢了")
        item = items[0]
        self.assertEqual(str(item.get("source") or ""), "journal")
        self.assertIn(ASSISTANT_LINE, str(item.get("text") or ""))

    def test_user_line_is_still_recalled(self) -> None:
        result = self.app.store.search("河边 鲤鱼", record=False)
        self.assertTrue(
            [item for item in result["items"] if str(item.get("text") or "") == USER_LINE]
        )

    def test_raw_fallback_does_not_write_facts_or_journal(self) -> None:
        memory_before = self.app.db.conn.execute("SELECT COUNT(*) AS c FROM memory").fetchone()["c"]
        journal_before = self.app.db.conn.execute("SELECT COUNT(*) AS c FROM journal_line").fetchone()["c"]
        self.app.store.search("门卫 钥匙", record=False)
        self.app.store.search("河边 鲤鱼", record=False)
        memory_after = self.app.db.conn.execute("SELECT COUNT(*) AS c FROM memory").fetchone()["c"]
        journal_after = self.app.db.conn.execute("SELECT COUNT(*) AS c FROM journal_line").fetchone()["c"]
        self.assertEqual(memory_before, memory_after, "raw 通道不该往事实层写")
        self.assertEqual(journal_before, journal_after, "raw 通道不该动原文")


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
