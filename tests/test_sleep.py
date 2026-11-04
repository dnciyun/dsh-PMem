"""M4：睡眠引擎（海马体 replay）。

验收四件事（PLAN.md M4 Gate）：

1. 8 阶段能跑通、产出规律/经验/特质/反思/快照；
2. 重跑幂等（派生表新增 = 0）；
3. 任一阶段"崩溃"后能从 checkpoint 续跑，且结果与一次跑完一致；
4. dry-run 全流程回滚，只留报告。

外加一条性能门槛：1k 新记忆 < 60 s。
"""

from __future__ import annotations

import io
import json
import shutil
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from pmem.base.clock import to_iso
from pmem.base.config import Config
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.sleep import orchestrator, patterns, snapshot, traits

from .support import InProcessMcp, drop_dir, temp_dir

LIVE = ["我住在杭州", "我目前住在杭州", "我现在住在杭州"]
LIKE = [
    "我喜欢咖啡",
    "我特别喜欢咖啡",
    "我一直喜欢咖啡",
    "我挺喜欢咖啡",
    "我偏爱咖啡",
    "我很喜欢咖啡",
]
EXPERIENCE = {
    "situation": "给陌生项目做技术选型",
    "action": "先跑基线再优化",
    "outcome": "success",
    "lesson": "先建立可复现基线",
}

DERIVED = (
    "entity",
    "entity_mention",
    "relation",
    "conflict",
    "pattern",
    "experience",
    "trait",
    "reflection",
    "snapshot",
    "metrics",
)


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
        self._dir = temp_dir("sleep")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _seed(self, app: PMem | None = None, days=(3, 2, 1)) -> list[str]:
        target = app or self.app
        ids: list[str] = []
        for index, offset in enumerate(days):
            stamp = stamp_for(offset)
            ids.append(
                target.store.add(LIVE[index % len(LIVE)], created_at=stamp, session_id=f"s{offset}")["id"]
            )
            ids.append(
                target.store.add(LIKE[index * 2 % len(LIKE)], created_at=stamp, session_id=f"s{offset}")["id"]
            )
            ids.append(
                target.store.add(
                    LIKE[index * 2 + 1 % len(LIKE)], created_at=stamp, session_id=f"s{offset}"
                )["id"]
            )
            ids.append(
                target.store.add("我在字节工作", created_at=stamp, session_id=f"s{offset}")["id"]
            )
            ids.append(
                target.store.add(
                    f"给陌生项目做技术选型时先跑基线再优化，否则无法判断收益（第{index}次）",
                    kind="experience",
                    extra=EXPERIENCE,
                    created_at=stamp,
                    session_id=f"s{offset}",
                )["id"]
            )
        return ids

    def _counts(self, app: PMem | None = None) -> dict[str, int]:
        target = app or self.app
        return {
            table: target.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in DERIVED
        }

    def _run(self, **kwargs: object) -> dict:
        result = self.app.sleep(day_key=day_for(1), resume=False, **kwargs)
        self.assertTrue(result["ok"], result)
        return result["data"]


