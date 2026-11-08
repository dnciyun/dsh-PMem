"""失败聚类审核队列（P3-18）：只读汇总、聚类、落 JSON，不自动改参数。

守五件事：

1. 空库返回空队列（``total=0``、``clusters=[]``），不炸；
2. weak/空召回、浮现后没被用、conflict/selfcheck 事件都能各归各的形态；
3. ``build`` 纯只读：跑前跑后 recall_log / surface_log / event_log 行数不变；
4. ``write`` 只写 ``--out`` 这一份 JSON，字段齐全；
5. CLI 在 ``ops.review_queue.enabled=false`` 时拒绝（退出码 2、不产出文件），
   打开后能跑通。
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from pmem.base import clock, events
from pmem.gate.sdk import PMem
from pmem.ops import review_queue

from .support import drop_dir, temp_dir

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class ReviewQueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("review-queue")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    # -- 造样例 ---------------------------------------------------------
    def _weak_recall(self, query: str = "完全不存在的关键词xyz") -> None:
        self.app.search(query, k=5, budget=600)

    def _surface_row(
        self, memory_id: str, *, mode: str = "turn", ts: str = "2026-11-05T21:00:00+00:00"
    ) -> None:
        with self.app.db.tx() as conn:
            conn.execute(
                "INSERT INTO surface_log(id, session_id, target_tbl, target_id, mode, grade, "
                "tokens, cue, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                ("sl-1", "s1", "memory", memory_id, mode, "B", 20, "咖啡", ts),
            )

    def _event(self, op: str, payload: dict) -> None:
        with self.app.db.tx() as conn:
            events.append(
                conn,
                actor="test",
                op=op,
                subject_id="self",
                payload=payload,
            )

    def _count(self, table: str) -> int:
        return int(self.app.db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])

    # -- 用例 -----------------------------------------------------------
    def test_empty_db(self) -> None:
        data = review_queue.build(self.app.db)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["clusters"], [])
        self.assertTrue(data["generated_at"])

    def test_weak_or_empty_recall(self) -> None:
        self._weak_recall()
        data = review_queue.build(self.app.db)
        keys = [cluster["key"] for cluster in data["clusters"]]
        self.assertTrue(any(key in ("recall.empty", "recall.weak:empty") for key in keys), keys)
        cluster = data["clusters"][keys.index(next(k for k in keys if k.startswith("recall.")))]
        self.assertEqual(cluster["count"], 1)
        sample = cluster["samples"][0]
        self.assertEqual(sample["kind"], "recall")
        self.assertIn("关键词", sample["text_excerpt"])
        self.assertIn("returned", sample["evidence"])

    def test_surface_discarded(self) -> None:
        created = self.app.add("用户喜欢手冲咖啡", kind="semantic")["data"]
        self._surface_row(created["id"])
        data = review_queue.build(self.app.db)
        keys = [cluster["key"] for cluster in data["clusters"]]
        self.assertIn("surface.discarded:turn", keys)
        cluster = next(c for c in data["clusters"] if c["key"] == "surface.discarded:turn")
        self.assertEqual(cluster["count"], 1)
        self.assertIn("手冲", cluster["samples"][0]["text_excerpt"])

    def test_surface_used_is_not_discarded(self) -> None:
        created = self.app.add("用户喜欢手冲咖啡", kind="semantic")["data"]
        self._surface_row(created["id"])
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE memory SET last_used_at=? WHERE id=?",
                ("2026-11-06T10:00:00+00:00", created["id"]),
            )
        data = review_queue.build(self.app.db)
        keys = [cluster["key"] for cluster in data["clusters"]]
        self.assertNotIn("surface.discarded:turn", keys)

    def test_conflict_and_selfcheck_events(self) -> None:
        self._event("conflict.detect", {"query": "他说喜欢咖啡还是茶"})
        self._event("selfcheck.issue", {"note": "答案里的日期没有出处"})
        data = review_queue.build(self.app.db)
        keys = [cluster["key"] for cluster in data["clusters"]]
        self.assertIn("event:conflict.detect", keys)
        self.assertIn("event:selfcheck.issue", keys)
        total = sum(cluster["count"] for cluster in data["clusters"])
        self.assertEqual(total, 2)

    def test_samples_capped_count_kept(self) -> None:
        for index in range(7):
            self._weak_recall(f"不存在的问题{index}")
        data = review_queue.build(self.app.db)
        recall_clusters = [c for c in data["clusters"] if c["key"].startswith("recall.")]
        self.assertTrue(recall_clusters)
        cluster = recall_clusters[0]
        self.assertEqual(cluster["count"], 7)
        self.assertLessEqual(len(cluster["samples"]), review_queue.SAMPLE_CAP)

    def test_build_is_read_only(self) -> None:
        self._weak_recall()
        created = self.app.add("用户喜欢手冲咖啡", kind="semantic")["data"]
        self._surface_row(created["id"])
        before = {table: self._count(table) for table in ("recall_log", "surface_log", "event_log")}
        review_queue.build(self.app.db)
        after = {table: self._count(table) for table in ("recall_log", "surface_log", "event_log")}
        self.assertEqual(before, after)

    def test_write_json(self) -> None:
        self._weak_recall()
        data = review_queue.build(self.app.db)
        out = Path(self._dir) / "queue" / "review.json"
        target = review_queue.write(out, data)
        self.assertEqual(target, out)
        loaded = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["total"], data["total"])
        self.assertEqual(loaded["clusters"][0]["key"], data["clusters"][0]["key"])
        self.assertTrue(out.read_text(encoding="utf-8").endswith("\n"))

    # -- CLI ------------------------------------------------------------
    def _config_file(self, enabled: bool) -> Path:
        path = Path(self._dir) / "config.toml"
        path.write_text(
            "[ops.review_queue]\n"
            f"enabled = {'true' if enabled else 'false'}\n"
            "limit = 50\n",
            encoding="utf-8",
        )
        return path

    def test_cli_refuses_when_disabled(self) -> None:
        config = self._config_file(False)
        out = Path(self._dir) / "queue.json"
        err = io.StringIO()
        with redirect_stderr(err):
            code = review_queue.main(
                ["--db", str(self._dir / "pmem.db"), "--out", str(out), "--config", str(config)]
            )
        self.assertEqual(code, 2)
        self.assertIn("enabled=false", err.getvalue())
        self.assertFalse(out.exists())

    def test_cli_runs_when_enabled(self) -> None:
        self._weak_recall()
        config = self._config_file(True)
        out = Path(self._dir) / "queue.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = review_queue.main(
                ["--db", str(self._dir / "pmem.db"), "--out", str(out), "--config", str(config)]
            )
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertTrue(out.exists())
        loaded = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreaterEqual(loaded["total"], 1)

    def test_cli_missing_db(self) -> None:
        config = self._config_file(True)
        err = io.StringIO()
        with redirect_stderr(err):
            code = review_queue.main(
                [
                    "--db",
                    str(Path(self._dir) / "nope.db"),
                    "--out",
                    str(Path(self._dir) / "queue.json"),
                    "--config",
                    str(config),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("不存在", err.getvalue())


if __name__ == "__main__":
    unittest.main()
