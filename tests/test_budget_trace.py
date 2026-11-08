"""budget_trace / item.via（P0-4，默认关）：预算去向要看得见，且关时 0.38.0 逐字节不变。

守四件事：

1. 默认关：返回里没有 ``budget_trace``，条目里没有 ``via``（结构不变）；
2. 打开：``budget_trace`` 是 ``[{stage, added, evicted, dropped, tokens}]``，首站必是 ``tok``，
   ``added`` 与条数、``tokens`` 与正文估算自洽；
3. ``item["via"]`` 每条都有、首遍是 ``tok``（其余阶段名见契约 §5.1）；
4. **关时零额外 SQL**：同一实例热身后，把 cfg 换成「没有这四个新键」的 0.38.0 视图，
   SQL 语句序列与返回体（除 latency_ms）必须逐字相同。
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem

from .support import drop_dir, temp_dir

# 关掉依赖机器状态的通道：测试只钉「新增开关关/开」这一层。
BASE_OVERRIDES: dict = {
    "retrieval": {
        "vector": {"enabled": False, "channel": False},
        "graph": {"append": False},
    }
}

NEW_KEYS = frozenset(
    {
        "retrieval.budget_trace",
        "retrieval.spans",
        "retrieval.aggregate",
        "retrieval.scope.project",
    }
)

# 契约 §5.1 的阶段名（episode/raw 之外的补充阶段都在这里；episode 是既有通道，
# 这次也纳入 trace，但属于对枚举的补充，见 task-14 汇报）。
ALLOWED_STAGES = frozenset(
    {
        "tok", "tri", "scope", "entity", "vector", "graph", "temporal", "raw",
        "assistant", "deepen", "second", "aggregate", "episode",
    }
)

SEED = [
    "用户喜欢喝手冲咖啡，不加糖",
    "用户常去一家手冲咖啡店",
    "用户住在杭州，常去西湖散步",
    "下周三要交财务报告",
]


def _config(**retrieval_overrides: object) -> Config:
    raw = copy.deepcopy(BASE_OVERRIDES)
    raw["retrieval"].update(retrieval_overrides)
    return Config(raw)


class LegacyCfg:
    """0.38.0 的 cfg 视图：新键一律不存在（``get`` 走调用方给的 default）。"""

    def __init__(self, real: object) -> None:
        self._real = real

    def get(self, key: str, default: object = None) -> object:
        if key in NEW_KEYS:
            return default
        return self._real.get(key, default)  # type: ignore[attr-defined]


def _run_counted(store: object, query: str, cfg: object | None = None) -> tuple[dict, list[str]]:
    """跑一次检索并记下 sqlite 实际执行的语句（set_trace_callback，只读仪表）。"""
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
    """去掉每次都会变的延迟，只比结构与内容。"""
    return {key: value for key, value in result.items() if key != "latency_ms"}


class BudgetTraceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, **retrieval_overrides: object) -> PMem:
        path = temp_dir("budget-trace")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=_config(**retrieval_overrides))
        self._apps.append(app)
        for text in SEED:
            app.store.add(text, session_id="s1")
        return app


class OffByDefaultTest(BudgetTraceCase):
    def test_no_trace_key_and_no_via_when_off(self) -> None:
        app = self._open()
        result = app.store.search("手冲咖啡", k=8, record=False)
        self.assertNotIn("budget_trace", result, "默认关时不该出现 budget_trace")
        for item in result["items"]:
            self.assertNotIn("via", item, f"默认关时 item 结构里不该多出 via：{item['text']}")

    def test_off_equals_038_and_zero_extra_sql(self) -> None:
        app = self._open()
        # 预热：df 缓存/图等进程内缓存填满，否则「第一次」的额外查询会被误算到开关头上。
        app.store.search("手冲咖啡", k=8, record=False)
        real_result, real_sql = _run_counted(app.store, "手冲咖啡")
        legacy_result, legacy_sql = _run_counted(app.store, "手冲咖啡", LegacyCfg(app.store.cfg))
        self.assertEqual(real_sql, legacy_sql, "关时（相对 0.38.0）多发了 SQL")
        self.assertEqual(
            json.dumps(_payload(real_result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy_result), ensure_ascii=False, sort_keys=True),
            "关时返回结构与 0.38.0 不一致",
        )


class TraceOnTest(BudgetTraceCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self._open(budget_trace=True)

    def test_trace_shape_and_first_stage(self) -> None:
        result = self.app.store.search("手冲咖啡", k=8, record=False)
        trace = result["budget_trace"]
        self.assertIsInstance(trace, list)
        self.assertTrue(trace, "打开后至少要有首遍 tok 一条")
        first = trace[0]
        self.assertEqual(first["stage"], "tok")
        self.assertEqual(set(first), {"stage", "added", "evicted", "dropped", "tokens"})
        self.assertEqual(first["added"], len(result["items"]))
        self.assertGreater(first["tokens"], 0)
        for entry in trace:
            self.assertIn(entry["stage"], ALLOWED_STAGES)
            for key in ("added", "evicted", "dropped", "tokens"):
                self.assertIsInstance(entry[key], int)
                self.assertGreaterEqual(entry[key], 0)

    def test_items_carry_via_with_tok_first(self) -> None:
        result = self.app.store.search("手冲咖啡", k=8, record=False)
        self.assertTrue(result["items"])
        for item in result["items"]:
            self.assertIn(item.get("via"), ALLOWED_STAGES, item["text"])
        self.assertEqual(result["items"][0]["via"], "tok")

    def test_trace_does_not_change_selection(self) -> None:
        off_app = self._open()
        off = off_app.store.search("手冲咖啡", k=8, record=False)
        on = self.app.store.search("手冲咖啡", k=8, record=False)
        self.assertEqual(
            [item["text"] for item in on["items"]],
            [item["text"] for item in off["items"]],
            "打 trace 不能改变选出来的条目与顺序",
        )
        self.assertEqual(on["budget_used"], off["budget_used"])


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
