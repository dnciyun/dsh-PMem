"""spans 原文展开（P2-13，默认关）：给条目附 source_span，只读、不改正文。

守四件事：

1. 默认关：条目里没有 ``source_span`` / ``source_line_id``，且相对 0.38.0 **零额外 SQL**；
2. 打开：journal 来源的条目带 ``source_line_id``（就是 journal_line.id）与
   ``source_span``（原文前 ``max_chars`` 字），**``item["text"]`` 一个字都不改**；
3. ``max_chars`` 生效（短上限时截断，不是整段塞）；
4. 只读：span 展开不写库（开/关跑完，journal_line 行数与内容不变）。
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal

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

JOURNAL_TEXT = "我昨天在河边钓到一条三斤的大鲤鱼，回家炖了汤"
QUERY = "河边 钓到 鲤鱼"


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
        result = store.search(query, record=False)
    finally:
        conn.set_trace_callback(None)
        store.cfg = real_cfg
    return result, statements


def _payload(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "latency_ms"}


class SpansCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        for app in self._apps:
            app.close()
        for path in self._dirs:
            drop_dir(path)

    def _open(self, **retrieval_overrides: object) -> tuple[PMem, str]:
        path = temp_dir("spans")
        self._dirs.append(path)
        app = PMem.open(path / "pmem.db", config=_config(**retrieval_overrides))
        self._apps.append(app)
        appended = journal.append(
            app.db,
            JOURNAL_TEXT,
            role="user",
            session_id="s1",
            ts="2026-11-05T09:00:00+00:00",
        )
        self.assertTrue(appended["ok"], appended)
        return app, appended["id"]

    def _journal_line(self, app: PMem) -> dict:
        row = app.db.conn.execute(
            "SELECT id, text, day_key FROM journal_line ORDER BY seq LIMIT 1"
        ).fetchone()
        return {"id": row["id"], "text": row["text"], "day_key": row["day_key"]}


class SpansOffTest(SpansCase):
    def test_default_off_has_no_span_fields(self) -> None:
        app, _line_id = self._open()
        result = app.store.search(QUERY, record=False)
        self.assertTrue(result["items"], "本测试依赖原话兜底，至少应召回一条 journal 条目")
        for item in result["items"]:
            self.assertNotIn("source_span", item)
            self.assertNotIn("source_line_id", item)

    def test_off_equals_038_and_zero_extra_sql(self) -> None:
        app, _line_id = self._open()
        app.store.search(QUERY, record=False)  # 预热
        real_result, real_sql = _run_counted(app.store, QUERY)
        legacy_result, legacy_sql = _run_counted(app.store, QUERY, LegacyCfg(app.store.cfg))
        self.assertEqual(real_sql, legacy_sql, "关时（相对 0.38.0）多发了 SQL")
        self.assertEqual(
            json.dumps(_payload(real_result), ensure_ascii=False, sort_keys=True),
            json.dumps(_payload(legacy_result), ensure_ascii=False, sort_keys=True),
        )


class SpansOnTest(SpansCase):
    def test_source_span_attached_and_text_untouched(self) -> None:
        app, line_id = self._open(spans={"enabled": True, "max_chars": 8})
        line = self._journal_line(app)
        result = app.store.search(QUERY, record=False)
        items = [item for item in result["items"] if str(item.get("source")) == "journal"]
        self.assertTrue(items, "原话兜底没有返回 journal 条目")
        item = items[0]
        self.assertEqual(item["source_line_id"], line_id)
        self.assertEqual(item["source_span"], line["text"][:8])
        self.assertEqual(len(item["source_span"]), 8)
        # 正文一个字都不改：source_span 只是附注，不是替换。
        self.assertEqual(item["text"], line["text"])
        self.assertNotIn("via", item, "只开 spans 时不该顺带打 item.via")

    def test_max_chars_is_a_hard_cap(self) -> None:
        app, _line_id = self._open(spans={"enabled": True, "max_chars": 3})
        result = app.store.search(QUERY, record=False)
        spans = [item["source_span"] for item in result["items"] if item.get("source_span")]
        self.assertTrue(spans)
        for span in spans:
            self.assertLessEqual(len(span), 3)

    def test_span_expansion_is_read_only_and_adds_sql(self) -> None:
        app, _line_id = self._open(spans={"enabled": True, "max_chars": 120})
        before = [
            (row["id"], row["text"])
            for row in app.db.conn.execute("SELECT id, text FROM journal_line ORDER BY seq")
        ]
        off_app, _off_line = self._open()
        off_app.store.search(QUERY, record=False)
        _off_result, off_sql = _run_counted(off_app.store, QUERY)
        on_result, on_sql = _run_counted(app.store, QUERY)
        after = [
            (row["id"], row["text"])
            for row in app.db.conn.execute("SELECT id, text FROM journal_line ORDER BY seq")
        ]
        self.assertEqual(before, after, "span 展开写库了（应只读）")
        self.assertTrue([item for item in on_result["items"] if item.get("source_span")])
        self.assertGreater(len(on_sql), len(off_sql), "打开 spans 后应该多出只读原文的那条 SQL")


if __name__ == "__main__":  # pragma: no cover - 方便单独调试
    unittest.main()
