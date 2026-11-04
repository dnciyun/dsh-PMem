"""1.11.0 / 0.15.0：语文范围（M15）——代词、自称、角色与「这是谁的事」。

要守住的东西：

1. **角色词归一**：问句写「他爸」、记忆写「爸爸」，是同一个角色；
2. **名字+代词**指本人：「小雅她…」不是「小雅的男朋友…」；
3. **自称 vs 他人所属**：「我是什么工作的」不该答成「用户的朋友在博远工作」；
4. 这些都是词面规则，可关、可解释、不调模型。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pmem.base.config import Config
from pmem.base.clock import days_between
from pmem.gate.sdk import PMem
from pmem.memory import scope, temporal


def _iso(days_ago: float) -> str:
    import datetime as dt

    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return stamp.isoformat(timespec="seconds")


class TestRelationWords(unittest.TestCase):
    def test_canonical_family(self) -> None:
        self.assertEqual(scope.relation_words("小明他爸是做什么的"), {"爸"})
        self.assertEqual(scope.relation_words("小明爸爸开出租车"), {"爸"})
        self.assertIn("妈", scope.relation_words("小红她妈是护士"))
        self.assertIn("妈", scope.relation_words("小红妈妈在急诊"))

    def test_relation_words_longest_first(self) -> None:
        words = scope.relation_words("小雅的男朋友在成都")
        self.assertIn("男伴", words)
        self.assertNotIn("朋友", "".join(sorted(words)))

    def test_no_relation(self) -> None:
        self.assertEqual(scope.relation_words("我在杭州上班"), set())

    def test_self_query(self) -> None:
        for query in ("我是什么工作的", "本人叫什么", "自己住哪儿", "咱家猫叫啥", "我们公司叫什么"):
            self.assertTrue(scope.is_self_query(query), query)
        self.assertFalse(scope.is_self_query("小明他爸是做什么的"))


class TestScopeRules(unittest.TestCase):
    def test_named_pronoun(self) -> None:
        self.assertEqual(scope.named_pronoun_subjects("小雅她是在哪儿上班"), ["小雅"])
        self.assertEqual(scope.named_pronoun_subjects("小满她最近压力大吗"), ["小满"])
        self.assertEqual(scope.named_pronoun_subjects("他们后来怎么样"), [])

    def test_owner_conflict_when_asking_person(self) -> None:
        self.assertTrue(scope.owner_conflict("小雅的男朋友在成都上班", "小雅她是在哪儿上班"))

    def test_owner_conflict_not_when_asking_relation(self) -> None:
        # 问句自己就在问「她男朋友」，这时答「小雅的男朋友」正是要的
        self.assertFalse(scope.owner_conflict("小雅的男朋友在成都上班", "小雅她男朋友在哪儿上班"))

    def test_other_owner(self) -> None:
        self.assertTrue(scope.other_owner("用户的朋友在博远工作"))
        self.assertTrue(scope.other_owner("用户的妹妹今年考研"))
        self.assertFalse(scope.other_owner("用户在云图科技工作"))

    def test_name_focus(self) -> None:
        self.assertGreater(scope.name_focus("小满她最近压力大吗", "用户的妹妹小满今年考研"), 0)
        self.assertEqual(scope.name_focus("小满她最近压力大吗", "用户最近项目上线"), 0.0)

    def test_role_bonus(self) -> None:
        self.assertGreater(scope.role_bonus("小明他爸是开什么的", "小明爸爸开出租车"), 0)
        self.assertEqual(scope.role_bonus("小明他爸是开什么的", "小明在念小学"), 0.0)
        self.assertEqual(scope.role_bonus("今天天气怎么样", "小明爸爸开出租车"), 0.0)


class TestScopeScoring(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _app(self, *, enabled: bool) -> PMem:
        cfg = Config(
            {
                "retrieval": {
                    "vector": {"enabled": False},
                    "scope": {"enabled": enabled},
                }
            }
        )
        app = PMem.open(Path(self.tmp.name) / f"scope-{enabled}.db", config=cfg)
        app.db.init()
        return app

    def _top(self, app: PMem, query: str) -> list[str]:
        data = app.search(query, k=8, record=False)["data"]
        return [str(item.get("text") or "") for item in data["items"]]

    def test_friend_work_not_top_when_asking_self(self) -> None:
        app = self._app(enabled=True)
        try:
            app.store.add("用户现在在云图科技工作，做前端实习", kind="semantic", provenance="user_explicit")
            app.store.add("用户的朋友在博远工作，做数据产品", kind="semantic", provenance="user_explicit")
            top = self._top(app, "我是什么工作的？")
            self.assertIn("云图", top[0], top[:2])
        finally:
            app.close()

    def test_relation_person_not_top_when_asking_person(self) -> None:
        app = self._app(enabled=True)
        try:
            app.store.add("小雅在杭州当老师", kind="semantic", provenance="user_explicit")
            app.store.add("小雅的男朋友在成都上班", kind="semantic", provenance="user_explicit")
            top = self._top(app, "小雅她是在哪儿上班来着？")
            self.assertIn("杭州", top[0], top[:2])
        finally:
            app.close()

    def test_role_word_binds_father(self) -> None:
        app = self._app(enabled=True)
        try:
            app.store.add("小明在念小学二年级", kind="semantic", provenance="user_explicit")
            app.store.add("小明爸爸开出租车，夜班多", kind="semantic", provenance="user_explicit")
            top = self._top(app, "小明他爸是开什么的？")
            self.assertIn("出租车", top[0], top[:2])
        finally:
            app.close()


class TestOldIntent(unittest.TestCase):
    def test_old_markers(self) -> None:
        for query in ("我第一次去修车摊是什么问题", "最初的迁移方案是啥", "我一开始用什么编辑器", "当初怎么定的"):
            self.assertTrue(temporal.is_old_query(query), query)
            self.assertTrue(temporal.is_past_query(query) or temporal.is_old_query(query))

    def test_plain_question_is_not_old(self) -> None:
        self.assertFalse(temporal.is_old_query("我现在住哪儿"))
        self.assertFalse(temporal.is_old_query("周末去哪玩"))

    def test_old_and_update_combo_does_not_invert(self) -> None:
        """「当初…后来…」是复合问句：能识别出 old，但 store 里不会反转新近度。"""
        query = "小邹当初是从哪儿回来的，后来我俩怎么了"
        self.assertTrue(temporal.is_old_query(query))
        self.assertTrue(temporal.is_update_query(query))


class TestScopeRecall(unittest.TestCase):
    def test_prefix_kinship_regex(self) -> None:
        for text, word in (("我小姨来了", "小姨"), ("二舅在成都", "二舅"), ("三叔在北京", "三叔"), ("表姐是护士", "表姐")):
            self.assertIn(word, scope.relation_words(text), text)

    def test_relation_surface_keeps_words(self) -> None:
        self.assertIn("小姨", scope.relation_surface("我小姨和二舅都来了"))
        self.assertIn("二舅", scope.relation_surface("我小姨和二舅都来了"))

    def test_recall_terms(self) -> None:
        terms = scope.recall_terms("小雅她妈是做什么的")
        self.assertIn("小雅", terms)
        terms2 = scope.recall_terms("小姨最近怎么样")
        self.assertIn("小姨", terms2)


class TestValidWindow(unittest.TestCase):
    def test_expired_and_pending_helpers(self) -> None:
        self.assertTrue(temporal.expired({"valid_to": _iso(10)}))
        self.assertFalse(temporal.expired({"valid_to": _iso(-10)}))
        self.assertFalse(temporal.expired({"valid_to": None}))
        self.assertTrue(temporal.pending({"valid_from": _iso(-10)}))
        self.assertFalse(temporal.pending({"valid_from": _iso(10)}))
        self.assertFalse(temporal.pending({"valid_from": None}))

    def test_expired_fact_capped_below_valid(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = Config({"retrieval": {"vector": {"enabled": False}}})
            app = PMem.open(Path(tmp.name) / "win.db", config=cfg)
            app.db.init()
            app.store.add(
                "用户家的猫吃冻干，别的都不碰",
                kind="semantic",
                provenance="user_explicit",
                valid_from=_iso(200),
            )
            app.store.add(
                "用户家的猫吃的是罐头",
                kind="semantic",
                provenance="user_explicit",
                valid_from=_iso(100),
                valid_to=_iso(30),
            )
            top = [str(item["text"]) for item in app.search("我家猫吃什么", k=8, record=False)["data"]["items"]]
            self.assertIn("冻干", top[0], top[:2])
            app.close()
        finally:
            tmp.cleanup()

    def test_past_query_keeps_expired_relevant(self) -> None:
        """问「以前」时，过期事实不降权（那时它成立）。"""
        self.assertTrue(temporal.is_past_query("我以前住哪儿来着"))
        self.assertFalse(temporal.is_past_query("我现在住哪儿"))
        self.assertTrue(temporal.is_future_query("我下个月去哪儿"))
        self.assertFalse(temporal.is_future_query("我现在在哪儿"))


class TestOldQueryRanking(unittest.TestCase):
    def test_first_time_question_prefers_older_fact(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = Config({"retrieval": {"vector": {"enabled": False}}})
            app = PMem.open(Path(tmp.name) / "old.db", config=cfg)
            app.db.init()
            app.store.add(
                "用户第一次去修车摊是后闸松了，花二十换线管",
                kind="semantic",
                provenance="user_explicit",
                valid_from=_iso(300),
            )
            app.store.add(
                "用户第三次去修车摊，链条前拨一起修",
                kind="semantic",
                provenance="user_explicit",
                valid_from=_iso(60),
            )
            top = [str(item["text"]) for item in app.search("我第一次去修车摊是什么问题？", k=4, record=False)["data"]["items"]]
            app.close()
            self.assertIn("后闸", top[0], top[:2])
        finally:
            tmp.cleanup()

    def test_recent_question_prefers_newer_fact(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = Config({"retrieval": {"vector": {"enabled": False}}})
            app = PMem.open(Path(tmp.name) / "new.db", config=cfg)
            app.db.init()
            app.store.add(
                "用户最开始只喝冰美式咖啡",
                kind="semantic",
                provenance="user_explicit",
                valid_from=_iso(300),
            )
            app.store.add(
                "用户后来改喝生椰拿铁咖啡了",
                kind="semantic",
                provenance="user_explicit",
                valid_from=_iso(60),
            )
            top = [str(item["text"]) for item in app.search("我现在喝什么咖啡？", k=4, record=False)["data"]["items"]]
            app.close()
            self.assertIn("生椰", top[0], top[:2])
        finally:
            tmp.cleanup()


class TestDisabled(unittest.TestCase):
    def test_scope_can_be_disabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = Config({"retrieval": {"scope": {"enabled": False}, "vector": {"enabled": False}}})
            app = PMem.open(Path(tmp.name) / "off.db", config=cfg)
            app.db.init()
            app.store.add("用户的朋友在博远工作", kind="semantic", provenance="user_explicit")
            items = app.search("我是什么工作的", k=4, record=False)["data"]["items"]
            self.assertIsInstance(items, list)
            app.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
