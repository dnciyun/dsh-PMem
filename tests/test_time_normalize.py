"""写时时间归一（P2-16）：明确说法落成绝对日期，模糊词不猜，原文不动。

守五件事：

1. 关（默认）：fact 结构与 0.38.0 一致——result 里没有 ``time_normalized`` 键，
   memory.extra_json 里没有 ``time_expr`` / ``time_abs``；
2. 开：明确相对说法（上周五/下周一/三天前/半个月前）按**这一行的 ts** 换算；
3. 明确日期（3月27日 / 2026-03-27）同样换算；没写年份按「最近一次」推；
4. 模糊词（上次/那次/以前）不猜；2 月 30 日这种不存在的日期只留 ``time_expr``；
5. ``result["facts"]`` 与 journal_line 原文一个字都不改。
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.gate.sdk import PMem
from pmem.memory import journal

from .support import drop_dir, temp_dir

# 2026-05-03 是周日：中文口语「上周五」按周日起算 = 2026-05-01（timeresolve 单测同口径）。
T0 = datetime(2026, 5, 3, 21, 0, tzinfo=UTC)
TS = T0.isoformat()


class TimeNormalizeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("time-normalize")
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        drop_dir(self._dir)

    def _open(self, enabled: bool) -> tuple[PMem, Config]:
        cfg = Config(
            {"journal": {"time_normalize": {"enabled": enabled}, "min_lines": 1}}
        )
        app = PMem.open(Path(self._dir) / "pmem.db", config=cfg)
        return app, cfg

    def _digest(self, app: PMem, cfg: Config, text: str) -> dict:
        journal.append(app.db, text, role="user", session_id="s1", ts=TS)
        return journal.digest(app.db, store=app.store, config=cfg)

    def _extra(self, app: PMem, memory_id: str) -> dict:
        row = app.db.conn.execute(
            "SELECT extra_json FROM memory WHERE id=?", (memory_id,)
        ).fetchone()
        return json.loads(row["extra_json"])

    def test_off_matches_038(self) -> None:
        app, cfg = self._open(False)
        try:
            result = self._digest(app, cfg, "我上周五去游泳了")
            self.assertEqual(result["facts"], ["用户上周五去游泳了"])
            self.assertNotIn("time_normalized", result)
            extra = self._extra(app, result["memory_ids"][0])
            self.assertNotIn("time_expr", extra)
            self.assertNotIn("time_abs", extra)
        finally:
            app.close()

    def test_relative_weekday(self) -> None:
        app, cfg = self._open(True)
        try:
            result = self._digest(app, cfg, "我上周五去游泳了")
            self.assertEqual(
                result["time_normalized"],
                [{"index": 0, "time_expr": "上周五", "time_abs": "2026-05-01"}],
            )
            extra = self._extra(app, result["memory_ids"][0])
            self.assertEqual(extra["time_expr"], "上周五")
            self.assertEqual(extra["time_abs"], "2026-05-01")
        finally:
            app.close()

    def test_message_text_unchanged(self) -> None:
        app, cfg = self._open(True)
        try:
            result = self._digest(app, cfg, "我上周五去游泳了")
            self.assertEqual(result["facts"], ["用户上周五去游泳了"])
            line = app.db.conn.execute(
                "SELECT text FROM journal_line ORDER BY seq LIMIT 1"
            ).fetchone()
            self.assertEqual(line["text"], "我上周五去游泳了")
            row = app.db.conn.execute(
                "SELECT text_raw FROM memory WHERE id=?", (result["memory_ids"][0],)
            ).fetchone()
            self.assertEqual(row["text_raw"], "用户上周五去游泳了")
        finally:
            app.close()

    def test_explicit_date_yearless(self) -> None:
        app, cfg = self._open(True)
        try:
            result = self._digest(app, cfg, "我3月27日去游泳了")
            self.assertEqual(result["time_normalized"][0]["time_expr"], "3月27日")
            self.assertEqual(result["time_normalized"][0]["time_abs"], "2026-03-27")
        finally:
            app.close()

    def test_explicit_date_with_year(self) -> None:
        app, cfg = self._open(True)
        try:
            result = self._digest(app, cfg, "我2026-03-27去游泳了")
            self.assertEqual(result["time_normalized"][0]["time_abs"], "2026-03-27")
        finally:
            app.close()

    def test_invalid_date_keeps_expr_only(self) -> None:
        app, cfg = self._open(True)
        try:
            result = self._digest(app, cfg, "我2月30日去游泳了")
            entry = result["time_normalized"][0]
            self.assertEqual(entry["time_expr"], "2月30日")
            self.assertNotIn("time_abs", entry)
            extra = self._extra(app, result["memory_ids"][0])
            self.assertNotIn("time_abs", extra)
        finally:
            app.close()

    def test_vague_words_not_guessed(self) -> None:
        app, cfg = self._open(True)
        try:
            # 四句一次消化：模糊词一个都不许被猜成日期。
            for text in ("我上次去游泳了", "我那次去游泳了", "我以后再去游泳", "我以前常游泳"):
                journal.append(app.db, text, role="user", session_id="s1", ts=TS)
            result = journal.digest(app.db, store=app.store, config=cfg)
            self.assertEqual(len(result["facts"]), 4)
            self.assertNotIn("time_normalized", result)
        finally:
            app.close()

    def test_half_month(self) -> None:
        app, cfg = self._open(True)
        try:
            result = self._digest(app, cfg, "我半个月前去了杭州")
            self.assertEqual(result["time_normalized"][0]["time_expr"], "半个月前")
            self.assertEqual(result["time_normalized"][0]["time_abs"], "2026-04-18")
        finally:
            app.close()

    def test_normalize_helper_direct(self) -> None:
        self.assertEqual(
            journal._normalize_time("上周五下班", TS),
            {"time_expr": "上周五", "time_abs": "2026-05-01"},
        )
        self.assertEqual(
            journal._normalize_time("3月27日那天", TS),
            {"time_expr": "3月27日", "time_abs": "2026-03-27"},
        )
        self.assertIsNone(journal._normalize_time("上次那件事", TS))
        self.assertEqual(
            journal._normalize_time("2月30日", TS), {"time_expr": "2月30日"}
        )

    def test_enabled_flag_reader(self) -> None:
        self.assertTrue(journal._time_normalize_enabled(
            Config({"journal": {"time_normalize": {"enabled": True}}})
        ))
        self.assertFalse(journal._time_normalize_enabled(Config()))


if __name__ == "__main__":
    unittest.main()
