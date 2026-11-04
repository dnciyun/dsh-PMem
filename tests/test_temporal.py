"""1.10.0 / 0.14.0：时效语义（M14）——两根时间轴、supersede 链、更新题加权。

要守住的东西：

1. **两根时间轴不混用**：打分用 ``valid_from``（现实时间），缺失才退回 ``created_at``；
   回填/导入/日记派生的旧事实不能因为「刚写进库」就排到新说法前面；
2. **旧说法是路标**：旧说法命中时沿 ``superseded_by`` 链补它的替代版本，
   而且只在「问变化」的句子里做（别的题型里补链会把答案顶掉）；
3. **链是只读派生**：不改原文、不改状态，纯读现成的两列。
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.base.clock import days_between
from pmem.gate.sdk import PMem
from pmem.memory import temporal


def _iso(days_ago: float) -> str:
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return stamp.isoformat(timespec="seconds")


def _row(valid_from: str | None, created_at: str | None = None) -> dict:
    row = {"created_at": created_at or _iso(0)}
    if valid_from is not None:
        row["valid_from"] = valid_from
    return row


class TestIntent(unittest.TestCase):
    def test_update_markers(self) -> None:
        for query in (
            "我后来是不是开始喝奶咖了？",
            "我把那台烧油的老伙计换成啥了",
            "我那个日语学到最后坚持下来了吗？",
            "我儿子那个社团后来改成啥了",
            "翠湖那边后来到底签没签？",
        ):
            self.assertTrue(temporal.is_update_query(query), query)

    def test_plain_questions_are_not_update(self) -> None:
        for query in (
            "我家那只橘猫叫什么名字来着？",
            "周末去哪玩了",
            "用户的固定约稿编辑是谁",
        ):
            self.assertFalse(temporal.is_update_query(query), query)

    def test_stale_markers(self) -> None:
        self.assertTrue(temporal.looks_stale("用户本来想这个月辞职的"))
        self.assertTrue(temporal.looks_stale("用户原来打算去大理"))
        self.assertFalse(temporal.looks_stale("用户十月决定留在重庆"))


class TestTimeAxis(unittest.TestCase):
    def test_freshness_uses_valid_from(self) -> None:
        fresh = temporal.freshness(_row(_iso(1)), half_life_days=30)
        stale = temporal.freshness(_row(_iso(120)), half_life_days=30)
        self.assertGreater(fresh, 0.9)
        self.assertLess(stale, 0.1)

    def test_freshness_falls_back_to_created_at(self) -> None:
        row = _row(None, created_at=_iso(60))
        self.assertLess(temporal.freshness(row, half_life_days=30), 0.3)

    def test_freshness_treats_future_as_latest(self) -> None:
        future = (_iso(0).replace("T", "T")[:-6] + "+00:00") if False else _iso(-5)
        self.assertGreater(temporal.freshness(_row(future), half_life_days=30), 0.99)

    def test_freshness_disabled_half_life(self) -> None:
        self.assertEqual(temporal.freshness(_row(_iso(999)), half_life_days=0), 1.0)

    def test_real_axis_prefers_valid_from(self) -> None:
        row = {"valid_from": "2026-09-01T12:00:00+00:00", "created_at": "2026-11-03T12:00:00+00:00"}
        self.assertEqual(temporal.real_axis(row), "2026-09-01T12:00:00+00:00")
        row2 = {"valid_from": None, "created_at": "2026-11-03T12:00:00+00:00"}
        self.assertEqual(temporal.real_axis(row2), "2026-11-03T12:00:00+00:00")


class TestChainScore(unittest.TestCase):
    def test_monotone_and_floor(self) -> None:
        top = temporal.chain_score(1)
        mid = temporal.chain_score(5)
        floor = temporal.chain_score(99, floor=0.55)
        self.assertGreater(top, mid)
        self.assertGreaterEqual(floor, 0.55)
        self.assertLessEqual(top, 0.75)


class TestChainLinks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PMem.open(Path(self.tmp.name) / "chain.db", config=Config())
        self.app.db.init()
        self.store = self.app.store
        self.conn = self.app.db.conn

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _add(self, text: str, *, valid_from: str | None = None) -> dict:
        return self.store.add(
            text,
            kind="semantic",
            provenance="user_explicit",
            valid_from=valid_from,
        )

    def _seq(self, memory_id: str) -> int:
        return int(
            self.conn.execute("SELECT seq FROM memory WHERE id=?", (memory_id,)).fetchone()["seq"]
        )

    def _supersede(self, old_id: str, new_id: str) -> None:
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE memory SET status='superseded', superseded_by=? WHERE id=?", (new_id, old_id)
            )
            conn.execute("UPDATE memory SET supersedes_id=? WHERE id=?", (old_id, new_id))

    def test_single_hop(self) -> None:
        old = self._add("用户早上挤地铁上班，单程五十多分钟")
        new = self._add("用户改骑车上班，单程二十五分钟")
        self._supersede(old["id"], new["id"])
        found = temporal.chain_replacements(self.conn, [self._seq(old["id"])], subject="self")
        self.assertEqual(found[self._seq(old["id"])][0], self._seq(new["id"]))

    def test_multi_hop_reaches_tail(self) -> None:
        first = self._add("用户手冲参数第一版，一比十五")
        second = self._add("用户手冲参数第二版，一比十四")
        third = self._add("用户手冲参数第三版，91 度、一比十四")
        self._supersede(first["id"], second["id"])
        self._supersede(second["id"], third["id"])
        found = temporal.chain_replacements(self.conn, [self._seq(first["id"])], subject="self", hops=3)
        self.assertEqual(found[self._seq(first["id"])][0], self._seq(third["id"]))
        self.assertEqual(found[self._seq(first["id"])][1], 2)

    def test_hop_limit(self) -> None:
        first = self._add("参数第一版")
        second = self._add("参数第二版")
        third = self._add("参数第三版")
        self._supersede(first["id"], second["id"])
        self._supersede(second["id"], third["id"])
        found = temporal.chain_replacements(self.conn, [self._seq(first["id"])], subject="self", hops=1)
        self.assertEqual(found, {})

    def test_dangling_link_skipped(self) -> None:
        old = self._add("旧说法")
        new = self._add("新说法")
        self._supersede(old["id"], new["id"])
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE memory SET status='deleted', deleted_at=? WHERE id=?",
                ("2026-11-01T00:00:00+00:00", new["id"]),
            )
        found = temporal.chain_replacements(self.conn, [self._seq(old["id"])], subject="self")
        self.assertEqual(found, {})

    def test_no_link_no_map(self) -> None:
        plain = self._add("没有更新链的记忆")
        found = temporal.chain_replacements(self.conn, [self._seq(plain["id"])], subject="self")
        self.assertEqual(found, {})


class TestTemporalSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config({"retrieval": {"vector": {"enabled": False}}})
        self.app = PMem.open(Path(self.tmp.name) / "t.db", config=self.cfg)
        self.app.db.init()
        self.store = self.app.store

    def tearDown(self) -> None:
        self.app.close()
        self.tmp.cleanup()

    def _add(self, text: str, *, valid_from: str | None = None) -> dict:
        return self.store.add(
            text, kind="semantic", provenance="user_explicit", valid_from=valid_from
        )

    def _link(self, old_id: str, new_id: str) -> None:
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE memory SET status='superseded', superseded_by=? WHERE id=?", (new_id, old_id)
            )
            conn.execute("UPDATE memory SET supersedes_id=? WHERE id=?", (old_id, new_id))

    def test_update_query_follows_chain(self) -> None:
        old = self._add("用户早上挤地铁上班，单程五十多分钟", valid_from="2026-08-20T12:00:00+00:00")
        # 新说法故意不写「用户」：写成「用户改骑车」会被查询改写通道的「用户」二字
        # 直接命中（那是另一条既有通道），测不到链补这一步。
        new = self._add("改骑车上班，单程二十五分钟", valid_from="2026-09-01T12:00:00+00:00")
        self._link(old["id"], new["id"])
        data = self.app.search("我早上不挤那趟车之后改成了什么？", k=8, record=False)["data"]
        ids = [item["id"] for item in data["items"]]
        self.assertIn(new["id"], ids)
        item = next(item for item in data["items"] if item["id"] == new["id"])
        self.assertEqual(item["source"], "supersede")
        self.assertEqual(data["temporal"]["added"], 1)

    def test_plain_query_does_not_follow_chain(self) -> None:
        old = self._add("用户早上挤地铁上班，单程五十多分钟", valid_from="2026-08-20T12:00:00+00:00")
        new = self._add("用户改骑车上班，单程二十五分钟", valid_from="2026-09-01T12:00:00+00:00")
        self._link(old["id"], new["id"])
        data = self.app.search("早上挤地铁几点出门", k=8, record=False)["data"]
        self.assertFalse(any(item.get("source") == "supersede" for item in data["items"]))

    def test_update_query_without_links_is_harmless(self) -> None:
        self._add("用户养了一只橘猫叫年糕")
        data = self.app.search("我后来养的猫叫啥来着", k=8, record=False)["data"]
        self.assertFalse(any(item.get("source") == "supersede" for item in data["items"]))

    def test_valid_from_axis_beats_write_time(self) -> None:
        """两句话都在「今天」写进库，但一句讲的是三个月前、一句讲的是昨天。"""
        self._add("用户三个月前决定不学日语了，把 App 删了", valid_from=_iso(90))
        fresh = self._add("用户昨天开始重新学日语，背五十音", valid_from=_iso(1))
        data = self.app.search("我日语学到哪了", k=4, record=False)["data"]
        ids = [item["id"] for item in data["items"]]
        self.assertIn(fresh["id"], ids)
        self.assertLess(ids.index(fresh["id"]), 2)

    def test_temporal_can_be_disabled(self) -> None:
        cfg = Config({"retrieval": {"temporal": {"enabled": False}, "vector": {"enabled": False}}})
        app = PMem.open(Path(self.tmp.name) / "off.db", config=cfg)
        app.db.init()
        try:
            old = app.store.add(
                "用户早上挤地铁上班", kind="semantic", provenance="user_explicit",
                valid_from="2026-08-20T12:00:00+00:00",
            )
            new = app.store.add(
                "用户改骑车上班", kind="semantic", provenance="user_explicit",
                valid_from="2026-09-01T12:00:00+00:00",
            )
            with app.db.tx() as conn:
                conn.execute(
                    "UPDATE memory SET status='superseded', superseded_by=? WHERE id=?",
                    (new["id"], old["id"]),
                )
            data = app.search("我早上不挤那趟车之后改成了什么？", k=8, record=False)["data"]
            self.assertNotIn("temporal", data)
            self.assertFalse(any(item.get("source") == "supersede" for item in data["items"]))
        finally:
            app.close()


class TestAxisHelper(unittest.TestCase):
    def test_days_between_sanity(self) -> None:
        self.assertGreater(days_between(_iso(30)), 29)


if __name__ == "__main__":
    unittest.main()
