"""project scope 过滤（P3-17，默认关）：把检索限定在当前项目的 grp 路径前缀下。

守五件事：

1. 默认关（或 ``name`` 为空）时行为与 0.38.0 **逐字节等价**（结构/SQL 都不变）；
2. 打开且 ``name`` 非空：按 grp 路径前缀过滤候选（子树包含），未分组的记忆不返回；
3. ``name`` 是路径前缀：「项目」包含「项目/Alpha」与「项目/Beta」，「项目/Alpha」只含它自己；
4. 调用方自己传了 ``filters["group"]`` 时尊重调用方（不覆盖）；
5. 关时零额外 SQL（同一实例热身后与「没有新键的 0.38.0 视图」逐条对比语句序列）。
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
NEW_KEYS = frozenset(
    {
        "retrieval.budget_trace",
        "retrieval.spans",
        "retrieval.aggregate",
        "retrieval.scope.project",
    }
)

QUERY = "接口设计"
ALPHA = "接口设计用 REST，Alpha 项目"
BETA = "接口设计用 GraphQL，Beta 项目"
UNGROUPED = "接口设计还在讨论，没归档"


def _config(project: dict | None = None) -> Config:
    raw = copy.deepcopy(BASE_OVERRIDES)
    if project is not None:
        raw["retrieval"]["scope"] = {"project": project}
    return Config(raw)


class LegacyCfg:
    """0.38.0 的 cfg 视图：新键一律不存在。"""

    def __init__(self, real: object) -> None:
        self._real = real

    def get(self, key: str, default: object = None) -> object:
        if key in NEW_KEYS:
            return default
        return self._real.get(key, default)  # type: ignore[attr-defined]


def _run_counted(store: object, query: str, cfg: object | None = None,
                 filters: dict | None = None) -> tuple[dict, list[str]]:
    real_cfg = store.cfg
    if cfg is not None:
        store.cfg = cfg
    statements: list[str] = []
    conn = store.db.conn
    conn.set_trace_callback(statements.append)
    try:
        result = store.search(query, k=8, record=False, filters=filters)
    finally:
        conn.set_trace_callback(None)
        store.cfg = real_cfg
    return result, statements


def _payload(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "latency_ms"}


class ProjectScopeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, project: dict | None = None) -> PMem:
        path = temp_dir("scope-project")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=_config(project))
        self._apps.append(app)
        app.store.add(ALPHA, groups=["项目/Alpha"], session_id="s1")
        app.store.add(BETA, groups=["项目/Beta"], session_id="s1")
        app.store.add(UNGROUPED, session_id="s1")
        return app

    @staticmethod
    def _texts(result: dict) -> list[str]:
        return [item["text"] for item in result["items"]]


class OffTest(ProjectScopeCase):
    def test_disabled_returns_everything(self) -> None:
        app = self._open({"enabled": False, "name": "项目/Alpha"})
        texts = self._texts(app.store.search(QUERY, k=8, record=False))
        self.assertEqual(sorted(texts), sorted([ALPHA, BETA, UNGROUPED]))

    def test_empty_name_equals_off(self) -> None:
        app = self._open()
        off_cfg = _config({"enabled": False, "name": ""})
        on_cfg = _config({"enabled": True, "name": ""})
        off_result, _off_sql = _run_counted(app.store, QUERY, off_cfg)
        on_result, _on_sql = _run_counted(app.store, QUERY, on_cfg)
        self.assertEqual(
            json.dumps(_payload(off_result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(on_result), ensure_ascii=False, sort_keys=True),
            "name 为空时应与关完全等价",
        )

    def test_off_equals_038_and_zero_extra_sql(self) -> None:
        app = self._open()
        app.store.search(QUERY, k=8, record=False)  # 预热
        real_result, real_sql = _run_counted(app.store, QUERY)
        legacy_result, legacy_sql = _run_counted(app.store, QUERY, LegacyCfg(app.store.cfg))
        self.assertEqual(real_sql, legacy_sql, "关时（相对 0.38.0）多发了 SQL")
        self.assertEqual(
            json.dumps(_payload(real_result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy_result), ensure_ascii=False, sort_keys=True),
        )


class OnTest(ProjectScopeCase):
    def test_filters_by_exact_project_path(self) -> None:
        app = self._open({"enabled": True, "name": "项目/Alpha"})
        texts = self._texts(app.store.search(QUERY, k=8, record=False))
        self.assertEqual(texts, [ALPHA], "只应返回 Alpha 项目的记忆")

    def test_filter_is_a_path_prefix(self) -> None:
        app = self._open({"enabled": True, "name": "项目"})
        texts = self._texts(app.store.search(QUERY, k=8, record=False))
        self.assertEqual(sorted(texts), sorted([ALPHA, BETA]), "前缀应包含两个子项目、排除未分组")

    def test_caller_group_filter_wins(self) -> None:
        app = self._open({"enabled": True, "name": "项目/Alpha"})
        texts = self._texts(
            app.store.search(QUERY, k=8, record=False, filters={"group": "项目/Beta"})
        )
        self.assertEqual(texts, [BETA], "调用方的 filters['group'] 不该被项目范围覆盖")


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
