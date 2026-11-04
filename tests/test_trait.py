"""M6：特质（强度 / established 门槛 / 证据重算 / 上限 / 时间线 / 画像）。

验收要点：

1. 跨 ≥3 天、strength/confidence 达标且无反证 → established；
2. 有反证或证据不足 → hypothesis，并生成新版本（旧版 deprecated）；
3. 每个 (subject, scope) 最多 30 条活跃特质，超限按 strength 保留；
4. 任何一次升降级都能从版本链 + 事件日志追溯；
5. profile 只读、按预算截断，不产生新结论。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

from pmem.base.clock import to_iso
from pmem.base.config import Config
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.trait import derive_trait, strength
from pmem.trait import history as trait_history

from .support import InProcessMcp, drop_dir, temp_dir

LIVE = ["我住在杭州", "我目前住在杭州", "我现在住在杭州"]
LIKE = ["我喜欢咖啡", "我特别喜欢咖啡", "我一直喜欢咖啡", "我挺喜欢咖啡", "我偏爱咖啡", "我很喜欢咖啡"]
WORK = ["我在字节工作", "我目前在字节工作", "我现在在字节工作"]


def stamp_for(days_ago: int) -> str:
    return to_iso(datetime.now().astimezone() - timedelta(days=days_ago))


def day_for(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def run_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(argv)
    return code, buf.getvalue()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("trait")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _seed_three_days(self) -> None:
        for index, offset in enumerate((3, 2, 1)):
            stamp = stamp_for(offset)
            for text in (LIVE[index], LIKE[index * 2], LIKE[index * 2 + 1], WORK[index]):
                self.store.add(text, created_at=stamp, session_id=f"s{offset}")
        result = self.app.sleep(day_key=day_for(1), resume=False)
        self.assertTrue(result["ok"], result)

    def _trait_rows(self) -> list:
        return self.db.conn.execute(
            "SELECT * FROM trait ORDER BY version"
        ).fetchall()

    def _pattern(self) -> object:
        return self.db.conn.execute(
            "SELECT * FROM pattern WHERE kind='frequency' LIMIT 1"
        ).fetchone()

    def _add_pattern(self, key: str, memory_ids: list[str], *, support: int = 3) -> str:
        from pmem.base.ids import ulid

        pattern_id = ulid()
        ts = to_iso(datetime.now())
        evidence = json.dumps(
            [{"memory_id": mid} for mid in sorted(memory_ids)], ensure_ascii=False
        )
        self.db.conn.execute(
            "INSERT INTO pattern(id, subject_id, kind, key, support, confidence, window_json, "
            "evidence_json, status, first_seen, last_seen, derivation, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pattern_id,
                "self",
                "frequency",
                key,
                support,
                0.8,
                "{}",
                evidence,
                "candidate",
                day_for(3),
                day_for(1),
                "test",
                ts,
            ),
        )
        return pattern_id

    def _add_trait(self, index: int, strength: float = 0.5) -> str:
        from pmem.base.ids import ulid

        trait_id = ulid()
        ts = to_iso(datetime.now())
        self.db.conn.execute(
            "INSERT INTO trait(id, subject_id, scope, type, key, value_json, strength, confidence, "
            "status, evidence_json, evidence_days, first_seen, last_support_at, version, derivation, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trait_id,
                "self",
                "self",
                "habit",
                f"manual:{index}",
                json.dumps({"label": f"manual {index}"}, ensure_ascii=False),
                strength,
                0.8,
                "hypothesis",
                json.dumps([{"memory_id": "x"}], ensure_ascii=False),
                1,
                day_for(1),
                ts,
                1,
                "manual:test",
                ts,
                ts,
            ),
        )
        return trait_id


class TestStrength(Case):
    def test_three_days_establishes(self) -> None:
        self._seed_three_days()
        rows = self._trait_rows()
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "established" for row in rows))
        self.assertTrue(all(row["evidence_days"] >= 3 for row in rows))
        self.assertTrue(all(row["strength"] >= 0.65 for row in rows))
        self.assertTrue(all(row["confidence"] >= 0.70 for row in rows))
        self.assertTrue(all(len(json.loads(row["evidence_json"])) >= 3 for row in rows))

    def test_formula_dimensions(self) -> None:
        low = strength(
            evidence_days=1, supports=1, contradictions=0, last_support_at=stamp_for(1)
        )
        high = strength(
            evidence_days=10, supports=10, contradictions=0, last_support_at=stamp_for(1)
        )
        contradicted = strength(
            evidence_days=10, supports=8, contradictions=2, last_support_at=stamp_for(1)
        )
        self.assertLess(low, high)
        self.assertLess(contradicted, high)
        self.assertGreaterEqual(low, 0.0)

    def test_two_day_evidence_is_hypothesis(self) -> None:
        ids = []
        for offset, texts in ((2, ["我住在城市2", "我目前住在城市2"]), (1, ["我住在城市1"])):
            for text in texts:
                ids.append(
                    self.store.add(
                        text, created_at=stamp_for(offset), session_id=f"s{offset}"
                    )["id"]
                )
        self._add_pattern("self|lives_in|城市", ids, support=2)
        stats = derive_trait(self.db, subject="self", day_key=day_for(1))
        self.assertEqual(stats["created"], 1)
        row = self.db.conn.execute("SELECT * FROM trait").fetchone()
        self.assertEqual(row["status"], "hypothesis")
        self.assertEqual(row["evidence_days"], 2)
        self.assertEqual(len(json.loads(row["evidence_json"])), 3)


class TestRecomputeAndDemotion(Case):
    def test_contradiction_demotes(self) -> None:
        self._seed_three_days()
        pattern = self._pattern()
        ids = [item["memory_id"] for item in json.loads(pattern["evidence_json"])]
        self.db.conn.execute("UPDATE memory SET status='contested' WHERE id=?", (ids[0],))
        stats = derive_trait(self.db, subject="self", day_key=day_for(1))
        self.assertGreaterEqual(stats["demoted"], 1)
        rows = self.db.conn.execute(
            "SELECT * FROM trait WHERE key LIKE 'pattern:frequency:%' ORDER BY version"
        ).fetchall()
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "deprecated")
        self.assertEqual(rows[-1]["status"], "hypothesis")
        self.assertLess(rows[-1]["strength"], rows[0]["strength"])

    def test_evidence_collapse_retires(self) -> None:
        self._seed_three_days()
        pattern = self._pattern()
        ids = [item["memory_id"] for item in json.loads(pattern["evidence_json"])]
        self.store.delete(ids[0], hard=True)
        stats = derive_trait(self.db, subject="self", day_key=day_for(1))
        self.assertGreaterEqual(stats["retired"], 1)
        row = self.db.conn.execute(
            "SELECT * FROM trait WHERE key LIKE 'pattern:frequency:%' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "deprecated")
        self.assertEqual(row["retired_reason"], "insufficient_evidence")

    def test_version_chain_and_timeline(self) -> None:
        self._seed_three_days()
        pattern = self._pattern()
        ids = [item["memory_id"] for item in json.loads(pattern["evidence_json"])]
        key = self.db.conn.execute(
            "SELECT key FROM trait WHERE key LIKE 'pattern:frequency:%' LIMIT 1"
        ).fetchone()["key"]
        self.db.conn.execute("UPDATE memory SET status='contested' WHERE id=?", (ids[0],))
        derive_trait(self.db, subject="self", day_key=day_for(1))
        versions = trait_history.versions(self.db, subject="self", ref=key)
        self.assertGreaterEqual(len(versions), 2)
        self.assertEqual(versions[-1]["version"], versions[-2]["version"] + 1)
        self.assertEqual(versions[-1]["prev_id"], versions[-2]["id"])
        timeline = trait_history.timeline(self.db, subject="self", ref=key)
        self.assertTrue(any(event["op"] in ("trait.version", "trait.demote") for event in timeline))


class TestCapacity(Case):
    def test_active_traits_capped_at_thirty(self) -> None:
        for index in range(35):
            self._add_trait(index, strength=0.3 + index / 100.0)
        stats = derive_trait(self.db, subject="self", day_key=day_for(1))
        self.assertEqual(stats["capacity_retired"], 5)
        self.assertEqual(stats["active"], 30)
        deprecated = self.db.conn.execute(
            "SELECT COUNT(*) c FROM trait WHERE status='deprecated' AND retired_reason='capacity'"
        ).fetchone()["c"]
        self.assertEqual(deprecated, 5)


class TestMetricsAndProfile(Case):
    def test_traceability_and_metrics(self) -> None:
        self._seed_three_days()
        metrics = trait_history.metrics(self.db)
        self.assertEqual(metrics["trace_rate"], 1.0)
        self.assertEqual(metrics["no_derivation"], 0)
        self.assertGreaterEqual(metrics["established"], 2)
        self.assertGreaterEqual(metrics["evidence_median"], 3)

    def test_churn_is_bounded_on_a_month_of_traits(self) -> None:
        for index in range(30):
            self._add_trait(index, strength=0.6)
        self.db.conn.execute(
            "UPDATE trait SET status='deprecated', retired_reason='superseded', updated_at=? "
            "WHERE key='manual:0'",
            (to_iso(datetime.now()),),
        )
        metrics = trait_history.metrics(self.db)
        self.assertLessEqual(metrics["churn"], 0.10)
        self.assertEqual(metrics["active"], 29)

    def test_profile_budget_and_order(self) -> None:
        self._seed_three_days()
        data = self.app.profile(budget=80)["data"]
        self.assertLessEqual(data["tokens"], 80)
        self.assertTrue(data["traits"])
        self.assertEqual(data["traits"][0]["status"], "established")
        self.assertTrue(data["rendered"])

    def test_cli_trait_and_profile(self) -> None:
        self._seed_three_days()
        code, out = run_cli(["--db", str(self.db_path), "trait", "list", "--json"])
        self.assertEqual(code, 0)
        items = json.loads(out)["data"]["items"]
        self.assertTrue(items)
        key = items[0]["key"]
        code, out = run_cli(
            ["--db", str(self.db_path), "trait", "show", key, "--json"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["data"]["versions"])
        code, out = run_cli(["--db", str(self.db_path), "profile", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("traits", json.loads(out)["data"])

    def test_mcp_profile(self) -> None:
        self._seed_three_days()
        mcp = InProcessMcp(self.db_path)
        try:
            result = mcp.tool("memory_profile", {"budget": 120})
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["data"]["traits"])
            tools = mcp.call("tools/list")["result"]["tools"]
            self.assertIn("memory_profile", [tool["name"] for tool in tools])
        finally:
            mcp.close()


if __name__ == "__main__":
    unittest.main()