class TestFullRun(Case):
    def test_all_eight_stages_and_report(self) -> None:
        self._seed()
        data = self._run()
        self.assertEqual(data["status"], "ok")
        self.assertEqual([stage["name"] for stage in data["stages"]], list(orchestrator.STAGES))
        self.assertTrue(all(stage["ok"] for stage in data["stages"]))
        report = Path(data["report_path"])
        self.assertTrue(report.exists())
        text = report.read_text(encoding="utf-8")
        self.assertIn(day_for(1), text)
        self.assertIn("睡眠报告", text)
        self.assertTrue(report.with_suffix(".json").exists())

        status = self.app.sleep_status(day_key=day_for(1))["data"]
        self.assertEqual(status["run"]["status"], "ok")
        self.assertFalse(status["resumable"])
        self.assertEqual(len(status["stages"]), 8)

    def test_derived_structures(self) -> None:
        self._seed()
        self._run()
        kinds = {
            row["kind"]
            for row in self.db.conn.execute("SELECT kind FROM pattern").fetchall()
        }
        self.assertIn("frequency", kinds)
        self.assertIn("preference", kinds)

        experiences = self.db.conn.execute("SELECT * FROM experience").fetchall()
        self.assertEqual(len(experiences), 1)
        self.assertEqual(experiences[0]["outcome"], "success")

        active_traits = self.db.conn.execute(
            "SELECT * FROM trait WHERE status IN ('hypothesis','established')"
        ).fetchall()
        self.assertGreaterEqual(len(active_traits), 3)
        self.assertTrue(all(row["evidence_days"] >= 1 for row in active_traits))

        reflection = self.db.conn.execute("SELECT * FROM reflection").fetchone()
        self.assertIsNotNone(reflection)
        questions = json.loads(reflection["questions_json"])
        self.assertEqual(len(questions), 7)
        self.assertIn("what_to_ask", questions)

        snap = self.db.conn.execute("SELECT * FROM snapshot").fetchone()
        self.assertIsNotNone(snap)
        self.assertTrue(Path(snap["path"]).exists())
        self.assertEqual(len(snap["sha256"]), 64)

    def test_conflict_is_marked_not_resolved(self) -> None:
        stamp = stamp_for(1)
        first = self.store.add("我住在杭州", created_at=stamp, session_id="c1")["id"]
        second = self.store.add("我住在上海", created_at=stamp, session_id="c2")["id"]
        data = self._run()
        s4 = next(stage for stage in data["stages"] if stage["name"] == "s4_conflicts")
        self.assertEqual(s4["stats"]["created"], 1)
        rows = self.db.conn.execute(
            "SELECT id, status FROM memory WHERE id IN (?,?)", (first, second)
        ).fetchall()
        self.assertEqual({row["status"] for row in rows}, {"contested"})
        conflict = self.db.conn.execute("SELECT * FROM conflict").fetchone()
        self.assertEqual(conflict["status"], "open")

    def test_dedup_merges_near_duplicates(self) -> None:
        stamp = stamp_for(1)
        first = self.store.add("PMem 用 SQLite 做存储", created_at=stamp, session_id="d1")["id"]
        second = self.store.add("PMem 用 SQLite 做存储。", created_at=stamp, session_id="d2")["id"]
        data = self._run()
        s2 = next(stage for stage in data["stages"] if stage["name"] == "s2_dedup")
        self.assertGreaterEqual(s2["stats"]["merged"], 1)
        rows = self.db.conn.execute(
            "SELECT id, status, canonical_id FROM memory WHERE id IN (?,?)", (first, second)
        ).fetchall()
        self.assertTrue(any(row["canonical_id"] for row in rows))
        self.assertTrue(any(row["status"] == "archived" for row in rows))


class TestIdempotency(Case):
    def test_rerun_adds_no_derived_rows(self) -> None:
        self._seed()
        self._run()
        before = self._counts()
        again = self._run()
        after = self._counts()
        self.assertEqual({key: after[key] - before[key] for key in before}, {key: 0 for key in before})
        s5 = next(stage for stage in again["stages"] if stage["name"] == "s5_patterns")
        self.assertEqual(s5["stats"]["patterns_created"], 0)
        s6 = next(stage for stage in again["stages"] if stage["name"] == "s6_traits")
        self.assertEqual(s6["stats"]["created"], 0)
        s7 = next(stage for stage in again["stages"] if stage["name"] == "s7_reflect")
        self.assertFalse(s7["stats"]["reflection_created"])

    def test_dry_run_rolls_back_everything(self) -> None:
        self._seed()
        before = self._counts()
        sleep_runs = self.db.conn.execute("SELECT COUNT(*) c FROM sleep_run").fetchone()["c"]
        data = self._run(dry_run=True)
        after = self._counts()
        self.assertEqual(data["status"], "dry_run")
        self.assertTrue(data["rolled_back"])
        self.assertEqual({key: after[key] - before[key] for key in before}, {key: 0 for key in before})
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM sleep_run").fetchone()["c"], sleep_runs
        )
        self.assertTrue(Path(data["report_path"]).exists())
        last = next(stage for stage in data["stages"] if stage["name"] == "s8_snapshot")
        self.assertTrue(last["skipped"])


