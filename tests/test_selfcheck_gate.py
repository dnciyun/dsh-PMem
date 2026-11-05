"""作答自检的 gate 契约（0.36.0）：MCP 工具 ``memory_selfcheck`` 与 CLI ``pmem selfcheck``。

模块本身的正确性在 ``tests/test_selfcheck.py`` 里钉；这里只钉「怎么连到底层」：
- 工具数、名字出现在 tools/list；
- MCP 调用返回 ``{"ok": true, "data": {"clean": ..., "issues": ..., "report": ...}}``
  —— 信封的 ok 是「调用成功」，data.clean 才是「回答对不对得上」；
- CLI 支持位置参数和 stdin 两种输入。
"""

from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from pmem.gate import cli

from .support import InProcessMcp, drop_dir, temp_dir


class SelfcheckGateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("selfcheck-gate")
        self.db = self.dir / "pmem.db"
        self.mcp = InProcessMcp(self.db)
        self.mcp.tool("memory_add", {"text": "2026-10-26 那天把青鸟七号的代码模块全部对上了"})
        self.mcp.tool(
            "memory_add",
            {"text": "李静当时也在，我们三个人一起对到很晚", "valid_from": "2026-10-26T09:00:00+00:00"},
        )

    def tearDown(self) -> None:
        self.mcp.close()
        drop_dir(self.dir)

    def test_tool_is_listed(self) -> None:
        resp = self.mcp.call("tools/list", {})
        names = {tool["name"] for tool in resp["result"]["tools"]}
        self.assertIn("memory_selfcheck", names)

    def test_mcp_tool_reports_fabricated_date(self) -> None:
        payload = self.mcp.tool(
            "memory_selfcheck",
            {"answer": "我们是 2026-09-09 把青鸟七号对上的。", "question": "青鸟七号什么时候对上的？"},
        )
        self.assertTrue(payload["ok"], payload)
        data = payload["data"]
        self.assertFalse(data["clean"])
        self.assertTrue(any(issue["kind"] == "date" for issue in data["issues"]), data)
        self.assertIn("自检发现", data["report"])

    def test_mcp_tool_clean_answer(self) -> None:
        payload = self.mcp.tool(
            "memory_selfcheck",
            {"answer": "2026-10-26 那天把青鸟七号对上了，李静也在。"},
        )
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["data"]["clean"], payload["data"])

    def test_mcp_tool_rejects_empty_answer(self) -> None:
        payload = self.mcp.tool("memory_selfcheck", {"answer": "   "})
        self.assertFalse(payload["ok"], payload)
        self.assertEqual(payload["error"]["code"], "E_VALIDATION")

    def test_cli_selfcheck_positional_and_stdin(self) -> None:
        buf = StringIO()
        with redirect_stdout(buf):
            code = cli.main(
                ["--db", str(self.db), "--json", "selfcheck", "答案里写了 2026-02-30 这件事"]
            )
        self.assertEqual(code, 0, buf.getvalue())
        data = json.loads(buf.getvalue())["data"]
        self.assertFalse(data["clean"])

        with mock.patch("sys.stdin", StringIO("答案里写了 2026-10-26 这件事")):
            buf = StringIO()
            with redirect_stdout(buf):
                code = cli.main(["--db", str(self.db), "--json", "selfcheck"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("data", json.loads(buf.getvalue()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
