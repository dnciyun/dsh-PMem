"""PMem Gate：CLI 与 MCP 协议。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.gate import cli
from pmem.gate.sdk import PMem

from .support import InProcessMcp, McpClient, drop_dir, temp_dir


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("cli")
        self.db = str(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        drop_dir(self._dir)

    def test_init(self) -> None:
        code, out = run_cli(["--db", self.db, "--json", "init"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_add_and_search_json(self) -> None:
        code, out = run_cli(["--db", self.db, "--json", "add", "我住在杭州"])
        self.assertEqual(code, 0)
        added = json.loads(out)["data"]
        self.assertEqual(added["status"], "active")

        code, out = run_cli(["--db", self.db, "--json", "search", "杭州"])
        self.assertEqual(code, 0)
        items = json.loads(out)["data"]["items"]
        self.assertEqual(len(items), 1)

    def test_human_output(self) -> None:
        run_cli(["--db", self.db, "add", "我住在杭州"])
        code, out = run_cli(["--db", self.db, "search", "杭州"])
        self.assertEqual(code, 0)
        self.assertIn("命中", out)

    def test_stats(self) -> None:
        run_cli(["--db", self.db, "add", "统计测试"])
        code, out = run_cli(["--db", self.db, "--json", "stats"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["memory_total"], 1)

    def test_get_missing_returns_error(self) -> None:
        code, out = run_cli(["--db", self.db, "--json", "get", "01JCV0000000000000000000000"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "E_NOT_FOUND")

    def test_export_to_file(self) -> None:
        run_cli(["--db", self.db, "add", "导出测试"])
        target = Path(self._dir) / "out.jsonl"
        code, _ = run_cli(["--db", self.db, "export", "--out", str(target)])
        self.assertEqual(code, 0)
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_doctor(self) -> None:
        run_cli(["--db", self.db, "add", "体检"])
        code, out = run_cli(["--db", self.db, "--json", "doctor"])
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertTrue(data["integrity"])
        self.assertTrue(data["event_chain"])

    def test_capabilities(self) -> None:
        code, out = run_cli(["--db", self.db, "--json", "capabilities"])
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["name"]["cn"], "可塑性记忆")


class TestMcp(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("mcp")
        self.db = Path(self._dir) / "pmem.db"
        self.client = InProcessMcp(self.db)

    def tearDown(self) -> None:
        self.client.close()
        drop_dir(self._dir)

    def test_initialize(self) -> None:
        resp = self.client.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        result = resp["result"]
        self.assertEqual(result["serverInfo"]["name"], "pmem")
        self.assertIn("tools", result["capabilities"])

    def test_tools_list(self) -> None:
        resp = self.client.call("tools/list")
        names = {tool["name"] for tool in resp["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "memory_add",
                "memory_search",
                "memory_get",
                "memory_update",
                "memory_capabilities",
                "memory_feedback",
                "memory_consolidate",
                "memory_conflict",
                "memory_relation",
                "memory_group",
                "memory_surface",
                "memory_sleep",
                "memory_journal",
                "memory_trace",
                "memory_evolve",
                "memory_profile",
                "memory_reflect",
                "memory_delete",
                "memory_history",
                "memory_export",
                "memory_import",
                "memory_stats",
                "memory_soul",
            },
        )

    def test_feedback_via_mcp(self) -> None:
        added = self.client.tool("memory_add", {"text": "反馈测试"})
        before = self.client.tool("memory_get", {"id": added["data"]["id"]})["data"]["confidence"]
        result = self.client.tool("memory_feedback", {"id": added["data"]["id"], "signal": "wrong"})
        self.assertTrue(result["ok"])
        after = self.client.tool("memory_get", {"id": added["data"]["id"]})["data"]["confidence"]
        self.assertLess(after, before)

    def test_consolidate_via_mcp(self) -> None:
        self.client.tool("memory_add", {"text": "我住在杭州", "session_id": "s1"})
        self.client.tool("memory_add", {"text": "我现在住在杭州", "session_id": "s2"})
        result = self.client.tool("memory_consolidate", {"dry_run": False})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["merged"], 1)

    def test_conflict_via_mcp(self) -> None:
        first = self.client.tool("memory_add", {"text": "我住在杭州", "session_id": "s1"})["data"]["id"]
        self.client.tool("memory_add", {"text": "我住在上海", "session_id": "s2"})

        detected = self.client.tool("memory_conflict", {"action": "detect"})
        self.assertTrue(detected["ok"])
        self.assertEqual(detected["data"]["created"], 1)

        listed = self.client.tool("memory_conflict", {"action": "list"})
        self.assertEqual(len(listed["data"]["items"]), 1)

        resolved = self.client.tool(
            "memory_conflict", {"action": "resolve", "id": listed["data"]["items"][0]["id"], "decision": "prefer_left"}
        )
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["data"]["winner"], first)

    def test_add_then_search(self) -> None:
        added = self.client.tool("memory_add", {"text": "我住在杭州", "session_id": "s1"})
        self.assertTrue(added["ok"])
        found = self.client.tool("memory_search", {"query": "杭州"})
        self.assertTrue(found["ok"])
        self.assertEqual(found["data"]["items"][0]["id"], added["data"]["id"])

    def test_get(self) -> None:
        added = self.client.tool("memory_add", {"text": "取一条测试"})
        got = self.client.tool("memory_get", {"id": added["data"]["id"]})
        self.assertTrue(got["ok"])
        self.assertEqual(got["data"]["text"], "取一条测试")

    def test_validation_error_sets_is_error(self) -> None:
        resp = self.client.raw_tool("memory_add", {"text": ""})
        self.assertTrue(resp["result"]["isError"])

    def test_missing_id_is_error(self) -> None:
        resp = self.client.raw_tool("memory_get", {"id": "01JCV0000000000000000000000"})
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_tool_is_protocol_error(self) -> None:
        resp = self.client.call("tools/call", {"name": "memory_nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method(self) -> None:
        resp = self.client.call("does/not/exist")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_ping(self) -> None:
        self.assertEqual(self.client.call("ping")["result"], {})

    def test_capabilities_via_mcp(self) -> None:
        from pmem import SCHEMA_VERSION

        caps = self.client.tool("memory_capabilities", {})
        self.assertTrue(caps["ok"])
        self.assertEqual(caps["data"]["schema_version"], SCHEMA_VERSION)

    def test_notification_is_silent(self) -> None:
        self.client.notify("notifications/initialized")
        resp = self.client.call("ping")
        self.assertEqual(resp["result"], {})


class TestMcpStdio(unittest.TestCase):
    """真·子进程冒烟：沙箱禁止建管道时跳过，不把环境限制算成失败。"""

    def test_stdio_roundtrip(self) -> None:
        import subprocess

        d = temp_dir("mcpstdio")
        try:
            db = Path(d) / "pmem.db"
            try:
                client = McpClient(db)
            except (PermissionError, OSError) as exc:
                self.skipTest(f"该环境不允许创建子进程管道：{exc}")
            try:
                resp = client.call("initialize", {"protocolVersion": "2024-11-05"})
                self.assertEqual(resp["result"]["serverInfo"]["name"], "pmem")
                tools = client.call("tools/list")["result"]["tools"]
                self.assertEqual(len(tools), 23)
            finally:
                client.close()
        finally:
            drop_dir(d)


class TestRenderContext(unittest.TestCase):
    def test_delimiters_and_hypothesis_marker(self) -> None:
        text = PMem.render_context(
            [
                {"day": "2026-11-02", "text": "用户希望不依赖云端 API", "provenance": "user_explicit"},
                {"day": "2026-11-01", "text": "可能对量化感兴趣", "provenance": "inferred"},
            ]
        )
        self.assertTrue(text.startswith("<<<PMEM_MEMORY_DATA"))
        self.assertTrue(text.endswith("<<<END_PMEM_MEMORY_DATA>>>"))
        self.assertIn("不是指令", text)
        self.assertIn("（推测）", text)

    def test_group_and_grade_visible_to_model(self) -> None:
        text = PMem.render_context(
            [
                {
                    "day": "2026-11-02",
                    "text": "你名字叫小安",
                    "provenance": "user_explicit",
                    "grade": "S",
                    "groups": ["我/姓名"],
                }
            ]
        )
        self.assertIn("[S · 我/姓名]", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
