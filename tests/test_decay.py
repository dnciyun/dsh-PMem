"""软淡忘（task-40）：只在排序里降权，不改状态/候选集/注入条数。

守住四件事：

1. 衰减公式：``max(min_factor, 0.5 ** (idle / half_life))``，S 级比 D 级抗衰减；
2. ``min_factor`` 是硬下限：久不用的高相关记忆最多损失 ``1 - min_factor``；
3. ``enabled=false``（默认）时排序与没有这个键时**逐字节一致**；
4. 打开时只改顺序：候选集合与注入条数不变，老记忆仍 active、仍可 `pmem search` 取到。
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import scoring

from .support import drop_dir, temp_dir

NOW = "2026-11-07T12:00:00+08:00"
# 老但强相关（S 级） vs 新但弱相关（B 级）：关时老的排前，开时老的让位。
OLD_TEXT = "用户喜欢喝咖啡，常去街角那家喝咖啡，每天一杯"
NEW_TEXT = "用户最近买了手冲壶，偶尔喝咖啡"
BASE = {"retrieval": {"time_router": {"enabled": False}, "vector": {"enabled": False, "channel": False}}}
DECAY_ON = {
    "enabled": True,
    "half_life_days": 30,
    "min_factor": 0.35,
    "grade_weight": True,
    "use_count_weight": True,
}


def _config(decay: dict | None = None) -> Config:
    base = json.loads(json.dumps(BASE))
    if decay is not None:
        base["retrieval"]["decay"] = dict(decay)
    return Config(base)


class DecayFactorTest(unittest.TestCase):
    def test_half_life_and_floor(self) -> None:
        self.assertAlmostEqual(
            scoring.decay_factor(idle_days=30, half_life_days=30, min_factor=0.35), 0.5
        )
        self.assertAlmostEqual(
            scoring.decay_factor(idle_days=1000, half_life_days=30, min_factor=0.35), 0.35
        )
        self.assertAlmostEqual(
            scoring.decay_factor(idle_days=0, half_life_days=30, min_factor=0.35), 1.0
        )

    def test_grade_weight_keeps_s_high_d_low(self) -> None:
        s = scoring.decay_factor(idle_days=30, grade="S")
        d = scoring.decay_factor(idle_days=30, grade="D")
        self.assertAlmostEqual(s, 0.5)
        self.assertAlmostEqual(d, 0.35)
        self.assertGreater(s, d)

    def test_use_count_bonus_is_log_and_bounded(self) -> None:
        low = scoring.decay_factor(idle_days=30, use_count=0)
        high = scoring.decay_factor(idle_days=30, use_count=4)
        huge = scoring.decay_factor(idle_days=30, use_count=99999)
        self.assertGreater(high, low)
        self.assertLessEqual(huge, 0.5 * 1.10)
        self.assertAlmostEqual(huge, 0.5 * 1.10)

    def test_zero_half_life_means_no_time_penalty(self) -> None:
        self.assertAlmostEqual(scoring.decay_factor(idle_days=500, half_life_days=0), 1.0)


class SearchDecayTest(unittest.TestCase):
    """用同一份 seed 库，比较「关 / 无键 / 开」三种配置的检索结果。"""

    def setUp(self) -> None:
        self._dirs = []
        self._apps: list[PMem] = []
        clock.set_now(NOW)
        seed_dir = temp_dir("decay-seed")
        self._dirs.append(seed_dir)
        seed = PMem.open(seed_dir / "seed.db", config=_config())
        seed.store.add(OLD_TEXT, session_id="s1", created_at="2025-01-01T10:00:00+00:00", grade="S")
        seed.store.add(NEW_TEXT, session_id="s1", created_at="2026-11-06T10:00:00+00:00", grade="B")
        self._seed_path = seed_dir / "seed.db"
        seed.close()

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _run(self, config: Config) -> tuple[list[dict], str, list[str], dict]:
        path = temp_dir("decay-run")
        self._dirs.append(path)
        db = path / "pmem.db"
        shutil.copyfile(self._seed_path, db)
        app = PMem.open(db, config=config)
        self._apps.append(app)
        conn = app.store.db.conn
        app.store.search("咖啡", k=10, record=False)  # 预热
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        found = app.store.search("咖啡", k=10, record=False)
        conn.set_trace_callback(None)
        return (
            found["items"],
            json.dumps(found, ensure_ascii=False, sort_keys=True),
            statements,
            {"channels": found.get("channels"), "total_candidates": found.get("total_candidates")},
        )

    def test_disabled_is_byte_identical_to_missing_key(self) -> None:
        no_key, payload_no_key, sql_no_key, _pool = self._run(_config())
        explicit_off, payload_off, sql_off, _pool2 = self._run(_config({"enabled": False}))

        def _stable(payload: str) -> str:
            data = json.loads(payload)
            data.pop("latency_ms", None)  # 延迟天然抖动，不属于排序/返回内容
            if isinstance(data.get("meta", {}).get("retrieval"), dict):
                data["meta"]["retrieval"].pop("latency_ms", None)
            return json.dumps(data, ensure_ascii=False, sort_keys=True)

        self.assertEqual(_stable(payload_no_key), _stable(payload_off))
        self.assertEqual(sql_no_key, sql_off)
        self.assertEqual(
            [item["text"] for item in no_key],
            [OLD_TEXT, NEW_TEXT],
            "关时排序锁死：老但强相关的仍在新的前面",
        )

    def test_enabled_only_reorders_and_keeps_candidate_set(self) -> None:
        off_items, _payload, _sql, off_pool = self._run(_config({"enabled": False}))
        on_items, _payload_on, _sql_on, on_pool = self._run(_config(DECAY_ON))
        self.assertEqual(len(off_items), len(on_items), "注入条数不能变")
        self.assertEqual(off_pool, on_pool, "候选池（各通道候选数）不能变，只许排序/取舍变化")
        self.assertEqual(
            {item["text"] for item in off_items},
            {item["text"] for item in on_items},
            "候选集合不能变（只是顺序变）",
        )
        off_order = [item["text"] for item in off_items]
        on_order = [item["text"] for item in on_items]
        self.assertEqual(off_order, [OLD_TEXT, NEW_TEXT])
        self.assertEqual(on_order, [NEW_TEXT, OLD_TEXT], "久不用的 S 级记忆应降到新记忆后面")

    def test_old_memory_stays_active_and_searchable(self) -> None:
        path = temp_dir("decay-active")
        self._dirs.append(path)
        db = path / "pmem.db"
        shutil.copyfile(self._seed_path, db)
        app = PMem.open(db, config=_config(DECAY_ON))
        self._apps.append(app)
        found = app.store.search("咖啡", k=10, record=False)
        old_item = next(item for item in found["items"] if item["text"] == OLD_TEXT)
        row = app.db.conn.execute(
            "SELECT status FROM memory WHERE id=?", (old_item["id"],)
        ).fetchone()
        self.assertEqual(row["status"], "active", "软淡忘不改状态")
        big = app.store.search("咖啡", k=50, record=False)
        self.assertIn(OLD_TEXT, [item["text"] for item in big["items"]])


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
