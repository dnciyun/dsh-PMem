"""原话通道（task-53）：关时等价、只增不减、去重、时间衰减、预算上限。"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal
from pmem.memory.retrieval import rawline

from .support import drop_dir, temp_dir

# 这两个对照里关掉旧 raw_fallback：否则它先把同样的原话塞进 payload，rawline 的去重
# 会正确地判重、测不到「追加」；我们要测的是 rawline 通道本身。
OFF_A = Config({"retrieval": {"raw_fallback": False, "rawline": {"enabled": False}}})
OFF_B = Config(
    {"retrieval": {"raw_fallback": False, "time_router": {"enabled": False},
                   "rawline": {"enabled": False}}}
)
ON = Config(
    {
        "retrieval": {
            "raw_fallback": False,
            "rawline": {
                "enabled": True,
                "count": 8,
                "max_age_days": 45,
                "min_score": 0.0,
                "budget_share": 0.4,
                "dedupe_overlap": 0.6,
            },
        }
    }
)
TIGHT = Config(
    {
        "retrieval": {
            "raw_fallback": False,
            "rawline": {
                "enabled": True,
                "count": 8,
                "max_age_days": 45,
                "min_score": 0.0,
                "budget_share": 0.1,
                "dedupe_overlap": 0.6,
            },
        }
    }
)


class RawlineCase(unittest.TestCase):
    def setUp(self) -> None:
        clock.set_now("2026-11-08T12:00:00+08:00")
        self._dirs = []
        self._apps = []
        seed_dir = temp_dir("rawline-seed")
        self._dirs.append(seed_dir)
        seed = PMem.open(seed_dir / "seed.db", config=Config())
        seed.store.add("用户住在南岗，周末喜欢去江边散步", session_id="s1")
        journal.append(
            seed.db,
            "今天在南岗地铁口买了点香菜，回家做饭",
            role="user",
            ts="2026-11-07T18:00:00+08:00",
        )
        journal.append(
            seed.db,
            "很久以前在南岗住过一阵，后来搬走了",
            role="user",
            ts="2026-01-01T10:00:00+08:00",
        )
        journal.append(
            seed.db,
            "助手说南岗那边的地铁很方便",
            role="assistant",
            ts="2026-11-06T09:00:00+08:00",
        )
        self._seed_path = seed_dir / "seed.db"
        seed.close()

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open_copy(self, config: Config) -> PMem:
        path = temp_dir("rawline-run")
        self._dirs.append(path)
        db = path / "pmem.db"
        shutil.copyfile(self._seed_path, db)
        app = PMem.open(db, config=config)
        self._apps.append(app)
        return app


class RawlineTest(RawlineCase):
    def test_disabled_is_byte_identical_and_no_extra_items(self) -> None:
        app_a = self._open_copy(OFF_A)
        app_b = self._open_copy(OFF_B)
        app_a.store.search("南岗 香菜", k=8, record=False)
        app_b.store.search("南岗 香菜", k=8, record=False)
        conn_a, conn_b = app_a.store.db.conn, app_b.store.db.conn
        stmts_a: list[str] = []
        stmts_b: list[str] = []
        conn_a.set_trace_callback(stmts_a.append)
        conn_b.set_trace_callback(stmts_b.append)
        found_a = app_a.store.search("南岗 香菜", k=8, record=False)
        found_b = app_b.store.search("南岗 香菜", k=8, record=False)
        conn_a.set_trace_callback(None)
        conn_b.set_trace_callback(None)
        for payload in (found_a, found_b):
            payload.pop("latency_ms", None)
            if isinstance(payload.get("meta", {}).get("retrieval"), dict):
                payload["meta"]["retrieval"].pop("latency_ms", None)
        self.assertEqual(
            json.dumps(found_a, ensure_ascii=False, sort_keys=True),
            json.dumps(found_b, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(stmts_a, stmts_b)
        self.assertFalse(
            any("rawline" in (item.get("channels") or []) for item in found_a["items"])
        )

    def test_enabled_only_appends_after_baseline(self) -> None:
        app_off = self._open_copy(OFF_A)
        app_on = self._open_copy(ON)
        off = app_off.store.search("南岗 香菜", k=8, record=False)["items"]
        on = app_on.store.search("南岗 香菜", k=8, record=False)["items"]
        base = [item["text"] for item in off]
        self.assertEqual([item["text"] for item in on[: len(base)]], base, "原有条目顺序不能变")
        extra = on[len(base) :]
        self.assertTrue(extra)
        self.assertTrue(all("rawline" in (item.get("channels") or []) for item in extra))
        self.assertTrue(all(item.get("kind") == "journal" for item in extra))
        self.assertTrue(all(item.get("source_line_id") for item in extra))
        self.assertTrue(all(item.get("role") in ("user", "assistant") for item in extra))

    def test_same_payload_text_is_not_duplicated(self) -> None:
        app = self._open_copy(ON)
        duplicated = "今天在南岗地铁口买了点香菜，回家做饭"
        # 先塞一条与 journal 行完全相同的 memory 文本，rawline 应该按 12 字窗口判重。
        app.store.add(duplicated, session_id="s1")
        found = app.store.search("南岗 香菜", k=8, record=False)
        texts = [
            item["text"] for item in found["items"]
            if "rawline" in (item.get("channels") or [])
        ]
        self.assertNotIn(duplicated, texts)
        self.assertFalse(any(duplicated in text for text in texts))

    def test_time_decay_prefers_newer_line(self) -> None:
        app = self._open_copy(ON)
        candidates = rawline.candidates(
            app.store, "南岗", cutoff_ts="2026-11-08T12:00:00+08:00", cfg=ON
        )
        self.assertGreaterEqual(len(candidates), 2)
        newest = next(item for item in candidates if item["ts"].startswith("2026-11-07"))
        oldest = next(item for item in candidates if item["ts"].startswith("2026-01-01"))
        self.assertGreater(oldest["age_days"], newest["age_days"])
        self.assertGreater(newest["score"], oldest["score"])

    def test_budget_share_caps_rawline_chars(self) -> None:
        app = self._open_copy(TIGHT)
        found = app.store.search("南岗 香菜", k=8, budget=200, record=False)
        raw = [
            item for item in found["items"]
            if "rawline" in (item.get("channels") or [])
        ]
        self.assertLessEqual(sum(len(item["text"]) for item in raw), 200 * 0.1)


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
