"""大睡一场（0.35.1）：按天把已有记忆从头睡一遍。

覆盖：模板模式、鲸眠模式、日期范围、limit、skip-digested、dry-run、鲸眠失败回落模板。
全程离线（假模型），不联网。
"""

from __future__ import annotations

import json
import unittest

from pmem.base.config import Config
from pmem.base.db import Database
from pmem.bench.model import Reply, Usage
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.memory.store import MemoryStore
from pmem.sleep import big

from .support import drop_dir, temp_dir

CFG = Config({"retrieval": {"vector": {"enabled": False}}, "net": {"auto_extract": False}})
DAYS = ["2026-05-01", "2026-05-02", "2026-05-03"]


class FakeModel:
    name = "fake-big"
    paid = False

    def __init__(self, replies: list[Reply] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls = 0

    def complete(self, messages, tools=None, *, hint=None):
        self.calls += 1
        if self.replies:
            return self.replies.pop(0)
        return Reply(content="不是 JSON", usage=Usage(10, 5))


def ok_reply(tag: str) -> Reply:
    return Reply(
        content=json.dumps(
            {"title": f"加班到十点{tag}", "lines": ["你今晚要加班到十点"], "loops": []},
            ensure_ascii=False,
        ),
        usage=Usage(100, 30),
    )


class BigSleepCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("big-sleep")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        self.store = MemoryStore(self.db, config=CFG, subject="self", actor="test")
        self.app = PMem(self.db, config=CFG)
        self.transcripts = self.dir / "whale-sleep"

    def tearDown(self) -> None:
        self.db.close()
        drop_dir(self.dir)

    def _ingest_days(self, days: list[str]) -> None:
        for day in days:
            for index, text in enumerate(["今晚要加班到十点", "明天可能下雨，记得带伞", "我先去开会了"]):
                journal.append(
                    self.db, text, subject="self", session_id=f"s-{day}",
                    role="user", ts=f"{day}T1{index}:00:00Z",
                )

    def _count(self, table: str) -> int:
        return self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _run(self, **kwargs):
        return big.run(
            self.db,
            subject="self",
            store=self.store,
            app=self.app,
            config=CFG,
            transcript_dir=None,
            **kwargs,
        ) if False else big.run(
            self.db, subject="self", store=self.store, app=self.app, config=CFG, **kwargs
        )

    def test_template_mode_sleeps_every_day(self) -> None:
        self._ingest_days(DAYS)
        result = self._run(use_whale=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["days"], 3)
        self.assertEqual(result["template_days"], 3)
        self.assertEqual(result["whale_days"], 0)
        self.assertEqual(self._count("episode"), 3)
        self.assertEqual(self._count("sleep_run"), 3)

    def test_whale_mode_uses_model_and_transcripts(self) -> None:
        self._ingest_days(DAYS[:2])
        model = FakeModel([ok_reply("A"), ok_reply("B")])
        cfg = Config({
            "retrieval": {"vector": {"enabled": False}},
            "net": {"auto_extract": False},
            "sleep": {"whale": {"transcript_dir": str(self.transcripts)}},
        })
        result = big.run(
            self.db, subject="self", store=self.store, app=self.app, config=cfg,
            model=model, use_whale=True,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["whale_days"], 2)
        self.assertEqual(result["template_days"], 0)
        self.assertGreater(result["tokens"], 0)
        self.assertEqual(self._count("episode"), 2)
        self.assertTrue((self.transcripts / f"{DAYS[0]}.jsonl").exists())
        self.assertTrue((self.transcripts / f"{DAYS[1]}.jsonl").exists())

    def test_date_range_and_limit(self) -> None:
        self._ingest_days(DAYS)
        ranged = self._run(use_whale=False, start=DAYS[1], end=DAYS[1])
        self.assertEqual(ranged["days"], 1)
        self.assertEqual([row["day"] for row in ranged["results"]], [DAYS[1]])
        limited = self._run(use_whale=False, limit=1)
        self.assertEqual(limited["days"], 1)

    def test_skip_digested_after_first_pass(self) -> None:
        self._ingest_days(DAYS)
        self._run(use_whale=False)
        # --skip-digested 的语义是「只列出还没消化的天」：全都消化过了就是 0 天，不再重复睡。
        self.assertEqual(big.select_days(self.db, "self", skip_digested=True), [])
        second = self._run(use_whale=False, skip_digested=True)
        self.assertEqual(second["days"], 0)
        self.assertEqual(self._count("episode"), 3, "跳过时不能再写一篇")

    def test_dry_run_writes_nothing(self) -> None:
        self._ingest_days(DAYS[:1])
        result = self._run(use_whale=False, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self._count("episode"), 0)
        self.assertEqual(self._count("sleep_run"), 0)

    def test_whale_failure_falls_back_to_template(self) -> None:
        self._ingest_days(DAYS[:1])
        model = FakeModel([])  # 永远回非 JSON → 鲸眠判 JSON 失败
        cfg = Config({
            "retrieval": {"vector": {"enabled": False}},
            "net": {"auto_extract": False},
            "sleep": {"whale": {"transcript_dir": str(self.transcripts)}},
        })
        result = big.run(
            self.db, subject="self", store=self.store, app=self.app, config=cfg,
            model=model, use_whale=True,
        )
        self.assertTrue(result["ok"])
        item = result["results"][0]
        self.assertIn("whale_error", item)
        self.assertEqual(item["diary"]["mode"], "template")
        self.assertEqual(self._count("episode"), 1)

    def test_sleep_plan_lists_days_without_writing(self) -> None:
        self._ingest_days(DAYS)
        result = self.app.sleep_plan(start=DAYS[1], end=DAYS[2])
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["days"], [DAYS[1], DAYS[2]])
        self.assertEqual(result["data"]["count"], 2)
        self.assertEqual(self._count("episode"), 0)

    def test_bad_range_raises(self) -> None:
        from pmem.base.errors import PmemError

        with self.assertRaises(PmemError):
            big.validate_range("2026-13-01", None)
        with self.assertRaises(PmemError):
            big.validate_range("2026-05-03", "2026-05-01")


if __name__ == "__main__":
    unittest.main()
