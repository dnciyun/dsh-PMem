"""确定性写入兜底：名字 / 生日 / 城市 / 工作 / 家人。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pmem.gate.sdk import PMem
from pmem.memory import autowrite

from .support import drop_dir, temp_dir


class TestExtract(unittest.TestCase):
    def rules(self, text: str) -> list[tuple[str, str]]:
        return [(item["rule"], item["text"]) for item in autowrite.extract(text)]

    def test_name(self) -> None:
        self.assertEqual(self.rules("早，我叫林野。"), [("name", "用户叫林野")])

    def test_chinese_birthday(self) -> None:
        self.assertEqual(
            self.rules("我生日是一月二十三号，不是二十四号。"), [("birthday", "用户的生日是1月23日")]
        )

    def test_city_and_job(self) -> None:
        got = dict(self.rules("我在杭州做前端开发。"))
        self.assertEqual(got.get("city"), "用户在杭州")
        self.assertEqual(got.get("job"), "用户做前端")

    def test_city_from_plain_statement(self) -> None:
        self.assertEqual(self.rules("我在杭州生活了六年。"), [("city", "用户在杭州")])

    def test_family(self) -> None:
        self.assertEqual(self.rules("我妈叫王秀兰。"), [("family", "用户的妈叫王秀兰")])

    def test_questions_are_not_facts(self) -> None:
        for question in ("我叫什么？", "我生日是几号？", "我是做什么工作的？"):
            self.assertEqual(self.rules(question), [], f"{question} 被当成了事实")

    def test_ordinary_chat_yields_nothing(self) -> None:
        for line in ("今天写了一天表单，眼睛快瞎了。", "跑完五公里，风挺大。", "豆豆今天很乖。"):
            self.assertEqual(self.rules(line), [])

    def test_name_does_not_swallow_whole_sentence(self) -> None:
        got = self.rules("我叫林野，希望被称呼为林野，别叫先生")
        self.assertEqual(got, [("name", "用户叫林野")])


class TestWrite(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("autowrite")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        drop_dir(self._dir)

    def test_writes_with_rule_provenance_and_group(self) -> None:
        written = autowrite.write(self.store, "我叫林野", session_id="s1")
        self.assertEqual(len(written), 1)
        row = self.app.db.conn.execute(
            "SELECT text_raw, source_kind FROM memory WHERE id=?", (written[0]["id"],)
        ).fetchone()
        self.assertEqual(row["text_raw"], "用户叫林野")
        self.assertEqual(row["source_kind"], "rule")
        groups = self.app.db.conn.execute(
            "SELECT g.path FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
            "WHERE gm.target_tbl='memory' AND gm.target_id=?",
            (written[0]["id"],),
        ).fetchall()
        self.assertEqual([g["path"] for g in groups], ["我/姓名"])

    def test_same_declaration_twice_is_idempotent(self) -> None:
        first = autowrite.write(self.store, "我叫林野", session_id="s1")
        second = autowrite.write(self.store, "我叫林野", session_id="s2")
        total = self.app.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
        self.assertEqual(total, 1)
        self.assertEqual(first[0]["id"], second[0]["id"])

    def test_nothing_written_for_chat(self) -> None:
        self.assertEqual(autowrite.write(self.store, "今天好累"), [])
        self.assertEqual(self.app.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
