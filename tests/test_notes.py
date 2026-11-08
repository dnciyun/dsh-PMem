"""常驻三栏笔记（1.12.0，默认关）的验收测试。

守四件事（对应 task-50 验收）：

1. **关时逐字节一致**：``notes.enabled=false`` 时 wake 输出与没有笔记层时完全一样；
   即使库里已经写入三栏，关着也不会多一个字、tokens 不变。
2. 渲染规则：三栏按 config 字符上限截断、空栏不输出、全空返回空串。
3. prep/apply：任务书带「不许把没发生的事写成已发生 / 不许编日期与专名」；
   apply 校验后写回、推进 line_seq（watermark），空栏不覆盖旧值。
4. 迁移：0.40.0 旧库（1.11.0）打开后自动建 ``state_note``、版本升到 1.12.0，旧数据可读。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.errors import ValidationError
from pmem.gate.sdk import PMem
from pmem.memory import journal as journal_mod
from pmem.memory import notes

from .support import temp_dir, drop_dir

BENCH_SNAP = Path(r"F:\Ai\chat-memory-benchmark\outputs\pmem\work\P01\snap\43.db")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._dirs: list[Path] = []
        self._soul = temp_dir("notes-soul")
        self._old_soul = os.environ.get("PMEM_SOUL_DIR")
        os.environ["PMEM_SOUL_DIR"] = str(self._soul)
        clock.set_now("2026-11-08T12:00:00+08:00")
        self._apps: list[PMem] = []

    def tearDown(self) -> None:
        clock.reset_now()
        for app in self._apps:
            try:
                app.close()
            except Exception:  # noqa: BLE001
                pass
        if self._old_soul is None:
            os.environ.pop("PMEM_SOUL_DIR", None)
        else:
            os.environ["PMEM_SOUL_DIR"] = self._old_soul
        for path in self._dirs:
            drop_dir(path)
        drop_dir(self._soul)

    def _app(self, notes_cfg: dict | None = None, **extra: dict) -> PMem:
        path = temp_dir("notes-db")
        self._dirs.append(path)
        data = dict(extra)
        if notes_cfg is not None:
            data["notes"] = notes_cfg
        app = PMem.open(path / "pmem.db", config=Config(data or None))
        self._apps.append(app)
        return app

    def _seed(self, app: PMem) -> None:
        journal_mod.append(
            app.db, "明天陪爸去社区医院开药，说好我九点到", subject=app.subject,
            session_id="s:1", role="user", ts="2026-11-07T20:00:00+08:00",
        )
        journal_mod.append(
            app.db, "答应的三件事还差两件，这周末补", subject=app.subject,
            session_id="s:1", role="user", ts="2026-11-07T20:01:00+08:00",
        )


class NotesRenderCase(_Base):
    def test_render_empty_when_disabled_even_with_rows(self) -> None:
        app = self._app({"enabled": False})
        notes.put(app.db, "about_user", "喜欢喝热麦茶", 1)
        self.assertEqual(notes.render(app.db, app.config), "")

    def test_render_segments_and_truncation(self) -> None:
        app = self._app({"enabled": True, "user_chars": 10, "self_chars": 800, "open_chars": 600})
        notes.put(app.db, "about_user", "这是一个很长很长的关于你的描述必须被截断", 3)
        notes.put(app.db, "open", "还欠三件事", 3)
        text = notes.render(app.db, app.config)
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)  # 关于我 空 → 不输出
        self.assertTrue(lines[0].startswith("关于你："))
        self.assertTrue(lines[0].endswith("…"))
        self.assertLessEqual(len(lines[0].split("：", 1)[1]), 10)
        self.assertEqual(lines[1], "还没了的事：还欠三件事")

    def test_render_all_empty_returns_blank(self) -> None:
        app = self._app({"enabled": True})
        self.assertEqual(notes.render(app.db, app.config), "")


class NotesPrepApplyCase(_Base):
    def test_prep_disabled_touches_nothing(self) -> None:
        app = self._app({"enabled": False})
        data = notes.prep(app.db, config=app.config)
        self.assertFalse(data["enabled"])
        self.assertEqual(data["material"], [])

    def test_prep_task_has_forbidden_rules(self) -> None:
        app = self._app({"enabled": True, "refresh_lines": 40})
        self._seed(app)
        data = notes.prep(app.db, config=app.config)
        self.assertTrue(data["enabled"])
        self.assertTrue(data["needed"])
        self.assertEqual(len(data["material"]), 2)
        task = data["task"]
        for needle in ("不许把没发生的事写成已发生", "不许编日期", "不许编日期、编专名" if False else "编专名", "没把握就空着"):
            self.assertIn(needle, task)
        self.assertIn("关于你", task)

    def test_apply_writes_and_advances_watermark(self) -> None:
        app = self._app({"enabled": True})
        self._seed(app)
        data = notes.apply(
            app.db,
            payload={"about_user": "要陪父亲去社区医院开药，约九点到。",
                     "about_self": "", "open": "答应的三件事还差两件。", "line_seq": 2},
            config=app.config,
        )
        self.assertEqual(data["changed"], 2)
        self.assertEqual(notes.latest_line_seq(app.db), 2)
        state = notes.get(app.db)
        self.assertIn("about_user", state)
        self.assertNotIn("about_self", state)
        # 第二次整理：空栏不覆盖旧值
        again = notes.apply(
            app.db,
            payload={"about_user": "", "about_self": "我不替用户做决定。", "open": ""},
            config=app.config,
            line_seq=4,
        )
        self.assertEqual(sorted(again["updated"]), ["about_self"])
        state2 = notes.get(app.db)
        self.assertEqual(state2["about_user"]["text"], state["about_user"]["text"])
        self.assertEqual(notes.latest_line_seq(app.db), 4)
        # 水位推进后 prep 不应再给同一批流水
        again_prep = notes.prep(app.db, config=app.config)
        self.assertFalse(again_prep.get("needed", False))

    def test_apply_rejects_bad_payload(self) -> None:
        app = self._app({"enabled": True})
        with self.assertRaises(ValidationError):
            notes.apply(app.db, payload={"about_user": ["不是字符串"]}, config=app.config)
        with self.assertRaises(ValidationError):
            notes.apply(app.db, payload={}, config=app.config)  # 空对象也算没写东西
        self.assertEqual(notes.get(app.db), {})


class NotesWakeCase(_Base):
    def _strip(self, data: dict) -> dict:
        clone = dict(data)
        clone.pop("built_at", None)
        return clone

    def test_wake_byte_identical_when_disabled(self) -> None:
        app = self._app({"enabled": False})
        self._seed(app)
        before = app.wake(budget=300)["data"]
        notes.put(app.db, "about_user", "喜欢热麦茶", 2)
        notes.put(app.db, "open", "还欠三件事", 2)
        after = app.wake(budget=300)["data"]
        self.assertEqual(json.dumps(self._strip(before), ensure_ascii=False, sort_keys=True),
                         json.dumps(self._strip(after), ensure_ascii=False, sort_keys=True))
        self.assertEqual(before["tokens"], after["tokens"])

    def test_wake_injects_notes_when_enabled(self) -> None:
        app = self._app({"enabled": True})
        self._seed(app)
        base = app.wake(budget=300)["data"]
        notes.put(app.db, "about_user", "要陪父亲去社区医院开药，约九点到", 2)
        notes.put(app.db, "open", "答应的三件事还差两件", 2)
        data = app.wake(budget=300)["data"]
        self.assertIn("关于你：", data["text"])
        self.assertIn("还没了的事：", data["text"])
        self.assertIn("关于你：", data["live"])
        self.assertGreater(data["tokens"], base["tokens"])
        self.assertGreater(data["tokens_live"], 0)

    def test_person_cards_render_and_inject(self) -> None:
        app = self._app({"enabled": True, "person_cards": 1})
        stamp = "2026-11-07T20:00:00+08:00"
        with app.db.tx() as conn:
            conn.execute(
                "INSERT INTO entity(id, subject_id, type, name, name_norm, status, confidence,"
                " provenance, first_seen, last_seen, mention_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("e1", "self", "person", "周宁", "周宁", "active", 0.9, "test", stamp, stamp, 5),
            )
            conn.execute(
                "INSERT INTO person_card(entity_id, relation, facts, boundary, contested, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                ("e1", "同事", json.dumps([{"predicate": "膝盖", "object": "雨天发酸",
                                           "polarity": 1}, {"predicate": "喜欢", "object": "美式",
                                                            "polarity": 1}], ensure_ascii=False),
                 "[]", "[]", stamp),
            )
        cards = notes.render_person_cards(app.db, app.config)
        self.assertIn("周宁（同事）", cards)
        self.assertIn("膝盖雨天发酸", cards)
        data = app.wake(budget=300)["data"]
        self.assertIn("人物卡：", data["text"])


class NotesMigrationCase(_Base):
    def test_old_snapshot_auto_upgrades_to_1_12_0(self) -> None:
        if not BENCH_SNAP.exists():
            self.skipTest("基准仓快照不存在")
        target = temp_dir("notes-migrate") / "snap.db"
        self._dirs.append(target.parent)
        shutil.copy2(BENCH_SNAP, target)
        con = sqlite3.connect(str(target))
        tables_before = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version_before = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        self.assertNotIn("state_note", tables_before)
        app = PMem.open(target, config=Config())
        self._apps.append(app)
        con = sqlite3.connect(str(target))
        tables_after = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version_after = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        mem_after = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        con.close()
        self.assertIn("state_note", tables_after)
        self.assertIn(version_before, ("1.11.0", "1.10.0"))  # 0.40 之前的旧库都可能
        self.assertEqual(version_after, "1.12.0")
        self.assertGreater(mem_after, 0)
        # 旧库上的笔记流程也能用
        notes.put(app.db, "about_user", "旧库也能写笔记", 1)
        enabled_cfg = Config({"notes": {"enabled": True}})
        self.assertIn("关于你", notes.render(app.db, enabled_cfg))


if __name__ == "__main__":
    unittest.main()