class TestResume(Case):
    def test_every_stage_is_resumable(self) -> None:
        """8 个阶段逐个"崩溃"，续跑结果都要和一次跑完一致。"""
        baseline_dir = temp_dir("sleep-base")
        baseline: dict[str, int] | None = None
        try:
            base_app = PMem.open(Path(baseline_dir) / "pmem.db")
            try:
                self._seed(base_app)
                self.assertEqual(
                    base_app.sleep(day_key=day_for(1), resume=False)["data"]["status"], "ok"
                )
                baseline = self._counts(base_app)
            finally:
                base_app.close()
        finally:
            drop_dir(baseline_dir)

        for stage in orchestrator.STAGES:
            case_dir = temp_dir(f"sleep-{stage}")
            try:
                app = PMem.open(Path(case_dir) / "pmem.db")
                try:
                    self._seed(app)
                    first = app.sleep(day_key=day_for(1), resume=False, stop_at=stage)
                    self.assertEqual(first["data"]["status"], "partial", stage)
                    resumed = app.sleep(day_key=day_for(1), resume=True)["data"]
                    self.assertEqual(resumed["status"], "ok", stage)
                    self.assertEqual(self._counts(app), baseline, stage)
                finally:
                    app.close()
            finally:
                drop_dir(case_dir)

    def test_mid_stage_exception_rolls_back_and_resumes(self) -> None:
        self._seed()
        baseline_dir = temp_dir("sleep-crash-base")
        try:
            base_app = PMem.open(Path(baseline_dir) / "pmem.db")
            try:
                self._seed(base_app)
                base_app.sleep(day_key=day_for(1), resume=False)
                baseline = self._counts(base_app)
            finally:
                base_app.close()
        finally:
            drop_dir(baseline_dir)

        def boom(ctx: object) -> dict:
            raise RuntimeError("injected crash")

        with mock.patch.dict(orchestrator.STAGE_FNS, {"s5_patterns": boom}):
            crashed = self.app.sleep(day_key=day_for(1), resume=False)["data"]
        self.assertEqual(crashed["status"], "partial")
        self.assertIn("injected crash", crashed["error"] or "")
        status = self.app.sleep_status(day_key=day_for(1))["data"]
        self.assertTrue(status["resumable"])
        self.assertNotIn("s5_patterns", status["stages"])
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM pattern").fetchone()["c"], 0
        )

        resumed = self.app.sleep(day_key=day_for(1), resume=True)["data"]
        self.assertEqual(resumed["status"], "ok")
        self.assertEqual(self._counts(), baseline)

    def test_stop_then_resume_matches_full_run(self) -> None:
        self._seed()
        partial = self._run(stop_at="s4_conflicts")
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(len(partial["stages"]), 4)
        status = self.app.sleep_status(day_key=day_for(1))["data"]
        self.assertTrue(status["resumable"])

        resumed = self.app.sleep(day_key=day_for(1), resume=True)["data"]
        self.assertEqual(resumed["status"], "ok")
        self.assertTrue(resumed["resumed"])
        skipped = [stage["name"] for stage in resumed["stages"] if stage["skipped"]]
        self.assertEqual(skipped, ["s1_collect", "s2_dedup", "s3_relations", "s4_conflicts"])
        resumed_counts = self._counts()

        other_dir = temp_dir("sleep-full")
        other_path = Path(other_dir) / "pmem.db"
        try:
            other = PMem.open(other_path)
            try:
                self._seed(other)
                full = other.sleep(day_key=day_for(1), resume=False)["data"]
                self.assertEqual(full["status"], "ok")
                full_counts = self._counts(other)
            finally:
                other.close()
        finally:
            drop_dir(other_dir)
        self.assertEqual(resumed_counts, full_counts)

    def test_partial_then_resume_does_not_duplicate(self) -> None:
        self._seed()
        self._run(stop_at="s5_patterns")
        patterns_before = self.db.conn.execute("SELECT COUNT(*) c FROM pattern").fetchone()["c"]
        resumed = self.app.sleep(day_key=day_for(1), resume=True)["data"]
        self.assertEqual(resumed["status"], "ok")
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM pattern").fetchone()["c"],
            patterns_before,
        )
        skipped = [stage["name"] for stage in resumed["stages"] if stage["skipped"]]
        self.assertEqual(
            skipped,
            ["s1_collect", "s2_dedup", "s3_relations", "s4_conflicts", "s5_patterns"],
        )


