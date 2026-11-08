"""写入侧主体归属（P1-8）：开关关时与 0.38.0 一致，开时只挂已抽取实体。

守四件事：

1. 关（默认）：派生 fact 与 0.38.0 逐字一致——``result["facts"]`` 还是字符串列表，
   没有 ``subject_links`` 键，``extra_json`` 里没有主体字段；
2. 开：fact 挂上该行已抽取实体的 id/名字，且**不改写原文**；
3. 只复用已有实体（entity / entity_alias），不新增规则表；单字实体、被更长名字
   覆盖的短名不挂；
4. 实体表为空/坏时不炸，只是不挂（写日记不能被它挡住）。
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

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class SubjectLinksCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("subject-links")
        self.config = Config(
            {"journal": {"subject_links": {"enabled": True}, "min_lines": 1}}
        )
        self.app = PMem.open(Path(self._dir) / "pmem.db", config=self.config)
        self.db = self.app.db
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def _seed_entity(self) -> None:
        # 先写一条能被确定性抽取器认出实体的记忆，让 entity 表里有「王小明 / 杭州」。
        self.store.add("我和王小明去杭州", provenance="user_explicit")

    def _digest(self, config: Config | None = None) -> dict:
        journal.append(
            self.db,
            "我和王小明去杭州了",
            role="user",
            session_id="s1",
            ts=T0.isoformat(),
        )
        return journal.digest(self.db, store=self.store, config=config or self.config)

    def test_disabled_matches_038(self) -> None:
        # 默认配置（关了）：结构、正文都与 0.38.0 一致。
        plain = Config({"journal": {"min_lines": 1}})
        result = self._digest(config=plain)
        self.assertEqual(result["facts"], ["用户和王小明去杭州了"])
        self.assertNotIn("subject_links", result)
        for memory_id in result["memory_ids"]:
            row = self.db.conn.execute(
                "SELECT extra_json FROM memory WHERE id=?", (memory_id,)
            ).fetchone()
            extra = json.loads(row["extra_json"])
            self.assertNotIn("subject_entity_ids", extra)
            self.assertNotIn("subject", extra)

    def test_enabled_attaches_entities(self) -> None:
        self._seed_entity()
        result = self._digest()
        self.assertEqual(result["facts"], ["用户和王小明去杭州了"])  # 原文不改写
        links = result["subject_links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["index"], 0)
        self.assertEqual(links[0]["subject"], "王小明")  # 人优先于地点
        self.assertTrue(links[0]["subject_entity_ids"])
        # 落库的 extra_json 里也有（检索/契约层之后能读到）。
        memory_id = result["memory_ids"][0]
        row = self.db.conn.execute(
            "SELECT extra_json FROM memory WHERE id=?", (memory_id,)
        ).fetchone()
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["subject"], "王小明")
        self.assertEqual(extra["subject_entity_ids"], links[0]["subject_entity_ids"])

    def test_enabled_without_known_entity(self) -> None:
        # 库里没有实体：开也不挂，且不炸。
        result = self._digest()
        self.assertNotIn("subject_links", result)
        self.assertEqual(result["facts"], ["用户和王小明去杭州了"])

    def test_match_prefers_longer_name(self) -> None:
        entities = [
            ("王小明", "e2", "person", 0.4),
            ("小明", "e1", "person", 0.4),
            ("杭州", "e3", "place", 0.9),
        ]
        links = journal._match_subject_links("用户和王小明去杭州", entities)
        self.assertIsNotNone(links)
        assert links is not None
        self.assertEqual(links["subject"], "王小明")
        self.assertIn("e2", links["subject_entity_ids"])
        self.assertNotIn("e1", links["subject_entity_ids"])  # 短名被更长名覆盖

    def test_match_skips_single_char_entity(self) -> None:
        self.assertIsNone(journal._match_subject_links("用户去家", [("家", "e1", "place", 0.9)]))

    def test_match_prefers_person_over_place(self) -> None:
        entities = [("杭州", "p1", "place", 0.99), ("王小明", "p2", "person", 0.2)]
        links = journal._match_subject_links("用户和王小明去杭州", entities)
        assert links is not None
        self.assertEqual(links["subject"], "王小明")

    def test_known_entities_broken_table_is_safe(self) -> None:
        self.db.conn.execute("DROP TABLE entity_alias")
        self.assertEqual(journal._known_entities(self.db, "self"), [])

    def test_disabled_does_not_query_entities(self) -> None:
        # 关时连实体表都不该查：把表删掉也必须能正常写日记。
        self.db.conn.execute("DROP TABLE entity")
        self.db.conn.execute("DROP TABLE entity_alias")
        plain = Config({"journal": {"min_lines": 1}})
        result = self._digest(config=plain)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["facts"]), 1)


if __name__ == "__main__":
    unittest.main()
