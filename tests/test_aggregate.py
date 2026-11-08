"""aggregate 同主体枚举（P1-7，默认关）：问「各/分别/哪几」时补同主体的多条事实。

守五件事：

1. 默认关 + 关时与 0.38.0 等价（结构/SQL 都不变）；
2. 只有 ``query.plan(q)["kind"] == "aggregate"`` 才触发；普通 lookup 不触发；
3. 触发后从 ``subject_terms`` 找同主体事实，**只填空位、不重排**（首遍顺序一条不动）；
4. 补进来的条目 ``via="aggregate"``，开 ``budget_trace`` 时能看到 aggregate 阶段；
5. ``query.plan`` 还没落地/抛异常/返回坏结构时**优雅跳过**，绝不带崩检索
   （`getattr(query_mod, "plan", None)` 的降级路径）。
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import query as query_mod

from .support import drop_dir, temp_dir

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

SUBJECT_FACTS = [
    "李静在杭州当小学老师",
    "李静的老公是骨科医生",
    "李静有个上小学的儿子",
]
NOISE = ["用户喜欢喝手冲咖啡", "用户住在杭州，常去西湖散步"]

AGG_QUERY = "她朋友的工作分别是什么"
FAKE_PLAN = {
    "kind": "aggregate",
    "aggregate_terms": ["分别"],
    "subject_terms": ["李静"],
    "confidence": 0.8,
}


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


def _fake_plan(plan: object) -> mock._patch:
    return mock.patch.object(query_mod, "plan", plan, create=True)


class AggregateCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, **retrieval_overrides: object) -> PMem:
        path = temp_dir("aggregate")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=_config(**retrieval_overrides))
        self._apps.append(app)
        for text in SUBJECT_FACTS + NOISE:
            app.store.add(text, session_id="s1")
        return app


class OffByDefaultTest(AggregateCase):
    def test_off_equals_038_and_zero_extra_sql(self) -> None:
        app = self._open()
        app.store.search(AGG_QUERY, record=False)  # 预热
        real_result, real_sql = _run_counted(app.store, AGG_QUERY)
        legacy_result, legacy_sql = _run_counted(app.store, AGG_QUERY, LegacyCfg(app.store.cfg))
        self.assertEqual(real_sql, legacy_sql, "关时（相对 0.38.0）多发了 SQL")
        self.assertEqual(
            json.dumps(_payload(real_result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy_result), ensure_ascii=False, sort_keys=True),
        )

    def test_off_does_not_call_query_plan(self) -> None:
        app = self._open()
        called = []

        def _plan(question):  # noqa: ANN001, ANN202 - 测试替身
            called.append(question)
            return dict(FAKE_PLAN)

        with _fake_plan(_plan):
            app.store.search(AGG_QUERY, record=False)
        self.assertEqual(called, [], "默认关时不该调用 query.plan（省一次纯函数调用）")


class AggregateOnTest(AggregateCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self._open(aggregate={"enabled": True, "max_items": 10})

    def test_aggregate_fills_empty_slots_with_via(self) -> None:
        with _fake_plan(lambda question: dict(FAKE_PLAN)):
            result = self.app.store.search(AGG_QUERY, k=8, record=False)
        texts = [item["text"] for item in result["items"]]
        self.assertEqual(sorted(texts), sorted(SUBJECT_FACTS))
        for item in result["items"]:
            self.assertEqual(item.get("via"), "aggregate")

    def test_non_aggregate_plan_does_not_aggregate(self) -> None:
        lookup = {"kind": "lookup", "aggregate_terms": [], "subject_terms": ["李静"], "confidence": 0.3}
        with _fake_plan(lambda question: dict(lookup)):
            result = self.app.store.search(AGG_QUERY, k=8, record=False)
        self.assertEqual(result["items"], [])

    def test_aggregate_does_not_reorder_first_pass(self) -> None:
        off_app = self._open()
        off = off_app.store.search("手冲咖啡", k=8, record=False)
        with _fake_plan(lambda question: dict(FAKE_PLAN)):
            on = self.app.store.search("手冲咖啡", k=8, record=False)
        prefix = [item["text"] for item in on["items"][: len(off["items"])]]
        self.assertEqual(prefix, [item["text"] for item in off["items"]], "聚合重排了首遍结果")
        tail = on["items"][len(off["items"]) :]
        self.assertTrue(tail, "有空位时聚合应该补进同主体事实")
        self.assertTrue(all(item.get("via") == "aggregate" for item in tail))

    def test_full_slots_disable_aggregate(self) -> None:
        """槽位已满时 aggregate 一条都不添——「只填空位」是硬语义（Lead 裁决保留）。

        默认 k=8（bench 是 k=40）且首遍几乎总是装满，所以在 bench 口径下这条能力
        基本 inert；不计分，也不擅自改成置换（G2 已证置换会挤掉对的）。
        """
        app = self._open(aggregate={"enabled": True, "max_items": 10})
        for index in range(8):
            app.store.add(f"会议记录第 {index} 份：讨论项目排期和风险", session_id="s1")
        with _fake_plan(lambda question: dict(FAKE_PLAN)):
            result = app.store.search("会议记录", k=8, record=False)
        self.assertEqual(len(result["items"]), 8, "首遍应已占满 k 个槽位")
        self.assertTrue(
            all(item.get("via") != "aggregate" for item in result["items"]),
            "槽位已满时不该有 aggregate 条目（它必须先置换才能进来）",
        )
        texts = [item["text"] for item in result["items"]]
        self.assertTrue(
            all(text not in texts for text in SUBJECT_FACTS),
            "聚合候选不该挤进已满的结果",
        )

    def test_trace_records_aggregate_stage(self) -> None:
        app = self._open(budget_trace=True, aggregate={"enabled": True, "max_items": 10})
        with _fake_plan(lambda question: dict(FAKE_PLAN)):
            result = app.store.search(AGG_QUERY, k=8, record=False)
        stages = [entry["stage"] for entry in result["budget_trace"]]
        self.assertIn("aggregate", stages)
        entry = next(item for item in result["budget_trace"] if item["stage"] == "aggregate")
        self.assertEqual(entry["added"], len(result["items"]))
        self.assertGreater(entry["tokens"], 0)

    def test_missing_query_plan_degrades_gracefully(self) -> None:
        with _fake_plan(None):
            result = self.app.store.search(AGG_QUERY, k=8, record=False)
        self.assertEqual(result["items"], [])

    def test_raising_query_plan_degrades_gracefully(self) -> None:
        def _boom(question):  # noqa: ANN001, ANN202 - 测试替身
            raise RuntimeError("plan 还没准备好")

        with _fake_plan(_boom):
            result = self.app.store.search("手冲咖啡", k=8, record=False)
        self.assertTrue(result["items"], "plan 抛异常不能带崩主检索")

    def test_bad_plan_payload_degrades_gracefully(self) -> None:
        bad_plans = [
            {"kind": "aggregate", "subject_terms": "不是列表"},
            {"kind": "aggregate", "subject_terms": {"不是": "列表"}},
            "不是 dict",
            [],
        ]
        for bad in bad_plans:
            with _fake_plan(lambda question, _bad=bad: _bad):
                result = self.app.store.search(AGG_QUERY, k=8, record=False)
            self.assertEqual(result["items"], [], f"坏 plan 不该被当成聚合：{bad!r}")


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
