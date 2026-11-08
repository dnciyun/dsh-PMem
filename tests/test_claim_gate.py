"""claim 对外接口（task-46）：CLI + MCP + 关闸契约。

守住四件事：

1. **关闸零变化**：``claim.enabled=false``（默认）时 MCP tools/list 仍是 30 个、
   没有 ``claim_*``；调用这些工具和「工具不存在」一样被拒；既有工具返回形状不变。
2. **打开后 +5**：``claim.enabled=true`` 时 tools/list 变 35，五个工具都可调。
3. **同一份实现**：MCP 的 claim_* 结果 == cli.claim_*_payload 的结果（不复制逻辑）。
4. **CLI 分支**：``claim list/search/delete/extract --prep/--apply/--rule`` 全通；
   关闸时 prep/rule 返回 enabled=false 且不写任何 claim 行。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate import cli, schemas
from pmem.gate.mcp_server import McpServer
from pmem.gate.sdk import PMem

from .support import InProcessMcp, drop_dir, temp_dir

DAY = "2026-11-07"
LINE = "小王在字节工作，我不吃香菜"


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


def run_cli_json(argv: list[str]) -> tuple[int, dict]:
    code, text = run_cli(argv)
    return code, json.loads(text)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        clock.set_now("2026-11-07T12:00:00+08:00")
        self.dir = temp_dir("claim-gate")
        self.db = str(self.dir / "pmem.db")
        self.config_path = str(self.dir / "config.toml")
        code, _ = run_cli(["--db", self.db, "--config", self.config_path, "--json", "init"])
        self.assertEqual(code, 0)
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        drop_dir(self.dir)

    def _write_config(self, enabled: bool) -> None:
        Path(self.config_path).write_text(
            f"claim.enabled = {'true' if enabled else 'false'}\n", encoding="utf-8"
        )

    def _app(self, *, enabled: bool) -> PMem:
        app = PMem.open(self.db, config=Config({"claim": {"enabled": enabled}}))
        self._apps.append(app)
        return app

    def _seed_line(self, app: PMem) -> str:
        result = app.journal_append(LINE, role="user", session_id="claim-gate")
        self.assertTrue(result["ok"], result)
        return str(result["data"]["id"])

    def _seed_line_named(self) -> str:
        """用独立句柄写一行原话，返回 journal_line id（写库与 CLI 子进程共享同一个 db 文件）。"""
        app = self._app(enabled=False)
        line_id = self._seed_line(app)
        app.close()
        self._apps.remove(app)
        return line_id

    def _server(self, app: PMem) -> tuple[McpServer, io.StringIO]:
        buf = io.StringIO()
        server = McpServer(app, out=buf)
        return server, buf

    def _call(self, server: McpServer, buf: io.StringIO, name: str, args: dict) -> dict:
        buf.seek(0)
        buf.truncate(0)
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        response = json.loads(buf.getvalue().strip().splitlines()[-1])
        if "error" in response:
            return response
        return json.loads(response["result"]["content"][0]["text"])

    def _list_tools(self, server: McpServer, buf: io.StringIO) -> list[dict]:
        buf.seek(0)
        buf.truncate(0)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        return json.loads(buf.getvalue().strip().splitlines()[-1])["result"]["tools"]


class ClaimToolCatalogTest(_Base):
    def test_default_off_keeps_tool_count_and_names(self) -> None:
        """关闸时 tools/list 与 0.40.0 完全一致：30 个、没有 claim_*。"""
        mcp = InProcessMcp(Path(self.db))
        try:
            tools = mcp.call("tools/list")["result"]["tools"]
            names = {tool["name"] for tool in tools}
            self.assertEqual(len(tools), 30)
            self.assertEqual(names, {tool["name"] for tool in schemas.TOOLS})
            self.assertFalse(any(name.startswith("claim_") for name in names))
        finally:
            mcp.close()

    def test_claim_tools_hidden_when_disabled(self) -> None:
        app = self._app(enabled=False)
        server, buf = self._server(app)
        tools = self._list_tools(server, buf)
        self.assertEqual(len(tools), 30)
        # 调用也不存在：unknown tool（不是 ok:false 的业务错误）。
        response = self._call(server, buf, "claim_list", {})
        self.assertIn("error", response)
        self.assertIn("unknown tool", response["error"]["message"])
        # 既有工具返回不变。
        stats = self._call(server, buf, "memory_stats", {})
        self.assertTrue(stats["ok"], stats)
        self.assertIn("data", stats)

    def test_enabled_exposes_five_more_tools(self) -> None:
        app = self._app(enabled=True)
        server, buf = self._server(app)
        tools = self._list_tools(server, buf)
        names = {tool["name"] for tool in tools}
        self.assertEqual(len(tools), 35)
        for name in schemas.CLAIM_TOOL_NAMES:
            self.assertIn(name, names)
        self.assertEqual(names - set(schemas.CLAIM_TOOL_NAMES), {t["name"] for t in schemas.TOOLS})


class ClaimMcpSharedImplementationTest(_Base):
    """MCP claim_* 必须直接复用 cli.claim_*_payload，不出现第二份实现。"""

    def test_list_search_delete_delegate_to_cli_helpers(self) -> None:
        app = self._app(enabled=True)
        line_id = self._seed_line(app)
        self.assertFalse(hasattr(app, "claim_list"), "SDK 不应悄悄新增 claim 方法（对外接口走 cli 共享函数）")
        from pmem.memory import claim as claim_mod

        inserted = claim_mod.create(
            app.db,
            kind="preference",
            predicate="不吃",
            object="香菜",
            polarity=0,
            evidence_quote="我不吃香菜",
            source_line=line_id,
            status="happened",
        )
        claim_id = inserted["claim_id"]

        server, buf = self._server(app)
        listed = self._call(server, buf, "claim_list", {})
        self.assertEqual(listed["data"], cli.claim_list_payload(app)["data"], "MCP claim_list 没复用 CLI 实现")
        self.assertEqual(listed["data"]["total"], 1)
        self.assertEqual(listed["data"]["items"][0]["object"], "香菜")

        searched = self._call(server, buf, "claim_search", {"query": "香菜", "k": 3})
        self.assertEqual(searched["data"], cli.claim_search_payload(app, query="香菜", k=3)["data"])
        self.assertEqual(searched["data"]["total"], 1)

        deleted = self._call(server, buf, "claim_delete", {"claim_id": claim_id, "reason": "mis-extract"})
        self.assertEqual(deleted["data"], {"deleted": True, "claim_id": claim_id})
        self.assertEqual(cli.claim_list_payload(app)["data"]["total"], 0, "软删后默认列表不该再有")


class ClaimMasterSwitchTest(_Base):
    """task-47：主开关 claim.enabled 关→开→工具数 30→35，关时 claim_* 被拒绝。"""

    def test_master_off_to_on_tool_count_and_calls(self) -> None:
        off_app = self._app(enabled=False)
        off_server, off_buf = self._server(off_app)
        self.assertEqual(len(self._list_tools(off_server, off_buf)), 30, "主开关关：工具数必须还是 30")
        denied = self._call(off_server, off_buf, "claim_list", {})
        self.assertIn("error", denied, "主开关关：claim_* 不能可调")
        self.assertIn("unknown tool", denied["error"]["message"])

        on_app = self._app(enabled=True)
        on_server, on_buf = self._server(on_app)
        tools = self._list_tools(on_server, on_buf)
        names = {tool["name"] for tool in tools}
        self.assertEqual(len(tools), 35, "主开关开：工具数 30 → 35")
        self.assertTrue(set(schemas.CLAIM_TOOL_NAMES).issubset(names))
        allowed = self._call(on_server, on_buf, "claim_list", {})
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(allowed["data"]["enabled"], True)

    def test_master_config_roundtrip_gates_extraction(self) -> None:
        """面板 POST master 走的就是 config set：关 → 开 → 关 后内核行为跟着变。"""
        self._write_config(False)
        self._seed_line_named()
        base = ["--db", self.db, "--config", self.config_path, "--json"]

        code, before = run_cli_json([*base, "claim", "extract", "--prep", "--day", DAY])
        self.assertEqual(code, 0)
        self.assertFalse(before["data"]["enabled"])
        self.assertEqual(before["data"]["material"], [])

        code, _ = run_cli_json([*base, "config", "set", "claim.enabled", "true"])
        self.assertEqual(code, 0)
        code, after = run_cli_json([*base, "claim", "extract", "--prep", "--day", DAY])
        self.assertEqual(code, 0)
        self.assertTrue(after["data"]["enabled"], "主开关打开后 prep 才读材料")
        self.assertEqual(len(after["data"]["material"]), 1)

        code, _ = run_cli_json([*base, "config", "set", "claim.enabled", "false"])
        self.assertEqual(code, 0)
        code, again = run_cli_json([*base, "claim", "extract", "--prep", "--day", DAY])
        self.assertFalse(again["data"]["enabled"], "关回去后 prep 立刻回到 no-op")
        self.assertEqual(again["data"]["material"], [])


class ClaimMcpExtractTest(_Base):
    def test_prep_apply_roundtrip(self) -> None:
        app = self._app(enabled=True)
        line_id = self._seed_line(app)

        server, buf = self._server(app)
        prepared = self._call(server, buf, "claim_prep", {"day_key": DAY})
        self.assertTrue(prepared["ok"], prepared)
        data = prepared["data"]
        self.assertTrue(data["enabled"])
        self.assertEqual(data["day_key"], DAY)
        self.assertEqual(len(data["material"]), 1)
        self.assertIn("claim JSON", data["task"])

        payload = {
            "claims": [
                {
                    "kind": "preference",
                    "predicate": "不吃",
                    "object": "香菜",
                    "polarity": 0,
                    "status": "happened",
                    "source_line": line_id,
                    "evidence_quote": "我不吃香菜",
                    "confidence": 0.95,
                }
            ]
        }
        applied = self._call(server, buf, "claim_apply", {"day_key": DAY, "payload": payload})
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(applied["data"]["kept"], 1)
        self.assertEqual(applied["data"]["inserted"], 1)

        listed = self._call(server, buf, "claim_list", {})
        self.assertEqual(listed["data"]["total"], 1)
        item = listed["data"]["items"][0]
        self.assertEqual(item["kind"], "preference")
        self.assertEqual(item["polarity"], 0)

    def test_apply_rejects_missing_payload(self) -> None:
        app = self._app(enabled=True)
        server, buf = self._server(app)
        result = self._call(server, buf, "claim_apply", {"day_key": DAY})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_VALIDATION")

    def test_disabled_prep_does_not_touch_tables(self) -> None:
        app = self._app(enabled=False)
        self._seed_line(app)
        server, buf = self._server(app)
        prepared = self._call(server, buf, "claim_prep", {"day_key": DAY})
        # 关闸时工具不可见，连调用都不该成功。
        self.assertIn("error", prepared)
        rows = app.db.conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()
        self.assertEqual(int(rows["n"]), 0)


class ClaimCliTest(_Base):
    def test_cli_off_prep_and_rule_are_noops(self) -> None:
        self._write_config(False)
        self._seed_line_named()

        code, prep = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "extract", "--prep", "--day", DAY]
        )
        self.assertEqual(code, 0)
        self.assertTrue(prep["ok"])
        self.assertFalse(prep["data"]["enabled"])
        self.assertEqual(prep["data"]["material"], [])

        code, rule = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "extract", "--rule", "--day", DAY]
        )
        self.assertEqual(code, 0)
        self.assertFalse(rule["data"]["enabled"])
        self.assertEqual(rule["data"]["claims"], [])

        app = self._app(enabled=False)
        rows = app.db.conn.execute("SELECT COUNT(*) AS n FROM claim").fetchone()
        self.assertEqual(int(rows["n"]), 0, "关闸时 prep/rule 不能写 claim")

    def test_cli_rule_extract_and_list_search_delete(self) -> None:
        self._write_config(True)
        self._seed_line_named()

        code, rule = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "extract", "--rule", "--day", DAY]
        )
        self.assertEqual(code, 0)
        self.assertTrue(rule["ok"], rule)
        self.assertTrue(rule["data"]["enabled"])
        self.assertGreaterEqual(rule["data"]["inserted"], 1)

        code, listed = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "list"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(listed["data"]["total"], 1)
        self.assertTrue(listed["data"]["enabled"])
        claim_id = listed["data"]["items"][0]["id"]

        code, searched = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "search", "香菜", "--k", "3"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(searched["data"]["total"], 1)

        code, deleted = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "delete", claim_id]
        )
        self.assertEqual(code, 0)
        self.assertTrue(deleted["data"]["deleted"])

        code, after = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "list"]
        )
        self.assertEqual(after["data"]["total"], 0)
        # 软删留墓碑：默认不列，但 include_superseded 也不该把 deleted 列出来。
        self.assertEqual(after["data"]["items"], [])

    def test_cli_apply_from_json_file(self) -> None:
        self._write_config(True)
        line = self._seed_line_named()
        payload_path = self.dir / "claims.json"
        payload_path.write_text(
            json.dumps(
                {
                    "claims": [
                        {
                            "kind": "preference",
                            "predicate": "不吃",
                            "object": "香菜",
                            "polarity": 0,
                            "status": "happened",
                            "source_line": line,
                            "evidence_quote": "我不吃香菜",
                            "confidence": 0.9,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code, applied = run_cli_json(
            [
                "--db", self.db, "--config", self.config_path, "--json",
                "claim", "extract", "--apply", str(payload_path), "--day", DAY,
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(applied["data"]["inserted"], 1)

        # 读不到的文件 → 可读错误，不是 traceback。
        code, missing = run_cli_json(
            [
                "--db", self.db, "--config", self.config_path, "--json",
                "claim", "extract", "--apply", str(self.dir / "nope.json"), "--day", DAY,
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"]["code"], "E_VALIDATION")

    def test_cli_delete_unknown_claim_returns_validation_error(self) -> None:
        self._write_config(True)
        code, result = run_cli_json(
            ["--db", self.db, "--config", self.config_path, "--json", "claim", "delete", "NOTFOUND"]
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_VALIDATION")


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
