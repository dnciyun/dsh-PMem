"""M9：长期运行（调度 / 备份演练 / 监控 / 月报）与海马体星图。

M9 的质量门里有两条只能等日历时间（30 天真实运行、recall@5 不下降）。
这里用**可注入时钟的 30 天模拟**验证调度器不丢/不重复，用真实备份副本验证
恢复演练；真实 30 天指标仍由 ``pmem monitor`` 在日历时间上积累。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from pmem.base import events
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.ops import backup, jobs, monitor, monthly, scheduler, starmap
from pmem.ops.metrics import record_eval

from .support import drop_dir, temp_dir


def _local(*args: int) -> datetime:
    return datetime(*args).astimezone()


class SchedulerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m9s")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)
        self.cfg = Config(
            {
                "sleep": {"at": "03:30", "backfill_days": 3},
                "runtime": {
                    "reflect_at": "03:45",
                    "backup_at": "04:00",
                    "monitor_at": "04:30",
                    "monthly_day": 1,
                    "monthly_at": "04:15",
                    "max_attempts": 3,
                    "retry_minutes": 1,
                    "stale_minutes": 60,
                },
            }
        )

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_next_after_slots(self) -> None:
        now = _local(2026, 11, 2, 12, 0)
        sleep_at = scheduler.next_after("sleep", now, self.cfg)
        self.assertEqual((sleep_at.hour, sleep_at.minute), (3, 30))
        self.assertEqual(sleep_at.date(), (now + timedelta(days=1)).date())
        monthly_at = scheduler.next_after("monthly", now, self.cfg)
        self.assertEqual((monthly_at.day, monthly_at.hour, monthly_at.minute), (1, 4, 15))
        self.assertGreater(monthly_at, now)

    def test_30_day_simulation_no_lost_or_duplicate(self) -> None:
        start = _local(2026, 11, 1, 0, 0)
        counts: dict[str, int] = {}
        seen: list[tuple[str, str]] = []

        def fake(db, cfg, subject, kind, payload):
            counts[kind] = counts.get(kind, 0) + 1
            seen.append((kind, db.conn.execute("SELECT date('now')").fetchone()[0]))
            return {"ok": True}

        scheduler.ensure_jobs(self.app.db, now=start, config=self.cfg)
        for step in range(30 * 96):  # 30 天，每 15 分钟一次
            now = start + timedelta(minutes=15 * step)
            result = scheduler.tick(
                self.app.db, now=now, config=self.cfg, runner=fake, max_jobs=8
            )
            self.assertTrue(result["ok"], result)
        self.assertEqual(counts.get("sleep"), 30, counts)
        self.assertEqual(counts.get("backup"), 30, counts)
        self.assertEqual(counts.get("monitor"), 30, counts)
        self.assertEqual(counts.get("monthly"), 1, counts)
        self.assertEqual(counts.get("reflect_week"), 5, counts)  # 11/2,9,16,23,30
        self.assertEqual(sum(counts.values()), 96)
        rows = jobs.list_jobs(self.app.db)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["state"] == "pending" for row in rows), rows)
        self.assertTrue(all(row["attempts"] == 0 for row in rows), rows)
        self.assertTrue(all(row["next_run_at"] > "2026-11-30" for row in rows), rows)

    def test_failure_backoff_then_dead(self) -> None:
        t0 = _local(2026, 11, 1, 5, 0)
        scheduler.ensure_jobs(self.app.db, now=t0, config=self.cfg)
        self.app.db.conn.execute(
            "UPDATE job SET next_run_at=? WHERE id='sched:sleep'", (scheduler.to_iso(t0),)
        )

        def boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        for minutes in (0, 2, 4):
            scheduler.tick(
                self.app.db,
                now=t0 + timedelta(minutes=minutes),
                config=self.cfg,
                runner=boom,
            )
        row = jobs.get(self.app.db, "sleep")
        self.assertEqual(row["state"], "dead", row)
        self.assertGreaterEqual(row["attempts"], 3)
        result = monitor.collect(self.app.db, config=self.cfg)
        dead = next(item for item in result["checks"] if item["name"] == "jobs")
        self.assertEqual(dead["status"], "fail")
        self.assertFalse(result["ok"])

    def test_recover_stale_running(self) -> None:
        t0 = _local(2026, 11, 1, 5, 0)
        scheduler.ensure_jobs(self.app.db, now=t0, config=self.cfg)
        self.app.db.conn.execute(
            "UPDATE job SET state='running', updated_at=?, next_run_at=? WHERE id='sched:sleep'",
            ("2026-11-01T00:00:00.000Z", scheduler.to_iso(t0)),
        )
        called = []

        def fake(*args, **kwargs):
            called.append(1)
            return {"ok": True}

        result = scheduler.tick(self.app.db, now=t0, config=self.cfg, runner=fake)
        self.assertEqual(result["recovered"], ["sched:sleep"])
        self.assertEqual(len(called), 1)
        self.assertEqual(jobs.get(self.app.db, "sleep")["state"], "pending")


class BackupCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m9b")
        self.path = Path(self._dir) / "pmem.db"
        self.backup_dir = Path(self._dir) / "backups"
        self.app = PMem.open(self.path)
        self.cfg = Config({"runtime": {"backup_dir": str(self.backup_dir), "backup_keep": 5}})
        for i in range(3):
            self.app.add(f"备份测试记忆 {i}", kind="semantic", provenance="user_explicit")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_backup_and_drill(self) -> None:
        result = backup.run(self.app.db, config=self.cfg)
        self.assertTrue(result["ok"], result)
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["memory_count"], 3)
        self.assertTrue(result["sha256"])
        row = self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE id=?", (result["snapshot_id"],)
        ).fetchone()
        self.assertIsNotNone(row)

        drill = backup.drill(self.app.db, config=self.cfg)
        self.assertTrue(drill["ok"], drill)
        self.assertEqual(drill["restored"]["memory"], 3)
        self.assertEqual(drill["counts"]["memory"], 3)
        self.assertIn("event_chain", {item["name"] for item in drill["checks"]})
        self.assertGreaterEqual(drill["restore_ms"], 0)

    def test_prune_keeps_latest(self) -> None:
        for _ in range(3):
            backup.run(self.app.db, config=self.cfg, keep=1)
        rows = self.app.db.conn.execute(
            "SELECT * FROM snapshot WHERE kind='manual'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(Path(rows[0]["path"]).exists())
        files = list(self.backup_dir.glob("pmem-backup-*.db"))
        self.assertEqual(len(files), 1)

    def test_drill_missing_source(self) -> None:
        result = backup.drill(self.app.db, config=self.cfg, source=str(self.backup_dir / "nope.db"))
        self.assertFalse(result["ok"])
        self.assertIn("没有可用", result["error"])


class MonitorCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m9m")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)
        self.cfg = Config({"runtime": {"recall_drop_tolerance": 0.02}})

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_recall_regression_fails(self) -> None:
        for day, value in (("2026-11-01", 0.95), ("2026-11-02", 0.90)):
            self.app.db.conn.execute(
                "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
                (f"{day}T00:00:00.000Z", day, "eval.recall_at_5", value, "{}"),
            )
        result = monitor.collect(self.app.db, config=self.cfg)
        recall = next(item for item in result["checks"] if item["name"] == "recall")
        self.assertEqual(recall["status"], "fail")
        self.assertFalse(result["ok"])

    def test_record_writes_metrics_and_event(self) -> None:
        result = monitor.collect(self.app.db, config=self.cfg, record=True)
        self.assertIn("db_size_bytes", result["metrics"])
        names = {
            row["name"]
            for row in self.app.db.conn.execute("SELECT name FROM metrics WHERE name LIKE 'ops.%'")
        }
        self.assertIn("ops.db_size_bytes", names)
        self.assertIn("ops.health", names)
        event = self.app.db.conn.execute(
            "SELECT * FROM event_log WHERE op='ops.monitor'"
        ).fetchone()
        self.assertIsNotNone(event)


class MonthlyCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m9r")
        self.path = Path(self._dir) / "pmem.db"
        self.app = PMem.open(self.path)
        for i in range(2):
            self.app.add(f"月报记忆 {i}", kind="semantic", provenance="user_explicit")
        self.app.db.conn.execute(
            "INSERT INTO reflection(id,subject_id,period,questions_json,findings_json,"
            "hypotheses_json,metrics_json,suggestions_json,created_at,run_id) "
            "VALUES('r1','self','day:2026-11-02','{}','[]','[]','{\"quality\": 61.5}','[]',?,NULL)",
            ("2026-11-02T00:00:00.000Z",),
        )
        self.app.db.conn.execute(
            "INSERT INTO metrics(ts,day_key,name,value,labels_json) VALUES(?,?,?,?,?)",
            ("2026-11-02T00:00:00.000Z", "2026-11-02", "reflect.quality", 61.5, "{}"),
        )
        self.app.db.conn.execute(
            "INSERT INTO metrics(ts,day_key,name,value,labels_json) VALUES(?,?,?,?,?)",
            ("2026-11-02T00:00:00.000Z", "2026-11-02", "eval.recall_at_5", 0.981, "{}"),
        )

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_generate_writes_markdown_and_json(self) -> None:
        out = Path(self._dir) / "reports"
        result = monthly.generate(self.app.db, month="2026-11", out=out)
        self.assertTrue(result["ok"], result)
        self.assertTrue(Path(result["path"]).exists())
        self.assertTrue(Path(result["json_path"]).exists())
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("PMem 月报 2026-11", text)
        self.assertIn("新增记忆", text)
        self.assertIn("61.5", text)
        self.assertIn("recall@5：0.981", text)
        data = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["month"], "2026-11")
        self.assertGreaterEqual(data["created"], 2)
        self.assertTrue(data["partial"])

    def test_month_bounds_validation(self) -> None:
        with self.assertRaises(ValueError):
            monthly.generate(self.app.db, month="2026-13")
        with self.assertRaises(ValueError):
            monthly.generate(self.app.db, month="bad")


class EvalMetricCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m9e")
        self.app = PMem.open(Path(self._dir) / "pmem.db")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_record_eval_is_queryable(self) -> None:
        result = record_eval(
            self.app.db,
            {
                "recall": {"lexical_recall_at_k": 0.91, "ndcg_at_k": 0.8, "latency_p50_ms": 3.2},
                "dedup": {"precision": 1.0, "recall": 0.87},
                "conflict": {"f1": 1.0},
                "poison": {"count": 0},
            },
        )
        self.assertTrue(result["ok"], result)
        row = self.app.db.conn.execute(
            "SELECT value FROM metrics WHERE name='eval.recall_at_5'"
        ).fetchone()
        self.assertAlmostEqual(row["value"], 0.91)
        event = self.app.db.conn.execute("SELECT * FROM event_log WHERE op='ops.eval.record'").fetchone()
        self.assertIsNotNone(event)


class StarMapCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("m9x")
        self.app = PMem.open(
            Path(self._dir) / "pmem.db", config=Config({"policy": {"allow_secret": True}})
        )
        self.ids = []

        def add(text, **kwargs):
            result = self.app.add(text, provenance="user_explicit", **kwargs)
            self.assertTrue(result["ok"], result)
            return result["data"]["id"]

        name_id = add("我是小安，An，小安。", kind="person")
        origin_id = add("我们写 PMem，是为了持久相处，不是为了做一个记忆库。", kind="semantic")
        chat_id = add("今天聊了海马体星图。", kind="episodic")
        db_id = add("用户提到数据库和可塑性。", kind="semantic")
        self.ids = [name_id, origin_id, chat_id, db_id]
        for target, path, grade in (
            (name_id, "我/姓名", "S"),
            (origin_id, "我们/初衷", "S"),
            (chat_id, "我们", "B"),
        ):
            result = self.app.group(
                action="assign", target_tbl="memory", target_id=target, path=path, grade=grade
            )
            self.assertTrue(result["ok"], result)
        self.app.add("秘密：不要显示这段。", kind="semantic", provenance="user_explicit",
                     sensitivity="secret")

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_build_has_bounded_deterministic_layout(self) -> None:
        self.app.add("猫喜欢在阳台晒太阳，猫很可爱。", kind="episodic", provenance="observed")
        self.app.add("猫在阳台晒太阳，猫很可爱，喜欢猫。", kind="episodic", provenance="observed")
        one = starmap.build(self.app.db)
        two = starmap.build(self.app.db)
        self.assertEqual(len(one["nodes"]), len(two["nodes"]))
        for node in one["nodes"]:
            self.assertGreaterEqual(node["x"], 0)
            self.assertLessEqual(node["x"], 1000)
            self.assertGreaterEqual(node["y"], 0)
            self.assertLessEqual(node["y"], 1000)
        positions = {n["id"]: (round(n["x"], 3), round(n["y"], 3)) for n in one["nodes"]}
        for node in two["nodes"]:
            self.assertEqual(positions[node["id"]], (round(node["x"], 3), round(node["y"], 3)))
        self.assertEqual(one["meta"]["shown"], 7)
        self.assertIn("memories", one["meta"]["counts"])
        self.assertEqual(one["shape"]["kind"], "organic")
        self.assertTrue(any(link["type"] == "similar" for link in one["links"]), one["links"])

    def test_write_html_svg_json_png_and_redaction(self) -> None:
        out = Path(self._dir) / "starmap"
        result = starmap.write(self.app.db, out=out, png=True)
        self.assertTrue(result["ok"], result)
        for key in ("html", "svg", "json"):
            self.assertTrue(Path(result[key]).exists(), key)
        html = Path(result["html"]).read_text(encoding="utf-8")
        self.assertIn("海马体星图", html)
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("不要显示这段", html)  # secret 默认隐去
        # 数据里的 </script> 会被转义，不能提前闭合脚本
        self.assertEqual(html.count("</script>"), 1)
        data = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        secret = next(n for n in data["nodes"] if n["type"] == "memory" and n.get("sensitivity") == "secret")
        self.assertNotIn("不要显示这段", secret["text"])
        if result.get("png", {}).get("ok"):
            self.assertEqual(Path(result["png"]["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_html_escapes_payload_text(self) -> None:
        payload = "</script><script>alert(1)</script>"
        self.app.add(payload, kind="semantic", provenance="observed")
        out = Path(self._dir) / "xss"
        result = starmap.write(self.app.db, out=out, png=False)
        html = Path(result["html"]).read_text(encoding="utf-8")
        self.assertEqual(html.count("</script>"), 1)
        self.assertNotIn(payload, html)


if __name__ == "__main__":
    unittest.main()
