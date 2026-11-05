"""渲染侧时间语义：valid_from 是否从存储一路透到模型能看到的层。

0.31.0 的核心改动把 memory.day_key 改成「发生日」，但模型真正看到的不是
数据库列，而是 search payload / MCP 文本 / 插件渲染。这里钉三件事：
1. 原生检索 item 带 valid_from（等于写入时给的 ts），day 是发生日；
2. raw_fallback 命中的 assistant 行带 role（兜底是唯一能召回助手原话的通道）；
3. MCP 的 memory_search 传输文本里 valid_from 仍在（缺失时保留 None）。
"""

from __future__ import annotations

import io
import json
import unittest

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.mcp_server import McpServer
from pmem.gate.sdk import PMem
from pmem.memory import journal

from .support import drop_dir, temp_dir

TS = "2026-04-13T21:26:00+08:00"
TS2 = "2026-04-14T09:32:00+08:00"


class OccurredTimeRenderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("occurred-render")
        self.app = PMem.open(
            self._dir / "pmem.db",
            config=Config({"retrieval": {"vector": {"enabled": False}}}),
        )
        self.app.db.init()

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_search_payload_keeps_valid_from_and_occurred_day(self) -> None:
        self.app.store.add(
            "青鸟七号 代码 模块 全部对上",
            valid_from=TS,
            day_key=clock.occurred_day(TS),
        )
        result = self.app.store.search("青鸟七号", k=5)
        self.assertTrue(result["items"], result)
        item = result["items"][0]
        self.assertEqual(item["valid_from"], TS, "原生检索 item 丢了发生时刻，渲染层就无从判断相对日期")
        self.assertEqual(item["day"], clock.occurred_day(TS), "day 应该是发生日，不是写入/巩固日")

    def test_raw_fallback_assistant_item_carries_role(self) -> None:
        journal.append(self.app.db, "今天天气还行，随便聊聊", role="user", ts=TS)
        journal.append(self.app.db, "薄荷别浇太多水，土干了再浇", role="assistant", ts=TS2)
        items = self.app.store._raw_fallback(q="薄荷浇水", terms=[], k=5, cov_best=0.0, got=0)
        assistants = [item for item in items if item.get("role") == "assistant"]
        self.assertTrue(assistants, f"raw_fallback 没把 assistant 的 role 透出来：{items}")
        self.assertEqual(assistants[0]["source"], "journal")
        self.assertEqual(assistants[0]["provenance"], "observed")
        self.assertEqual(assistants[0]["valid_from"], TS2, "原话行的时间应作为发生时刻透出")

    def test_mcp_memory_search_text_keeps_valid_from(self) -> None:
        self.app.store.add("青鸟七号 代码 模块 全部对上", valid_from=TS)
        self.app.store.add("无时间锚点的蝴蝶记忆")
        out = io.StringIO()
        server = McpServer(self.app, out=out)
        server.tools_call(1, {"name": "memory_search", "arguments": {"query": "青鸟七号", "k": 5}})
        message = json.loads(out.getvalue().splitlines()[-1])
        payload = json.loads(message["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"], payload)
        item = payload["data"]["items"][0]
        self.assertEqual(item["valid_from"], TS)

        # 没有 valid_from 的旧记忆也不能在新契约里变成假时间：字段在，值为 None。
        result = self.app.store.search("无时间锚点的蝴蝶", k=5)
        butterfly = result["items"][0]
        self.assertIn("valid_from", butterfly)
        self.assertIsNone(butterfly["valid_from"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
