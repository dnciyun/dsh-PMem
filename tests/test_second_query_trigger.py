"""条件二次检索（P1-10，默认 trigger=1.01 = 保持 0.38「开关一开就总是跑」）。

守四件事：

1. 默认 trigger 1.01 > 覆盖率上限 → 行为与旧的「无条件跑第二遍」一致；
2. trigger 设低时：首遍 cov_best 不达门槛就**不跑**第二遍，返回里没有 ``second_query`` 段，
   结果与「second_query 关」逐字节一致（且 SQL 语句序列相同）；
3. 首遍覆盖率低于门槛时才跑第二遍（cov_best 取自评分循环，不重算）；
4. 关时（second_query.enabled=false）与 0.38.0 等价：SQL 序列 + 返回体。
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

BASE_OVERRIDES: dict = {
    "retrieval": {
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}
NEW_KEYS = frozenset({"retrieval.budget_trace", "retrieval.packing", "retrieval.spans",
                      "retrieval.aggregate", "retrieval.scope.project",
                      "retrieval.second_query"})

SEED = ("用户喜欢喝手冲咖啡，不加糖", "用户常去一家手冲咖啡店", "用户住在杭州，常去西湖散步")
# 带上噪音词，让 content_terms(q) != q，第二遍才有意义。
QUERY = "请问手冲咖啡呢"


def _config(**retrieval_overrides: object) -> Config:
    raw = copy.deepcopy(BASE_OVERRIDES)
    raw["retrieval"].update(retrieval_overrides)
    return Config(raw)


class LegacyCfg:
    """0.38.0 的 cfg 视图：新键一律不存在。"""

    def __init__(self, real: object) -> None:
        self._real = real

    def get(self, key: str, default: object = None) -> object:
        if key in NEW_KEYS:
            return default
        return self._real.get(key, default)  # type: ignore[attr-defined]


def _run_counted(store: object, query: str, cfg: object | None = None) -> tuple[dict, list[str]]:
    real_cfg = store.cfg
    if cfg is not None:
        store.cfg = cfg
    statements: list[str] = []
    conn = store.db.conn
    conn.set_trace_callback(statements.append)
    try:
        result = store.search(query, k=8, record=False)
    finally:
        conn.set_trace_callback(None)
        store.cfg = real_cfg
    return result, statements


def _payload(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "latency_ms"}


def _second(trigger: float | None = None) -> Config:
    overrides: dict = {"enabled": True}
    if trigger is not None:
        overrides["trigger_coverage"] = trigger
    return _config(second_query=overrides)


class SecondQueryTriggerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("second-trigger")
        self.app = PMem.open(Path(self._dir) / "pmem.db", config=_config())
        for text in SEED:
            self.app.store.add(text, session_id="s1")
        # 预热 + 取首遍覆盖率（不跑第二遍的配置）。
        self.base_result, _sql = _run_counted(self.app.store, QUERY)
        self.cov = float((self.base_result.get("uncertainty") or {}).get("coverage") or 0.0)

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_default_trigger_keeps_old_behavior(self) -> None:
        result = self.app.store.search(QUERY, k=8, record=False)  # 默认配置：second 关
        self.assertNotIn("second_query", result)
        cfg = _second()  # trigger 用 config 默认 1.01
        result, _sql = _run_counted(self.app.store, QUERY, cfg)
        self.assertIn("second_query", result, "默认 trigger=1.01 时第二遍应该照旧跑")

    def test_trigger_skips_when_coverage_reaches_threshold(self) -> None:
        trigger = max(0.0, self.cov - 0.01)  # cov_best >= trigger → 不跑
        skipped, skipped_sql = _run_counted(self.app.store, QUERY, _second(trigger))
        off, off_sql = _run_counted(self.app.store, QUERY)
        self.assertNotIn("second_query", skipped)
        self.assertEqual(
            json.dumps(_payload(skipped), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(off), ensure_ascii=False, sort_keys=True),
            "被 trigger 拦下时应与 second_query 关完全一致",
        )
        self.assertEqual(skipped_sql, off_sql, "被拦下时不该多发第二遍的 SQL")

    def test_trigger_allows_when_coverage_is_low(self) -> None:
        trigger = min(1.01, self.cov + 0.5)  # cov_best < trigger → 跑
        result, _sql = _run_counted(self.app.store, QUERY, _second(trigger))
        self.assertIn("second_query", result)
        self.assertGreaterEqual(int(result["second_query"].get("added") or 0), 0)

    def test_zero_trigger_never_runs(self) -> None:
        result, sql = _run_counted(self.app.store, QUERY, _second(0.0))
        off, off_sql = _run_counted(self.app.store, QUERY)
        self.assertNotIn("second_query", result)
        self.assertEqual(
            json.dumps(_payload(result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(off), ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(sql, off_sql)

    def test_off_equals_038_and_zero_extra_sql(self) -> None:
        self.app.store.search(QUERY, k=8, record=False)  # 预热
        real, real_sql = _run_counted(self.app.store, QUERY)
        legacy, legacy_sql = _run_counted(self.app.store, QUERY, LegacyCfg(self.app.store.cfg))
        self.assertEqual(real_sql, legacy_sql)
        self.assertEqual(
            json.dumps(_payload(real), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy), ensure_ascii=False, sort_keys=True),
        )


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