class TestUncertainty(Case):
    def test_incomplete_experience_is_counted_not_written(self) -> None:
        self.store.add(
            "给陌生项目做技术选型时先跑基线再优化",
            kind="experience",
            extra={"situation": "选型"},
            created_at=stamp_for(1),
            session_id="x1",
        )
        self.store.add(
            "给陌生项目做技术选型时先跑基线再优化（第二版）",
            kind="experience",
            extra={"situation": "选型", "action": "先跑基线", "outcome": "success"},
            created_at=stamp_for(1),
            session_id="x2",
        )
        data = self._run()
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM experience").fetchone()["c"], 0
        )
        stage = next(stage for stage in data["stages"] if stage["name"] == "s5_patterns")
        self.assertEqual(stage["stats"]["experience_incomplete"], 1)
        self.assertEqual(stage["stats"]["experience_invalid"], 1)

    def test_trait_versions_instead_of_overwriting(self) -> None:
        self._seed()
        self._run()
        trait = self.db.conn.execute(
            "SELECT * FROM trait WHERE key LIKE 'pattern:preference:%' "
            "AND status IN ('hypothesis','established')"
        ).fetchone()
        self.assertIsNotNone(trait)
        pattern = self.db.conn.execute(
            "SELECT * FROM pattern WHERE kind='preference'"
        ).fetchone()
        self.db.conn.execute(
            "UPDATE pattern SET support=support+2 WHERE id=?", (pattern["id"],)
        )
        traits.derive(self.db, subject="self", day_key=day_for(1))
        rows = self.db.conn.execute(
            "SELECT * FROM trait WHERE key=? ORDER BY version", (trait["key"],)
        ).fetchall()
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[-1]["version"], rows[-2]["version"] + 1)
        self.assertEqual(rows[-1]["prev_id"], rows[-2]["id"])
        self.assertEqual(rows[-2]["status"], "deprecated")
        self.assertEqual(rows[-1]["status"], "established")

    def test_pattern_goes_stale_and_trait_retires(self) -> None:
        self._seed()
        self._run()
        self.db.conn.execute(
            "UPDATE pattern SET last_seen=? WHERE kind='frequency'",
            ((date.today() - timedelta(days=90)).isoformat(),),
        )
        stale = patterns.mark_stale(self.db, subject="self", stale_days=30)
        self.assertGreaterEqual(stale, 1)
        retired = traits.derive(self.db, subject="self", day_key=day_for(1))
        self.assertGreaterEqual(retired["retired"], 1)
        statuses = {
            row["status"]
            for row in self.db.conn.execute(
                "SELECT status FROM trait WHERE key LIKE 'pattern:frequency:%'"
            ).fetchall()
        }
        self.assertIn("deprecated", statuses)


