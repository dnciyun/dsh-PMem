"""CLI / MCP 契约（W3 批次）：默认值与配置对齐、doctor 输出、export 事件、新命令接线。

这些用例钉的是「命令怎么连到底层」而不是底层实现本身：
- CLI 的默认值必须跟 SDK/配置一致（别再出现 CLI 写死、SDK 可配的「两个默认」）；
- doctor 的 JSON 要带完整性明细和 SQLite 版本（脚本/验收要用）；
- MCP 的 memory_export 要能带事件（与 CLI `--with-events`、SDK `include_events` 对齐）；
- vacuum / restore 这类新命令的参数与错误分支（底层函数由别的批次落库，未落时跳过）。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from pmem.base.db import Database
from pmem.gate import cli
from pmem.gate.schemas import TOOLS
from pmem.gate.sdk import PMem

from .support import InProcessMcp, drop_dir, temp_dir

MCP_TOOL_COUNT = len(TOOLS)
HAS_VACUUM = hasattr(Database, "vacuum")
HAS_RESTORE = importlib.util.find_spec("pmem.ops.restore") is not None


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = StringIO()
    with redirect_stdout(buf):
        try:
            code = cli.main(argv)
        finally:
            pass
    return code, buf.getvalue()


def parse_json(text: str) -> dict:
    return json.loads(text)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("cli-contract")
        self.db = str(self.dir / "pmem.db")
        code, _ = run_cli(["--db", self.db, "--json", "init"])
        self.assertEqual(code, 0)

    def tearDown(self) -> None:
        drop_dir(self.dir)

    def _config(self, toml: str) -> str:
        path = self.dir / "config.toml"
        path.write_text(toml, encoding="utf-8")
        return str(path)


class SleepResumeDefaultCase(_Case):
    def _partial_run(self) -> str:
        app = PMem.open(self.db)
        try:
            for index in range(4):
                app.add(f"续跑测试记忆 {index}", kind="semantic")
            result = app.sleep(day_key="2026-11-04", stop_at="s2_dedup", resume=False)
            data = result["data"]
            self.assertIn(data["status"], ("partial",))
            return data["run_id"]
        finally:
            app.close()

    def test_sleep_run_defaults_to_resume(self) -> None:
        """`pmem sleep run` 默认续跑（与 SDK/MCP/文档一致）。"""
        partial_id = self._partial_run()
        code, out = run_cli(["--db", self.db, "--json", "sleep", "run", "--day", "2026-11-04"])
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertTrue(data.get("resumed"), data)
        self.assertEqual(data["run_id"], partial_id)

    def test_no_resume_starts_new_run(self) -> None:
        partial_id = self._partial_run()
        code, out = run_cli(
            ["--db", self.db, "--json", "sleep", "run", "--day", "2026-11-04", "--no-resume"]
        )
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertFalse(data.get("resumed"), data)
        self.assertNotEqual(data["run_id"], partial_id)


class SearchDefaultsCase(_Case):
    def test_search_k_budget_follow_config(self) -> None:
        """CLI 不再写死 k=8/budget=600：配置里的 top_k 要生效。"""
        app = PMem.open(self.db)
        try:
            for index in range(6):
                app.add(f"用户喜欢第 {index} 种手冲咖啡", kind="semantic")
        finally:
            app.close()
        config = self._config("[retrieval]\ntop_k = 2\ntoken_budget = 600\n")
        code, out = run_cli(
            ["--db", self.db, "--config", config, "--json", "search", "用户 手冲咖啡"]
        )
        self.assertEqual(code, 0, out)
        items = parse_json(out)["data"]["items"]
        self.assertEqual(len(items), 2)


class DefaultLimitForwardingCase(_Case):
    """--limit 不传时不要传 limit：让 SDK 用自己的默认，而不是 CLI 再写一个。"""

    def test_trait_list_default_limit_not_forwarded(self) -> None:
        seen: dict = {}

        def fake_trait(self, **kwargs):  # noqa: ANN001
            seen.update(kwargs)
            return {"ok": True, "data": {"items": []}}

        with mock.patch.object(PMem, "trait", fake_trait):
            code, out = run_cli(["--db", self.db, "--json", "trait", "list"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("limit", seen, seen)
        self.assertEqual(seen.get("action"), "list")

    def test_net_entities_default_limit_not_forwarded(self) -> None:
        seen: dict = {}

        def fake_entities(self, **kwargs):  # noqa: ANN001
            seen.update(kwargs)
            return {"ok": True, "data": {"items": []}}

        with mock.patch.object(PMem, "entities", fake_entities):
            code, out = run_cli(["--db", self.db, "--json", "net", "entities"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("limit", seen, seen)


class DoctorCase(_Case):
    def test_doctor_json_has_integrity_errors_and_sqlite_version(self) -> None:
        code, out = run_cli(["--db", self.db, "--json", "doctor"])
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertIn("integrity_errors", data)
        self.assertIsInstance(data["integrity_errors"], list)
        self.assertEqual(data["integrity_errors"], [])
        self.assertEqual(data["sqlite_version"], sqlite3.sqlite_version)
        self.assertTrue(data["integrity"])

    def test_doctor_human_prints_sqlite_version(self) -> None:
        code, out = run_cli(["--db", self.db, "doctor"])
        self.assertEqual(code, 0, out)
        self.assertIn("SQLite", out)
        self.assertIn(sqlite3.sqlite_version, out)


class McpExportEventsCase(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.client = InProcessMcp(Path(self.db))

    def tearDown(self) -> None:
        self.client.close()
        super().tearDown()

    def test_export_tool_schema_has_include_events(self) -> None:
        tool = next(t for t in TOOLS if t["name"] == "memory_export")
        props = tool["inputSchema"]["properties"]
        self.assertIn("include_events", props)
        self.assertFalse(props["include_events"]["default"])
        self.assertEqual(props["redact"]["default"], True)

    def test_export_with_events_returns_event_rows(self) -> None:
        self.client.tool("memory_add", {"text": "MCP 导出事件测试记忆"})
        result = self.client.tool("memory_export", {"include_events": True})
        self.assertTrue(result["ok"], result)
        data = result["data"]
        self.assertGreaterEqual(data["events"], 1, data)
        self.assertGreaterEqual(data["count"], 1, data)
        self.assertIn('"type":"event"', data["jsonl"].replace(" ", ""), "导出的 JSONL 里没有 event 行")

    def test_export_defaults_to_no_events(self) -> None:
        self.client.tool("memory_add", {"text": "MCP 默认导出测试记忆"})
        result = self.client.tool("memory_export", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["events"], 0)


class BackupKindCase(_Case):
    def test_backup_run_accepts_kind(self) -> None:
        config = self._config(
            '[runtime]\nbackup_dir = "' + str(self.dir / "backups").replace("\\", "/") + '"\n'
        )
        code, out = run_cli(
            ["--db", self.db, "--config", config, "--json", "backup", "run", "--kind", "daily"]
        )
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertEqual(data["kind"], "daily")
        row = sqlite3.connect(self.db).execute(
            "SELECT kind FROM snapshot WHERE id=?", (data["snapshot_id"],)
        ).fetchone()
        self.assertEqual(row[0], "daily")


class DeleteVacuumCase(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.config = self._config(
            "[runtime]\n"
            f'backup_dir = "{(self.dir / "backups").as_posix()}"\n'
            f'maintenance_file = "{(self.dir / "maintenance.json").as_posix()}"\n'
        )

    def _mark_maintenance(self) -> None:
        from pmem.ops import maintenance

        app = PMem.open(self.db, config_path=self.config)
        try:
            maintenance.mark(app.db, config=app.config, minutes=5, reason="contract-test")
        finally:
            app.close()

    def test_vacuum_requires_hard(self) -> None:
        code, out = run_cli(
            ["--db", self.db, "--config", self.config, "--json", "delete",
             "01JCV0000000000000000000000", "--vacuum"]
        )
        self.assertEqual(code, 1)
        payload = parse_json(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "E_VALIDATION")

    def test_vacuum_rejected_inside_maintenance(self) -> None:
        self._mark_maintenance()
        code, out = run_cli(
            ["--db", self.db, "--config", self.config, "--json", "delete",
             "01JCV0000000000000000000000", "--hard", "--vacuum"]
        )
        self.assertEqual(code, 1)
        payload = parse_json(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "E_MAINTENANCE")

    @unittest.skipUnless(HAS_VACUUM, "等待 W4：Database.vacuum")
    def test_hard_delete_then_vacuum_runs(self) -> None:
        app = PMem.open(self.db)
        try:
            item = app.add("要被硬删并 VACUUM 的记忆", kind="semantic")["data"]
        finally:
            app.close()
        code, out = run_cli(
            ["--db", self.db, "--config", self.config, "--json", "delete", item["id"],
             "--hard", "--vacuum"]
        )
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertTrue(data["deleted"] >= 1)
        self.assertIn("vacuum", data)
        self.assertTrue(data["vacuum"]["ok"], data["vacuum"])


@unittest.skipUnless(HAS_VACUUM, "等待 W4：Database.vacuum")
class VacuumCase(_Case):
    def test_vacuum_json_reports_before_after(self) -> None:
        app = PMem.open(self.db)
        try:
            for index in range(20):
                app.add(f"vacuum 测试记忆 {index}", kind="semantic")
        finally:
            app.close()
        code, out = run_cli(["--db", self.db, "--json", "vacuum"])
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        for key in ("before", "after"):
            self.assertIn("pages", data[key])
            self.assertIn("bytes", data[key])
        self.assertGreaterEqual(data["freed_bytes"], 0)

    def test_vacuum_analyze_flag(self) -> None:
        code, out = run_cli(["--db", self.db, "--json", "vacuum", "--analyze"])
        self.assertEqual(code, 0, out)
        self.assertTrue(parse_json(out)["data"]["analyze"])


@unittest.skipUnless(HAS_RESTORE, "等待 W4：ops.restore.restore_backup")
class RestoreCase(_Case):
    def test_restore_missing_backup_reports_error(self) -> None:
        code, out = run_cli(
            [
                "--db",
                self.db,
                "--json",
                "restore",
                str(self.dir / "nope.db"),
                "--force",
            ]
        )
        self.assertEqual(code, 1)
        payload = parse_json(out)
        self.assertFalse(payload["ok"])
        self.assertIn("code", payload["error"])

    def test_restore_replaces_db_with_backup(self) -> None:
        from pmem.ops import backup

        config = self._config(
            "[runtime]\n"
            f'backup_dir = "{(self.dir / "backups").as_posix()}"\n'
            f'maintenance_file = "{(self.dir / "maintenance.json").as_posix()}"\n'
        )
        app = PMem.open(self.db, config_path=config)
        try:
            app.add("备份前就有的记忆", kind="semantic")
            snapshot = backup.run(app.db, config=app.config, kind="manual")
        finally:
            app.close()
        # 备份之后再多写一条：恢复后它应该消失（回到备份时刻）。
        app = PMem.open(self.db, config_path=config)
        try:
            app.add("备份之后才写的记忆", kind="semantic")
        finally:
            app.close()

        code, out = run_cli(
            ["--db", self.db, "--config", config, "--json", "restore", snapshot["path"], "--force"]
        )
        self.assertEqual(code, 0, out)
        payload = parse_json(out)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["db_path"], str(Path(self.db)))

        app = PMem.open(self.db)
        try:
            texts = {row["text_raw"] for row in app.db.conn.execute("SELECT text_raw FROM memory")}
        finally:
            app.close()
        self.assertIn("备份前就有的记忆", texts)
        self.assertNotIn("备份之后才写的记忆", texts)


class ReindexFtsCase(_Case):
    def _seed(self) -> None:
        from pmem.memory import journal

        app = PMem.open(self.db)
        try:
            for index in range(3):
                app.add(f"reindex 测试记忆 {index}", kind="semantic")
            journal.append(app.db, "reindex 测试原话流水一行")
        finally:
            app.close()

    def test_reindex_fts_rebuilds_memory_and_journal(self) -> None:
        self._seed()
        code, out = run_cli(["--db", self.db, "--json", "reindex", "fts"])
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        for name in ("memory_fts_tok", "memory_fts_tri"):
            item = data[name]
            self.assertTrue(item["rebuilt"], item)
            self.assertEqual(item["rows"], 3, item)
            self.assertEqual(item["docs"], 3, item)
        journal_item = data["journal_fts"]
        self.assertTrue(journal_item["rebuilt"], journal_item)
        self.assertEqual(journal_item["rows"], 1, journal_item)
        self.assertEqual(journal_item["docs"], 1, journal_item)
        # episode_fts：W1 的入口还没落库时跳过，落库后重建成功；两者都算通过。
        episode_item = data["episode_fts"]
        self.assertTrue(episode_item["rebuilt"] or episode_item.get("skipped"), episode_item)

    def test_reindex_fts_dry_run_does_not_rebuild(self) -> None:
        self._seed()
        code, out = run_cli(["--db", self.db, "--json", "reindex", "fts", "--dry-run"])
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertTrue(data["dry_run"])
        for name in ("memory_fts_tok", "memory_fts_tri", "journal_fts", "episode_fts"):
            self.assertFalse(data[name]["rebuilt"], data[name])
            self.assertIn("rows", data[name])
            self.assertIsNotNone(data[name]["docs"], data[name])

    def test_reindex_fts_repairs_memory_drift(self) -> None:
        """把 shadow 表的文档计数删掉（模拟 I21 的漂移），reindex 后要追平基表。"""
        self._seed()
        conn = sqlite3.connect(self.db)
        try:
            before = conn.execute("SELECT COUNT(*) FROM memory_fts_tok_docsize").fetchone()[0]
            self.assertEqual(before, 3)
            conn.execute("DELETE FROM memory_fts_tok_docsize")
            conn.commit()
            drift = conn.execute("SELECT COUNT(*) FROM memory_fts_tok_docsize").fetchone()[0]
            self.assertEqual(drift, 0)
        finally:
            conn.close()
        code, out = run_cli(["--db", self.db, "--json", "reindex", "fts"])
        self.assertEqual(code, 0, out)
        data = parse_json(out)["data"]
        self.assertEqual(data["memory_fts_tok"]["docs_before"], 0, data["memory_fts_tok"])
        self.assertEqual(data["memory_fts_tok"]["docs"], 3, data["memory_fts_tok"])
        conn = sqlite3.connect(self.db)
        try:
            fixed = conn.execute("SELECT COUNT(*) FROM memory_fts_tok_docsize").fetchone()[0]
            self.assertEqual(fixed, 3)
        finally:
            conn.close()


class ToolCountSchemaCase(unittest.TestCase):
    def test_mcp_tool_count_is_24(self) -> None:
        self.assertEqual(MCP_TOOL_COUNT, 24)
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("memory_export", names)
        self.assertEqual(len(names), MCP_TOOL_COUNT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
