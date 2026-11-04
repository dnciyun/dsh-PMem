"""M5：进化引擎（复核 / 经验复用 / 冲突升级 / 参数自调）。

验四件事：

1. 人工标签能把候选变成 confirmed/rejected，且改判只追加不覆盖；
2. 经验卡能被情境匹配、复用，并按反馈降权/退役；
3. 冲突只升级、只自动裁决"一方已被替代"的情况；
4. 调参没有足够标注不动，有标注也只在 nDCG 提升时生效，且可回滚。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

from pmem.base.clock import now, to_iso
from pmem.base.config import Config
from pmem.evolve import conflicts as conflicts_mod
from pmem.evolve import experience as experience_mod
from pmem.evolve import review as review_mod
from pmem.evolve import runner as evolve_runner
from pmem.evolve import tuning as tuning_mod
from pmem.gate import cli
from pmem.gate.sdk import PMem
from pmem.memory import conflict as conflict_mod

from .support import InProcessMcp, drop_dir, temp_dir

LIVE = ["我住在杭州", "我目前住在杭州", "我现在住在杭州"]
WORK = ["我在字节工作", "我目前在字节工作", "我现在在字节工作"]
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
        self._dir = temp_dir("evolve")
        self.db_path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.db_path)
        self.db = self.app.db
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def _seed(self) -> None:
        for index, offset in enumerate((3, 2, 1)):
            stamp = stamp_for(offset)
            self.store.add(LIVE[index], created_at=stamp, session_id=f"s{offset}")
            self.store.add(LIKE[index * 2], created_at=stamp, session_id=f"s{offset}")
            self.store.add(LIKE[index * 2 + 1], created_at=stamp, session_id=f"s{offset}")
            self.store.add(WORK[index], created_at=stamp, session_id=f"s{offset}")
            self.store.add(
                f"给陌生项目做技术选型时先跑基线再优化（第{index}次）",
                kind="experience",
                extra=EXPERIENCE,
                created_at=stamp,
                session_id=f"s{offset}",
            )
        result = self.app.sleep(day_key=day_for(1), resume=False)
        self.assertTrue(result["ok"], result)

    def _pattern(self, kind: str = "frequency") -> str:
        row = self.db.conn.execute(
            "SELECT id FROM pattern WHERE kind=? ORDER BY support DESC LIMIT 1", (kind,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row["id"]

    def _experience(self) -> str:
        row = self.db.conn.execute("SELECT id FROM experience LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        return row["id"]

    def _conflict(self) -> str:
        self.store.add("我住在杭州", created_at=stamp_for(1), session_id="c1")
        self.store.add("我住在上海", created_at=stamp_for(1), session_id="c2")
        stats = conflict_mod.run(self.db)
        self.assertEqual(stats["created"], 1)
        row = self.db.conn.execute("SELECT id FROM conflict LIMIT 1").fetchone()
        return row["id"]


class TestReview(Case):
    def test_correct_and_wrong_labels_change_status(self) -> None:
        self._seed()
        good = self._pattern("frequency")
        bad = self.db.conn.execute(
            "SELECT id FROM pattern WHERE id<>? LIMIT 1", (good,)
        ).fetchone()["id"]
        review_mod.label(self.db, target_tbl="pattern", target_id=good, verdict="correct")
        review_mod.label(self.db, target_tbl="pattern", target_id=bad, verdict="wrong")
        applied = review_mod.apply_labels(self.db)
        self.assertEqual(applied["promoted"], 1)
        self.assertEqual(applied["rejected"], 1)
        statuses = {
            row["id"]: row["status"]
            for row in self.db.conn.execute(
                "SELECT id, status FROM pattern WHERE id IN (?,?)", (good, bad)
            ).fetchall()
        }
        self.assertEqual(statuses[good], "confirmed")
        self.assertEqual(statuses[bad], "rejected")
        stats = review_mod.precision(self.db, target_tbl="pattern")
        self.assertEqual(stats["precision"], 0.5)

    def test_relabel_uses_latest_and_keeps_history(self) -> None:
        self._seed()
        pattern = self._pattern("frequency")
        review_mod.label(self.db, target_tbl="pattern", target_id=pattern, verdict="wrong")
        review_mod.label(self.db, target_tbl="pattern", target_id=pattern, verdict="correct")
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM evolve_label").fetchone()["c"], 2
        )
        review_mod.apply_labels(self.db)
        status = self.db.conn.execute(
            "SELECT status FROM pattern WHERE id=?", (pattern,)
        ).fetchone()["status"]
        self.assertEqual(status, "confirmed")
        self.assertEqual(review_mod.precision(self.db)["correct"], 1)

    def test_queue_excludes_labeled_and_renders(self) -> None:
        self._seed()
        pattern = self._pattern("frequency")
        queue = review_mod.review_queue(self.db, target_tbl="pattern")
        self.assertGreaterEqual(len(queue), 3)
        review_mod.label(self.db, target_tbl="pattern", target_id=pattern, verdict="unsure")
        queue_after = review_mod.review_queue(self.db, target_tbl="pattern")
        self.assertNotIn(pattern, {item["id"] for item in queue_after})
        rendered = review_mod.render_queue(queue_after)
        self.assertIn("pmem evolve label", rendered)

    def test_bad_target_or_verdict(self) -> None:
        with self.assertRaises(KeyError):
            review_mod.label(self.db, target_tbl="pattern", target_id="nope", verdict="correct")
        with self.assertRaises(ValueError):
            review_mod.label(self.db, target_tbl="pattern", target_id="nope", verdict="maybe")

    def test_wrong_trait_label_deprecates(self) -> None:
        self._seed()
        trait = self.db.conn.execute(
            "SELECT id FROM trait WHERE status IN ('hypothesis','established') LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(trait)
        review_mod.label(self.db, target_tbl="trait", target_id=trait["id"], verdict="wrong")
        applied = review_mod.apply_labels(self.db)
        self.assertEqual(applied["deprecated"], 1)
        status = self.db.conn.execute(
            "SELECT status FROM trait WHERE id=?", (trait["id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "deprecated")


class TestExperience(Case):
    def test_match_exact_and_unrelated(self) -> None:
        self._seed()
        matched = experience_mod.match(
            self.db, situation="给陌生项目做技术选型", threshold=0.5
        )
        self.assertEqual(len(matched), 1)
        self.assertGreaterEqual(matched[0]["similarity"], 0.9)
        self.assertEqual(matched[0]["outcome"], "success")
        self.assertEqual(experience_mod.match(self.db, situation="今晚吃什么"), [])

    def test_reuse_and_success_rate(self) -> None:
        self._seed()
        experience_id = self._experience()
        reused = experience_mod.reuse(self.db, experience_id=experience_id)
        self.assertEqual(reused["reuse_count"], 1)
        self.assertEqual(experience_mod.success_rate(self.db)["success_rate"], None)
        experience_mod.feedback(self.db, experience_id=experience_id, signal="useful")
        rate = experience_mod.success_rate(self.db)
        self.assertEqual(rate["success_rate"], 1.0)
        experience_mod.feedback(self.db, experience_id=experience_id, signal="wrong")
        self.assertEqual(experience_mod.success_rate(self.db)["success_rate"], 0.0)

    def test_two_wrongs_make_stale_and_useful_revives(self) -> None:
        self._seed()
        experience_id = self._experience()
        experience_mod.feedback(self.db, experience_id=experience_id, signal="wrong")
        row = self.db.conn.execute(
            "SELECT status FROM experience WHERE id=?", (experience_id,)
        ).fetchone()
        self.assertEqual(row["status"], "active")
        experience_mod.feedback(self.db, experience_id=experience_id, signal="wrong")
        row = self.db.conn.execute(
            "SELECT status, confidence FROM experience WHERE id=?", (experience_id,)
        ).fetchone()
        self.assertEqual(row["status"], "stale")
        experience_mod.feedback(self.db, experience_id=experience_id, signal="useful")
        row = self.db.conn.execute(
            "SELECT status FROM experience WHERE id=?", (experience_id,)
        ).fetchone()
        self.assertEqual(row["status"], "active")


class TestConflicts(Case):
    def test_escalate_only_old_open_conflicts(self) -> None:
        conflict_id = self._conflict()
        self.db.conn.execute(
            "UPDATE conflict SET created_at=? WHERE id=?",
            (stamp_for(30), conflict_id),
        )
        before = self.db.conn.execute(
            "SELECT severity FROM conflict WHERE id=?", (conflict_id,)
        ).fetchone()["severity"]
        stats = conflicts_mod.escalate(self.db, days=14)
        self.assertEqual(stats["escalated"], 1)
        after = self.db.conn.execute(
            "SELECT severity FROM conflict WHERE id=?", (conflict_id,)
        ).fetchone()["severity"]
        self.assertAlmostEqual(after, min(1.0, before + 0.2), places=3)

    def test_auto_resolve_superseded_side_only(self) -> None:
        conflict_id = self._conflict()
        row = self.db.conn.execute(
            "SELECT left_id, right_id FROM conflict WHERE id=?", (conflict_id,)
        ).fetchone()
        self.db.conn.execute(
            "UPDATE memory SET status='superseded' WHERE id=?", (row["right_id"],)
        )
        stats = conflicts_mod.auto_resolve(self.db)
        self.assertEqual(stats["resolved"], 1)
        conflict = self.db.conn.execute(
            "SELECT status, resolved_by FROM conflict WHERE id=?", (conflict_id,)
        ).fetchone()
        self.assertEqual(conflict["status"], "prefer_left")
        winner = self.db.conn.execute(
            "SELECT status FROM memory WHERE id=?", (row["left_id"],)
        ).fetchone()["status"]
        self.assertEqual(winner, "active")

    def test_ambiguous_conflict_stays_open(self) -> None:
        conflict_id = self._conflict()
        stats = conflicts_mod.auto_resolve(self.db)
        self.assertEqual(stats["resolved"], 0)
        self.assertEqual(stats["skipped"], 1)
        status = self.db.conn.execute(
            "SELECT status FROM conflict WHERE id=?", (conflict_id,)
        ).fetchone()["status"]
        self.assertEqual(status, "open")


class TestTuning(Case):
    def _add_labels(self, count: int = 50) -> None:
        memory_id = self.store.add("调参样本", session_id="tune")["id"]
        for index in range(count):
            self.db.conn.execute(
                "INSERT INTO feedback(id, ts, subject_id, target_tbl, target_id, signal, note, actor) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (f"fb-{index}", to_iso(now()), "self", "memory", memory_id,
                 "useful", None, "user"),
            )

    def test_skips_without_enough_labels(self) -> None:
        result = tuning_mod.tune(self.db)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["labels"], 0)
        self.assertIsNone(tuning_mod.active(self.db))

    def test_accepts_only_when_ndcg_improves(self) -> None:
        self._add_labels()
        baseline = {"ndcg": 0.5, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}

        def evaluate(overrides: dict) -> dict:
            if not overrides:
                return dict(baseline)
            if overrides["retrieval"]["w_rrf"] == 0.65:
                return {"ndcg": 0.56, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}
            return dict(baseline)

        result = tuning_mod.tune(self.db, evaluate=evaluate)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["applied"])
        active = tuning_mod.active(self.db)
        self.assertEqual(active["params"]["retrieval"]["w_rrf"], 0.65)
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM config_version WHERE status='active'"
            ).fetchone()["c"],
            1,
        )

    def test_rejects_without_gain_and_keeps_default(self) -> None:
        self._add_labels()
        flat = {"ndcg": 0.5, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}
        result = tuning_mod.tune(self.db, evaluate=lambda overrides: dict(flat))
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["applied"])
        self.assertIsNone(tuning_mod.active(self.db))
        self.assertEqual(
            self.db.conn.execute(
                "SELECT COUNT(*) c FROM config_version WHERE status='rejected'"
            ).fetchone()["c"],
            1,
        )

    def test_rollback_restores_default(self) -> None:
        self._add_labels()
        baseline = {"ndcg": 0.5, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}

        def evaluate(overrides: dict) -> dict:
            if overrides and overrides["retrieval"]["w_rrf"] == 0.65:
                return {"ndcg": 0.6, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}
            return dict(baseline)

        tuning_mod.tune(self.db, evaluate=evaluate)
        rolled = tuning_mod.rollback(self.db)
        self.assertTrue(rolled["rolled_back"])
        self.assertIsNone(tuning_mod.active(self.db))
        reopened = PMem.open(self.db_path)
        try:
            self.assertEqual(reopened.config.get("retrieval.w_rrf"), 0.55)
        finally:
            reopened.close()

    def test_active_version_is_loaded_on_open(self) -> None:
        self._add_labels()
        baseline = {"ndcg": 0.5, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}

        def evaluate(overrides: dict) -> dict:
            if (
                overrides
                and overrides["retrieval"]["w_rrf"] == 0.65
                and overrides["retrieval"]["mmr_lambda"] == 0.7
            ):
                return {"ndcg": 0.6, "recall": 0.8, "dedup": 1.0, "conflict": 1.0}
            return dict(baseline)

        tuning_mod.tune(self.db, evaluate=evaluate)
        self.app.close()
        reopened = PMem.open(self.db_path)
        try:
            self.assertEqual(reopened.config.get("retrieval.w_rrf"), 0.65)
            self.assertEqual(reopened.config.get("retrieval.mmr_lambda"), 0.7)
        finally:
            reopened.close()
            self.app = PMem.open(self.db_path)


class TestRunner(Case):
    def test_dry_run_rolls_back_labels_and_escalation(self) -> None:
        self._seed()
        pattern = self._pattern("frequency")
        review_mod.label(self.db, target_tbl="pattern", target_id=pattern, verdict="wrong")
        conflict_id = self._conflict()
        self.db.conn.execute(
            "UPDATE conflict SET created_at=? WHERE id=?", (stamp_for(30), conflict_id)
        )
        before_status = self.db.conn.execute(
            "SELECT status FROM pattern WHERE id=?", (pattern,)
        ).fetchone()["status"]
        before_severity = self.db.conn.execute(
            "SELECT severity FROM conflict WHERE id=?", (conflict_id,)
        ).fetchone()["severity"]
        result = evolve_runner.run(self.db, dry_run=True)
        self.assertTrue(result["rolled_back"])
        self.assertIn("patterns", result["stats"])
        self.assertEqual(
            self.db.conn.execute("SELECT status FROM pattern WHERE id=?", (pattern,)).fetchone()["status"],
            before_status,
        )
        self.assertEqual(
            self.db.conn.execute(
                "SELECT severity FROM conflict WHERE id=?", (conflict_id,)
            ).fetchone()["severity"],
            before_severity,
        )

    def test_run_records_event_and_stats(self) -> None:
        self._seed()
        result = evolve_runner.run(self.db, layers=["patterns", "conflicts"])
        self.assertEqual(result["layers"], ["patterns", "conflicts"])
        event = self.db.conn.execute(
            "SELECT * FROM event_log WHERE op='evolve.run' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(event)
        stats = evolve_runner.stats(self.db)
        self.assertIn("tuning", stats)
        self.assertEqual(stats["review"]["pattern"]["labeled"], 0)


class TestInterfaces(Case):
    def test_mcp_evolve(self) -> None:
        self._seed()
        mcp = InProcessMcp(self.db_path)
        try:
            result = mcp.tool("memory_evolve", {"action": "review", "target_tbl": "pattern"})
            self.assertTrue(result["ok"], result)
            self.assertGreaterEqual(len(result["data"]["items"]), 1)
            tools = mcp.call("tools/list")["result"]["tools"]
            self.assertIn("memory_evolve", [tool["name"] for tool in tools])
        finally:
            mcp.close()

    def test_cli_review_and_label(self) -> None:
        self._seed()
        code, out = run_cli(["--db", str(self.db_path), "evolve", "review", "--json"])
        self.assertEqual(code, 0)
        items = json.loads(out)["data"]["items"]
        self.assertGreaterEqual(len(items), 1)
        target = items[0]["id"]
        code, _ = run_cli(
            ["--db", str(self.db_path), "evolve", "label", "pattern", target, "correct"]
        )
        self.assertEqual(code, 0)
        code, out = run_cli(["--db", str(self.db_path), "evolve", "stats", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["labels"], 1)

    def test_cli_experience_match(self) -> None:
        self._seed()
        code, out = run_cli(
            [
                "--db",
                str(self.db_path),
                "evolve",
                "experience",
                "match",
                "给陌生项目做技术选型",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(json.loads(out)["data"]["items"]), 1)


class TestNdcg(Case):
    def test_eval_exposes_ndcg_at_k(self) -> None:
        from pmem.eval import runner

        metrics = runner.run(seed=7, out_dir=Path(self._dir) / "eval", max_groups=1)
        ndcg = metrics["recall"]["ndcg_at_k"]
        self.assertGreaterEqual(ndcg, 0.0)
        self.assertLessEqual(ndcg, 1.0)


class TestReviewQueueScale(Case):
    def test_thirty_candidates_can_be_queued(self) -> None:
        names = ["张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十", "郑一", "冯二"]
        for index, name in enumerate(names):
            variants = [
                [f"{name}住在城市{index}", f"{name}目前住在城市{index}", f"{name}现在住在城市{index}"],
                [f"{name}在公司{index}工作", f"{name}目前在公司{index}工作", f"{name}现在在公司{index}工作"],
                [f"{name}使用工具{index}", f"{name}在用工具{index}", f"{name}用工具{index}"],
            ]
            for attribute in variants:
                for position, offset in enumerate((3, 2, 1)):
                    stamp = stamp_for(offset)
                    self.store.add(
                        attribute[position], created_at=stamp, session_id=f"{name}-{offset}"
                    )
        from pmem.sleep import patterns

        stats = patterns.mine(self.db, subject="self", day_key=day_for(1))
        self.assertGreaterEqual(stats["created"], 30)
        queue = review_mod.review_queue(self.db, target_tbl="pattern", limit=30)
        self.assertEqual(len(queue), 30)


if __name__ == "__main__":
    unittest.main()
