"""M7：反思（质量分 / 缺陷清单 / 周报 / 只读约束）。

验收要点：

1. 六维质量分可按公式复算，空库不冒充 100 分；
2. 缺陷清单是具体行（无实体、从未召回、重复、索引缺行、高等级过期、冲突、弱证据特质）；
3. 每条建议都有可执行命令与修复动作，可执行率可测；
4. 周报幂等：同周期同内容复用，内容变化追加；
5. G7：审计只读，反思只写 ``reflection`` + ``metrics``。
"""

from __future__ import annotations

import importlib
import inspect
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta
from pathlib import Path

from pmem.base.clock import to_iso
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.reflect import report, service
from pmem.reflect.audit import audit as run_audit

audit_module = importlib.import_module("pmem.reflect.audit")
report_module = importlib.import_module("pmem.reflect.report")
service_module = importlib.import_module("pmem.reflect.service")

from .support import InProcessMcp, drop_dir, temp_dir


def stamp_for(days_ago: int) -> str:
    return to_iso(datetime.now().astimezone() - timedelta(days=days_ago))


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("reflect")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    # -- 构造 ------------------------------------------------------------
    def _add(self, text: str, **kwargs) -> str:
        result = self.store.add(text, **kwargs)
        return result["id"]

    def _insert_pattern(self, key: str, evidence: list[str]) -> None:
        self.db.conn.execute(
            "INSERT INTO pattern(id, subject_id, kind, key, support, confidence, window_json, "
            "evidence_json, status, first_seen, last_seen, derivation, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"pat_{key}",
                "self",
                "frequency",
                key,
                2,
                0.8,
                "{}",
                json.dumps([{"memory_id": mid} for mid in evidence], ensure_ascii=False),
                "candidate",
                stamp_for(2),
                stamp_for(1),
                f"pattern:{key}",
                stamp_for(2),
            ),
        )

    def _insert_trait(
        self,
        key: str,
        evidence: list[str],
        *,
        status: str = "hypothesis",
        updated_days: int = 0,
    ) -> None:
        stamp = stamp_for(updated_days)
        self.db.conn.execute(
            "INSERT INTO trait(id, subject_id, scope, type, key, value_json, strength, confidence, "
            "status, evidence_json, evidence_days, first_seen, last_support_at, version, "
            "derivation, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"tr_{key}",
                "self",
                "self",
                "habit",
                key,
                json.dumps({"label": key}, ensure_ascii=False),
                0.7,
                0.8,
                status,
                json.dumps([{"memory_id": mid} for mid in evidence], ensure_ascii=False),
                3,
                stamp_for(3),
                stamp,
                1,
                f"pattern:{key}",
                stamp_for(3),
                stamp,
            ),
        )

    def _clone_memory(self, memory_id: str, new_id: str = "dup_clone") -> str:
        """造一条 text_norm 相同的活跃记忆（绕过 add 的去重，仅测试用）。"""
        cols = [
            row["name"]
            for row in self.db.conn.execute("PRAGMA table_info(memory)")
            if row["name"] not in ("seq", "id")
        ]
        joined = ",".join(cols)
        self.db.conn.execute(
            f"INSERT INTO memory(id,{joined}) SELECT ?,{joined} FROM memory WHERE id=?",
            (new_id, memory_id),
        )
        return new_id

    def _insert_conflict(self, *, severity: float = 0.9) -> None:
        self.db.conn.execute(
            "INSERT INTO conflict(id, subject_id, kind, left_tbl, left_id, right_tbl, right_id, "
            "slot, severity, detail_json, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cf_1",
                "self",
                "evidence",
                "memory",
                "a",
                "memory",
                "b",
                "lives_in",
                severity,
                "{}",
                "open",
                stamp_for(1),
            ),
        )

    # -- 质量分 ----------------------------------------------------------
    def test_empty_db_is_not_100(self) -> None:
        data = run_audit(self.db, subject="self")
        self.assertIsNone(data["quality"])
        self.assertTrue(data["insufficient_data"])
        self.assertEqual(data["defects"], [])
        self.assertIsNone(data["actionable_rate"])

    def test_quality_formula(self) -> None:
        m1 = self._add("我住在杭州", created_at=stamp_for(2))
        m2 = self._add("嗯嗯好的", created_at=stamp_for(2))
        m3 = self._clone_memory(m2, "dup_m2")
        self.app.net_backfill(memory_ids=[m1])

        self.db.conn.execute(
            "UPDATE memory SET use_count=1, last_used_at=? WHERE id=?", (stamp_for(0), m1)
        )
        self._insert_pattern("p1", [m1, m2])
        self._insert_trait("t1", [m1, m2])
        self._insert_conflict(severity=0.5)

        data = run_audit(self.db, subject="self")
        comps = data["components"]
        self.assertAlmostEqual(comps["traceability"]["value"], 1.0)
        self.assertAlmostEqual(comps["conflict_open"]["value"], 0.0)
        self.assertAlmostEqual(comps["coverage"]["value"], 1 / 3, places=4)
        self.assertAlmostEqual(comps["freshness"]["value"], 1.0)
        self.assertAlmostEqual(comps["recall_health"]["value"], 0.7843, places=4)
        self.assertAlmostEqual(comps["redundancy"]["value"], 2 / 3, places=4)
        self.assertEqual(data["weight_used"], 1.0)
        self.assertAlmostEqual(data["quality"], 61.2, places=1)
        codes = {item["code"]: item for item in data["defects"]}
        self.assertEqual(codes["duplicate_text"]["count"], 1)

    def test_fts_missing_detected(self) -> None:
        mid = self._add("索引检查")
        seq = self.db.conn.execute("SELECT seq FROM memory WHERE id=?", (mid,)).fetchone()["seq"]
        self.db.conn.execute("DELETE FROM memory_fts_tok WHERE rowid=?", (seq,))
        data = run_audit(self.db, subject="self")
        codes = {item["code"]: item for item in data["defects"]}
        self.assertEqual(codes["fts_missing"]["count"], 1)
        self.assertEqual(codes["fts_missing"]["severity"], "high")

    # -- 缺陷清单 --------------------------------------------------------
    def test_defect_catalog(self) -> None:
        m1 = self._add("随便记一条", created_at=stamp_for(2))
        m2 = self._add("再记一条", created_at=stamp_for(2))
        m3 = self._add("第三条", created_at=stamp_for(100))
        m4 = self._add("置顶但不可信", created_at=stamp_for(2), pinned=True)
        self.db.conn.execute("UPDATE memory SET confidence=0.3 WHERE id=?", (m4,))
        self.db.conn.execute("UPDATE memory SET cluster_id='c1' WHERE id IN (?,?)", (m1, m2))
        self._insert_trait("weak", [m1], status="established", updated_days=0)
        self._insert_conflict(severity=0.9)

        # 给 m3 一个 A 级但 100 天没被用过的标签
        self.db.conn.execute(
            "INSERT INTO target_grade(target_tbl, target_id, grade, rationale, evidence_json, "
            "assigned_by, created_at, updated_at) VALUES('memory',?,?,?,?,?,?,?)",
            (m3, "A", "test", "[]", "ai", stamp_for(100), stamp_for(100)),
        )

        data = run_audit(self.db, subject="self")
        codes = {item["code"]: item for item in data["defects"]}
        self.assertEqual(codes["no_entity"]["count"], 4)
        self.assertEqual(codes["never_recalled"]["count"], 4)
        self.assertEqual(codes["open_conflict_high"]["count"], 1)
        self.assertEqual(codes["trait_under_evidenced"]["count"], 1)
        self.assertEqual(codes["isolated_cluster"]["count"], 1)
        self.assertEqual(codes["low_confidence_pinned"]["count"], 1)
        self.assertEqual(codes["stale_high_grade"]["count"], 1)
        self.assertIn("vector_gap", codes)

    def test_no_entity_not_flagged_when_anchored(self) -> None:
        m1 = self._add("我住在杭州")
        self.app.net_backfill(memory_ids=[m1])
        data = run_audit(self.db, subject="self")
        codes = {item["code"] for item in data["defects"]}
        self.assertNotIn("no_entity", codes)

    # -- 建议 ------------------------------------------------------------
    def test_suggestions_are_actionable(self) -> None:
        for text in ("随便一条", "再来一条", "第三条"):
            self._add(text, created_at=stamp_for(2))
        data = run_audit(self.db, subject="self")
        self.assertTrue(data["suggestions"])
        self.assertEqual(data["actionable_rate"], 1.0)
        for item in data["suggestions"]:
            self.assertTrue(item["command"].startswith("pmem reflect --only "))
            self.assertTrue(item["fix"].startswith("pmem "))
            self.assertIn(item["effort"], ("low", "medium", "high"))
            self.assertTrue(item["why"])

        only = service.reflect(self.db, subject="self", period=f"day:{date.today()}", only="no_entity")
        self.assertEqual([item["code"] for item in only["defects"]], ["no_entity"])
        self.assertEqual(only["defects"][0]["count"], 3)

    # -- 周报 ------------------------------------------------------------
    def test_weekly_report_idempotent(self) -> None:
        key = report.week_key()
        start, _ = report.week_bounds(key)
        when = to_iso(datetime.combine(start, time(12)).astimezone())
        self._add("周报测试一", created_at=when)
        self._add("周报测试二", created_at=when)

        payload = report.compose(self.db, subject="self", key=key)
        self.assertEqual(payload["period"], f"week:{key}")
        self.assertEqual(payload["metrics"]["memories_created"], 2)
        self.assertIsNotNone(payload["quality"])

        first = report.write(self.db, subject="self", key=key)
        self.assertTrue(first["created"])
        again = report.write(self.db, subject="self", key=key)
        self.assertFalse(again["created"])
        self.assertEqual(first["id"], again["id"])
        rows = self.db.conn.execute(
            "SELECT COUNT(*) c FROM reflection WHERE period=?", (f"week:{key}",)
        ).fetchone()["c"]
        self.assertEqual(rows, 1)

        self._add("周报测试三", created_at=when)
        changed = report.write(self.db, subject="self", key=key)
        self.assertTrue(changed["created"])
        self.assertNotEqual(changed["id"], first["id"])

        metrics = self.db.conn.execute(
            "SELECT COUNT(*) c FROM metrics WHERE name='reflect.quality' AND day_key=?",
            (report.week_bounds(key)[1].isoformat(),),
        ).fetchone()["c"]
        self.assertEqual(metrics, 1)

    # -- 只读与 G7 -------------------------------------------------------
    def test_audit_is_read_only(self) -> None:
        self._add("只读检查", created_at=stamp_for(1))
        tables = ("memory", "trait", "pattern", "relation", "event_log", "reflection")
        before = {
            table: self.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in tables
        }
        run_audit(self.db, subject="self")
        after = {
            table: self.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in tables
        }
        self.assertEqual(before, after)

        for module in (audit_module, service_module, report_module):
            source = inspect.getsource(module)
            self.assertNotIn("memory.store", source)
            self.assertNotIn("from ..memory import store", source)

    def test_persist_only_writes_reflection(self) -> None:
        self._add("持久化检查", created_at=stamp_for(1))
        tables = ("memory", "trait", "pattern", "relation", "event_log")
        before = {
            table: self.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in tables
        }
        result = service.reflect(
            self.db, subject="self", period=f"day:{date.today()}", persist=True
        )
        self.assertTrue(result["persisted"])
        after = {
            table: self.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in tables
        }
        self.assertEqual(before, after)

    # -- 接口 ------------------------------------------------------------
    def test_mcp_tool(self) -> None:
        mcp = InProcessMcp(self.db_path)
        try:
            tools = mcp.call("tools/list")["result"]["tools"]
            names = {tool["name"] for tool in tools}
            self.assertIn("memory_reflect", names)
            self.assertEqual(len(tools), 30)

            before = self.db.conn.execute("SELECT COUNT(*) c FROM reflection").fetchone()["c"]
            data = mcp.tool("memory_reflect", {"period": f"day:{date.today()}"})
            self.assertTrue(data["ok"], data)
            self.assertFalse(data["data"]["persisted"])
            after = self.db.conn.execute("SELECT COUNT(*) c FROM reflection").fetchone()["c"]
            self.assertEqual(before, after)

            saved = mcp.tool(
                "memory_reflect", {"period": f"day:{date.today()}", "persist": True}
            )
            self.assertTrue(saved["ok"], saved)
            self.assertTrue(saved["data"]["persisted"])
        finally:
            mcp.close()

    def test_invalid_period(self) -> None:
        result = self.app.reflect(period="week:bad")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_VALIDATION")

    def test_cli(self) -> None:
        self._add("命令行反思", created_at=stamp_for(1))
        code, out = run_cli(["--db", str(self.db_path), "reflect", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertIn("quality", payload["data"])

        code, out = run_cli(["--db", str(self.db_path), "reflect", "--history"])
        self.assertEqual(code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
