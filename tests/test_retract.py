"""M23：撤回（墓碑）——不是删除，是「这句/这条不算数了」。

三条不变量，逐个钉住：
1. 撤回只追加：原文行、事件链一个字都不改（行还在，只是默认不返回）；
2. 默认视图隐藏、认账视图可见（`include_retracted` / `history`）；
3. 认不出目标就报错——宁可撤不掉，也不能撤错。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem import PMem
from pmem.memory import journal

from .support import InProcessMcp, drop_dir, temp_dir


class JournalRetractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PMem.open(str(Path(self.tmp.name) / "retract.db"))
        self.line = self.app.journal_append(
            "刚才那句「我明天搬家」是开玩笑的", role="user", session_id="s-1"
        )["data"]
        self.other = self.app.journal_append(
            "这句要留着：周六去看牙", role="user", session_id="s-1"
        )["data"]

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def test_retract_hides_from_trace_but_keeps_row(self) -> None:
        self.assertEqual(self.app.trace("我明天搬家")["data"]["count"], 1)
        result = self.app.retract(self.line["id"], reason="说错了")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["target"], "journal_line")
        self.assertFalse(result["data"]["already"])
        self.assertEqual(self.app.trace("我明天搬家")["data"]["count"], 0)
        # 行还在：原文没有被删掉
        row = self.app.db.conn.execute(
            "SELECT text FROM journal_line WHERE id=?", (self.line["id"],)
        ).fetchone()
        self.assertIn("我明天搬家", row["text"])

    def test_include_retracted_still_sees_it(self) -> None:
        self.app.retract(self.line["id"], reason="说错了")
        data = self.app.trace("我明天搬家", include_retracted=True)["data"]
        self.assertEqual(data["count"], 1)
        self.assertTrue(data["items"][0]["retracted"])
        self.assertIn("含已撤回", data["scope"])

    def test_retract_is_idempotent(self) -> None:
        first = self.app.retract(self.line["id"])["data"]
        second = self.app.retract(self.line["id"])["data"]
        self.assertFalse(first["already"])
        self.assertTrue(second["already"])
        rows = self.app.db.conn.execute(
            "SELECT COUNT(*) c FROM journal_retraction WHERE line_id=?", (self.line["id"],)
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_retracted_line_leaves_digest_queue(self) -> None:
        self.assertIn(self.line["id"], [r["id"] for r in journal.pending(self.app.db)])
        self.app.retract(self.line["id"], reason="别记这句")
        pending = [r["id"] for r in journal.pending(self.app.db)]
        self.assertNotIn(self.line["id"], pending)
        self.assertIn(self.other["id"], pending)
        stats = journal.stats(self.app.db)
        self.assertEqual(stats["retracted"], 1)

    def test_event_log_records_retraction(self) -> None:
        self.app.retract(self.line["id"], reason="说错了")
        ops = [h["op"] for h in self.app.history(f"journal_line:{self.line['id']}")["data"]["items"]]
        self.assertIn("journal.retract", ops)

    def test_unknown_target_raises_not_found(self) -> None:
        result = self.app.retract("01NOTAREALID")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_NOT_FOUND")

    def test_kind_mismatch_does_not_fall_through(self) -> None:
        result = self.app.retract(self.line["id"], kind="memory")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_NOT_FOUND")


class MemoryRetractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PMem.open(str(Path(self.tmp.name) / "retract.db"))
        self.mem = self.app.add(
            "我们周三下午三点在楼下开会", kind="semantic", provenance="user_explicit"
        )["data"]

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def test_retract_memory_hides_from_search(self) -> None:
        self.assertEqual(len(self.app.search("周三 开会")["data"]["items"]), 1)
        result = self.app.retract(self.mem["id"], reason="时间改了")
        self.assertEqual(result["data"]["target"], "memory")
        self.assertEqual(result["data"]["status"], "archived")
        self.assertEqual(len(self.app.search("周三 开会")["data"]["items"]), 0)

    def test_get_reports_retracted_and_restore_clears_it(self) -> None:
        self.app.retract(self.mem["id"], reason="时间改了")
        item = self.app.get(self.mem["id"])["data"]
        self.assertEqual(item["status"], "archived")
        self.assertTrue(item["retracted"])
        # 撤回不是删除：原文一个字没改，能恢复
        self.assertIn("周三下午三点", item["text"])
        self.app.update(self.mem["id"], set_fields={"status": "active"})
        item = self.app.get(self.mem["id"])["data"]
        self.assertEqual(item["status"], "active")
        self.assertFalse(item["retracted"])
        self.assertEqual(len(self.app.search("周三 开会")["data"]["items"]), 1)

    def test_retract_does_not_touch_text_or_delete(self) -> None:
        self.app.retract(self.mem["id"], reason="时间改了")
        row = self.app.db.conn.execute(
            "SELECT text_raw, status, deleted_at FROM memory WHERE id=?", (self.mem["id"],)
        ).fetchone()
        self.assertIn("周三下午三点", row["text_raw"])
        self.assertEqual(row["status"], "archived")
        self.assertIsNone(row["deleted_at"])
        ops = [h["op"] for h in self.app.history(self.mem["id"])["data"]["items"]]
        self.assertIn("memory.retract", ops)

    def test_retract_deleted_memory_is_refused(self) -> None:
        self.app.delete(self.mem["id"])
        result = self.app.retract(self.mem["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_VALIDATION")


class McpRetractTest(unittest.TestCase):
    """走一遍真正的对外通道：JSON-RPC → dispatch → SDK → 库。"""

    def setUp(self) -> None:
        self._dir = temp_dir("retract-mcp")
        self.client = InProcessMcp(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        self.client.close()
        drop_dir(self._dir)

    def test_retract_tool_roundtrip(self) -> None:
        line = self.client.app.journal_append(
            "刚才那句是开玩笑的", role="user", session_id="s1"
        )["data"]
        before = self.client.tool("memory_trace", {"query": "刚才那句是开玩笑的"})
        self.assertEqual(before["data"]["count"], 1)
        gone = self.client.tool("memory_retract", {"target": line["id"], "reason": "说错了"})
        self.assertTrue(gone["ok"])
        self.assertEqual(gone["data"]["target"], "journal_line")
        self.assertFalse(gone["data"]["already"])
        after = self.client.tool("memory_trace", {"query": "刚才那句是开玩笑的"})
        self.assertEqual(after["data"]["count"], 0)
        shown = self.client.tool(
            "memory_trace", {"query": "刚才那句是开玩笑的", "include_retracted": True}
        )
        self.assertTrue(shown["data"]["items"][0]["retracted"])

    def test_retract_memory_tool(self) -> None:
        mem = self.client.tool("memory_add", {"text": "我们周三下午三点开会"})["data"]
        gone = self.client.tool("memory_retract", {"target": mem["id"], "reason": "时间改了"})
        self.assertTrue(gone["ok"])
        self.assertEqual(gone["data"]["target"], "memory")
        hit = self.client.tool("memory_search", {"query": "周三 开会"})
        self.assertEqual(hit["data"]["items"], [])


if __name__ == "__main__":
    unittest.main()