class TestSnapshot(Case):
    def test_snapshot_is_verified_and_reused(self) -> None:
        self.store.add("PMem 用 SQLite 做存储", created_at=stamp_for(1), session_id="s")
        first = snapshot.create(
            self.db, day_key=day_for(1), out_dir=Path(self._dir) / "backups"
        )
        self.assertTrue(first["verified"])
        self.assertFalse(first["skipped"])
        self.assertEqual(first["memory_count"], 1)
        second = snapshot.create(
            self.db, day_key=day_for(1), out_dir=Path(self._dir) / "backups"
        )
        self.assertTrue(second["skipped"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"], 1
        )

    def test_snapshot_can_be_restored_under_a_minute(self) -> None:
        from pmem.base.db import Database

        self.store.add("PMem 用 SQLite 做存储", created_at=stamp_for(1), session_id="s")
        data = snapshot.create(
            self.db, day_key=day_for(1), out_dir=Path(self._dir) / "backups"
        )
        restored = Path(self._dir) / "restored.db"
        started = time.perf_counter()
        shutil.copy2(data["path"], restored)
        copy_elapsed = time.perf_counter() - started
        db = Database(restored)
        try:
            self.assertTrue(db.integrity_ok())
            self.assertEqual(
                db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"],
                data["memory_count"],
            )
            self.assertEqual(db.meta("schema_version"), self.db.meta("schema_version"))
        finally:
            db.close()
        self.assertLess(copy_elapsed, 60.0)


class TestCatchup(Case):
    def test_catchup_runs_missing_days(self) -> None:
        self.store.add("我住在杭州", created_at=stamp_for(2), session_id="c2")
        self.store.add("我住在上海", created_at=stamp_for(1), session_id="c1")
        data = self.app.sleep(catchup=3)["data"]
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["ran"], 2)
        days = {
            row["day_key"]
            for row in self.db.conn.execute(
                "SELECT day_key FROM sleep_run WHERE status='ok'"
            ).fetchall()
        }
        self.assertEqual(days, {day_for(2), day_for(1)})

    def test_catchup_skips_covered_days(self) -> None:
        self.store.add("我住在杭州", created_at=stamp_for(1), session_id="c1")
        self.app.sleep(day_key=day_for(1))
        data = self.app.sleep(catchup=2)["data"]
        self.assertEqual(data["ran"], 0)
        self.assertGreaterEqual(data["skipped"], 1)


class TestInterfaces(Case):
    def test_mcp_memory_sleep(self) -> None:
        self._seed()
        mcp = InProcessMcp(self.db_path)
        try:
            result = mcp.tool("memory_sleep", {"day_key": day_for(1), "dry_run": True})
            self.assertTrue(result["ok"], result)
            data = result["data"]
            self.assertEqual(data["status"], "dry_run")
            self.assertEqual(len(data["stages"]), 8)
            tools = mcp.call("tools/list")["result"]["tools"]
            self.assertIn("memory_sleep", [tool["name"] for tool in tools])
        finally:
            mcp.close()

    def test_cli_status_and_report(self) -> None:
        self._seed()
        self.app.sleep(day_key=day_for(1))
        code, out = run_cli(["--db", str(self.db_path), "sleep", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["run"]["status"], "ok")

        code, out = run_cli(
            ["--db", str(self.db_path), "sleep", "report", "--day", day_for(1)]
        )
        self.assertEqual(code, 0)
        self.assertIn("睡眠报告", out)
        self.assertIn(day_for(1), out)

    def test_cli_run_json(self) -> None:
        self._seed()
        code, out = run_cli(
            ["--db", str(self.db_path), "sleep", "run", "--day", day_for(1), "--json"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["status"], "ok")


class TestBudgets(Case):
    def test_one_thousand_memories_under_sixty_seconds(self) -> None:
        cfg = Config({"net": {"auto_extract": False}})
        app = PMem.open(self.db_path, config=cfg)
        try:
            stamp = stamp_for(1)
            for index in range(1000):
                app.store.add(
                    f"第 {index} 条：项目 {index % 37} 的会议记录，讨论了存储与检索的取舍",
                    created_at=stamp,
                    session_id=f"perf-{index % 11}",
                )
            started = time.perf_counter()
            data = app.sleep(day_key=day_for(1), resume=False)["data"]
            elapsed = time.perf_counter() - started
            self.assertEqual(data["status"], "ok")
            self.assertLess(elapsed, 60.0)
            self.assertGreaterEqual(data["stages"][0]["stats"]["memories"], 1000)
            self.assertGreaterEqual(data["stages"][0]["stats"]["work"], 1000)
        finally:
            app.close()


if __name__ == "__main__":
    unittest.main()
