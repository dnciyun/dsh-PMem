"""夜间可观测性（0.28.0）：sleep_history / sleep log 要能说清每次跑了什么。

用户的抱怨是「八个阶段看不见，完全不知道有没有执行」。数据本来就在 sleep_run 里
（stage / stats_json / status / error），缺的是读取入口——这组测试守住入口。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pmem import PMem
from pmem.base.ids import ulid


class TestSleepHistory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = PMem.open(Path(self.tmp.name) / "night.db")
        self.addCleanup(self.app.close)

    def _insert_run(
        self,
        *,
        day: str = "2026-11-03",
        status: str = "ok",
        stages: list | None = None,
        error: str | None = None,
    ) -> str:
        run_id = ulid()
        stats = {
            "stages": stages
            if stages is not None
            else [
                {"name": "s1_collect", "ok": True, "ms": 3, "stats": {"memories": 29}},
                {"name": "s2_dedup", "ok": True, "ms": 1, "stats": {"merged": 0}},
            ],
            "day_key": day,
        }
        self.app.db.conn.execute(
            "INSERT INTO sleep_run(id, subject_id, trigger, day_key, started_at, finished_at, "
            "status, stage, checkpoint_json, stats_json, error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "self",
                "manual",
                day,
                "2026-11-03T22:00:00Z",
                "2026-11-03T22:00:05Z",
                status,
                "s8_snapshot",
                "{}",
                json.dumps(stats, ensure_ascii=False),
                error,
            ),
        )
        self.app.db.conn.commit()
        return run_id

    def test_returns_runs_with_stages(self) -> None:
        run_id = self._insert_run()
        data = self.app.sleep_history(limit=5)["data"]
        self.assertEqual(len(data["runs"]), 1)
        run = data["runs"][0]
        self.assertEqual(run["id"], run_id)
        self.assertEqual(run["day"], "2026-11-03")
        self.assertEqual(run["status"], "ok")
        self.assertEqual([stage["name"] for stage in run["stages"]], ["s1_collect", "s2_dedup"])
        self.assertEqual(run["stages"][0]["stats"]["memories"], 29)

    def test_newest_first_and_limit(self) -> None:
        self._insert_run(day="2026-11-01")
        self._insert_run(day="2026-11-02")
        # 手工改时间保证排序稳定（同一个 started_at 下顺序不确定）
        self.app.db.conn.execute(
            "UPDATE sleep_run SET started_at='2026-11-01T22:00:00Z' WHERE day_key='2026-11-01'"
        )
        self.app.db.conn.execute(
            "UPDATE sleep_run SET started_at='2026-11-02T22:00:00Z' WHERE day_key='2026-11-02'"
        )
        self.app.db.conn.commit()
        runs = self.app.sleep_history(limit=1)["data"]["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["day"], "2026-11-02")

    def test_failed_run_keeps_error(self) -> None:
        self._insert_run(status="failed", stages=[], error="磁盘满了")
        run = self.app.sleep_history(limit=1)["data"]["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "磁盘满了")
        self.assertEqual(run["stages"], [])

    def test_empty_db_is_empty_list(self) -> None:
        self.assertEqual(self.app.sleep_history()["data"]["runs"], [])


if __name__ == "__main__":
    unittest.main()
