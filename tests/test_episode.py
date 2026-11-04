"""日记检索通道（M18）：问「上周/前几天」时，答案是一天的经过，不是一条事实。

守三件事：

1. **日记有自己的索引**：写完立刻能搜到，老库第一次打开时补建（幂等）；
2. **时间窗问句把日记放到最前面，问事实的句子不许被日记顶掉**——
   这一条是「记得住日常」和「被流水账淹没」之间的分界线；
3. **把握不够要明说**：不确定不只是排序靠后，而是给出 ``uncertainty`` 并留一条
   ``recall.weak`` 事件（可以直接查「我到底在哪些问题上没把握」）。

时间一律用 ``clock.set_now`` 推进，不靠 sleep，也不靠真实日历。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base import clock
from pmem.gate.sdk import PMem
from pmem.memory import journal, temporal

from .support import drop_dir, temp_dir

# 2026-11-05 21:00 UTC —— 「今天」；前面几天用来造日记。
T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("episode")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.db = self.app.db
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def make_episode(self, day: str, texts: list[str], *, facts: bool = True) -> dict:
        base = datetime.fromisoformat(day + "T10:00:00+00:00")
        for index, text in enumerate(texts):
            journal.append(
                self.db,
                text,
                role="user",
                session_id="s1",
                ts=(base + timedelta(minutes=index)).isoformat(),
            )
        result = journal.digest(
            self.db,
            day_key=day,
            store=self.store if facts else None,
            config=self.app.config,
        )
        self.assertTrue(result["ok"], result)
        return result


class WindowWordTest(unittest.TestCase):
    """时段词只做词面判断，但「上周末」不能被「周末」抢先。"""

    def test_specific_words_win(self) -> None:
        self.assertEqual(temporal.range_window("上周末咱们去哪了")["days"], 10)
        self.assertEqual(temporal.range_window("周末都干嘛了")["days"], 8)
        self.assertEqual(temporal.range_window("上个月花了多少钱")["days"], 32)
        self.assertEqual(temporal.range_window("上周咱们都干嘛了")["days"], 14)

    def test_short_windows(self) -> None:
        self.assertEqual(temporal.range_window("昨天发生了什么")["days"], 2)
        self.assertEqual(temporal.range_window("前几天忙什么")["days"], 6)
        self.assertEqual(temporal.range_window("最近在忙什么")["days"], 10)

    def test_no_window_is_none(self) -> None:
        self.assertIsNone(temporal.range_window("我养的那只猫叫什么名字"))

    def test_explicit_date(self) -> None:
        # 「11月3号那天发生了什么」比「最近」更具体，应该精确命中那一天。
        clock.set_now(T0)
        try:
            window = temporal.range_window("11月3号那天发生了什么")
            self.assertEqual(window["day"], "2026-11-03")
            self.assertEqual(window["label"], "11月3号")
            self.assertIn("发生了什么", "11月3号那天发生了什么")
            self.assertTrue(temporal.asks_for_events("11月3号那天发生了什么"))
        finally:
            clock.reset_now()

    def test_future_month_rolls_back_a_year(self) -> None:
        # 现在 11 月，问「12月3号」问的是去年那次——回忆几乎总是往回看。
        clock.set_now(T0)
        try:
            self.assertEqual(temporal.range_window("12月3号发生了什么")["day"], "2025-12-03")
            self.assertIsNone(temporal.range_window("2月30号发生了什么"))
        finally:
            clock.reset_now()

    def test_event_words(self) -> None:
        self.assertTrue(temporal.asks_for_events("上周咱们都干嘛了"))
        self.assertTrue(temporal.asks_for_events("前几天发生了什么"))
        # 问事实的句子不是问经过
        self.assertFalse(temporal.asks_for_events("上个月体检结果怎么样"))


class IndexTest(Base):
    def test_digest_writes_episode_index(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        count = self.db.conn.execute("SELECT COUNT(*) c FROM episode_fts").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_backfill_is_idempotent(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检"])
        self.db.conn.execute("DELETE FROM episode_fts")
        self.assertEqual(journal.ensure_episode_index(self.db), 1)
        self.assertEqual(journal.ensure_episode_index(self.db), 0)

    def test_backfill_covers_old_library(self) -> None:
        # 1.9.0 之前建的库：episode 有行、episode_fts 空——第一次打开就该补齐。
        self.db.conn.execute(
            "INSERT INTO episode(id, subject_id, channel, started_at, ended_at, day_key, title, "
            "summary, memory_count) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "ep_old",
                "self",
                "dsh",
                "2026-11-01T09:00:00+00:00",
                "2026-11-01T22:00:00+00:00",
                "2026-11-01",
                "搬家",
                "你搬到了城东，新房子朝南。",
                2,
            ),
        )
        self.assertEqual(journal.ensure_episode_index(self.db), 1)
        items = journal.search_episodes(self.db, query="搬家")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "ep_old")
        self.assertEqual(items[0]["memory_count"], 2)


class SearchEpisodeTest(Base):
    def test_matches_by_words(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        items = journal.search_episodes(self.db, query="体检")
        self.assertEqual(len(items), 1)
        self.assertIn("2026-11-03", items[0]["text"])
        self.assertEqual(items[0]["kind"], "episode")
        self.assertEqual(items[0]["source"], "episode")

    def test_window_falls_back_to_recent_diaries(self) -> None:
        # 一个词都没命中，但窗口里有日记：问「那几天」时它仍然是答案。
        self.make_episode("2026-11-03", ["我去医院做了体检"])
        items = journal.search_episodes(self.db, query="干嘛啦", since="2026-10-30")
        self.assertEqual(len(items), 1)

    def test_no_window_and_no_words_returns_nothing(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检"])
        self.assertEqual(journal.search_episodes(self.db, query="干嘛啦"), [])

    def test_window_excludes_older_diaries(self) -> None:
        self.make_episode("2026-10-01", ["我在写论文"])
        self.assertEqual(journal.search_episodes(self.db, query="论文", since="2026-10-30"), [])

    def test_explicit_day_survives_without_keyword_hit(self) -> None:
        # 「11月3号」那天写了什么：一个词都没命中，也要把那天的那一篇拿回来。
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        items = journal.search_episodes(
            self.db, query="发生了什么事情呀", since="2026-11-03", until="2026-11-03"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["day"], "2026-11-03")


class StoreChannelTest(Base):
    def test_week_question_puts_diary_first(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        self.store.add("用户上个月体检血脂偏高", kind="episodic")
        result = self.store.search("上周咱们都干嘛了", k=5)
        self.assertEqual(result["items"][0]["kind"], "episode")
        self.assertEqual(result["episode"]["at"], "head")
        self.assertEqual(result["episode"]["window"], "上周")

    def test_fact_question_is_not_hijacked(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检"])
        self.store.add("用户上个月体检查出血脂偏高")
        result = self.store.search("上个月体检结果怎么样", k=5)
        self.assertNotEqual(result["items"][0]["kind"], "episode")
        self.assertNotIn("episode", result)

    def test_unrelated_query_does_not_dump_a_diary(self) -> None:
        # 没有时间窗、词法也不相关：不该倒一篇不相干的日记当「线索」，那是噪音。
        self.make_episode("2026-11-03", ["我去医院做了体检"])
        result = self.store.search("完全不相关的一句话", k=5)
        self.assertNotIn("episode", result)

    def test_explicit_date_question_hits_that_day(self) -> None:
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        result = self.store.search("11月3号那天发生了什么", k=5)
        self.assertEqual(result["items"][0]["kind"], "episode")
        self.assertEqual(result["episode"]["window"], "11月3号")

    def test_explicit_date_survives_budget_pressure(self) -> None:
        # 日记就是答案：预算被普通记忆花光时，让尾部的记忆让位，而不是让日记消失。
        # 第一版只用「剩余预算」，结果这条问句一条日记都插不进来。
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        for index in range(8):
            self.store.add(f"用户上个月买了第 {index} 件东西，花了 {index * 10} 块钱")
        result = self.store.search("11月3号那天发生了什么", k=6, budget=150)
        self.assertEqual(result["items"][0]["kind"], "episode")

    def test_diary_used_as_fallback_for_a_fact_question_is_flagged(self) -> None:
        # 问事实却只捞到一篇日记：可以给出来当线索，但必须说「不确定」。
        self.make_episode("2026-11-03", ["我去医院做了体检"], facts=False)
        result = self.store.search("上个月体检结果怎么样", k=5)
        self.assertEqual(result["items"][0]["kind"], "episode")
        self.assertTrue(result["uncertainty"]["weak"])
        self.assertIn("coverage", result["uncertainty"]["reasons"])


class SparseTest(Base):
    """稀疏自适应（M19）：日记是会断的，空窗不能装作那段时间什么都没发生。"""

    def test_window_miss_widens_and_says_so(self) -> None:
        # 「昨天」没写日记，五天前有一篇：放宽到 8 天找到，并说明放宽到了哪。
        self.make_episode("2026-10-31", ["我去医院做了体检"])
        result = self.store.search("昨天发生了什么", k=5)
        self.assertEqual(result["items"][0]["kind"], "episode")
        self.assertTrue(result["episode"]["sparse"])
        self.assertIn("放宽", result["episode"]["note"])

    def test_nothing_nearby_falls_back_to_latest_diary(self) -> None:
        # 最近的日记在一个多月前：退到最近一篇，并说清它是哪一天的。
        self.make_episode("2026-09-20", ["我在写论文"])
        result = self.store.search("昨天发生了什么", k=5)
        self.assertEqual(result["items"][0]["kind"], "episode")
        self.assertIn("最近的一篇", result["episode"]["note"])
        self.assertEqual(result["episode"]["latest"], "2026-09-20")

    def test_sparseness_is_flagged_not_hidden(self) -> None:
        # 稀疏不是错，但必须被看见：字段叫 sparse，渲染层据此加一行说明。
        self.make_episode("2026-09-20", ["我在写论文"])
        result = self.store.search("上周咱们都干嘛了", k=5)
        self.assertTrue(result["episode"]["sparse"])


class UncertaintyTest(Base):
    def test_hedges_when_nothing_matches(self) -> None:
        result = self.store.search("宇宙飞船的燃料配方", k=5)
        self.assertTrue(result["uncertainty"]["weak"])
        self.assertIn("empty", result["uncertainty"]["reasons"])

    def test_no_hedge_on_a_clean_hit(self) -> None:
        self.store.add("用户养了一只布偶猫，叫团团")
        result = self.store.search("我养的那只布偶猫叫什么", k=5)
        self.assertFalse(result["uncertainty"]["weak"])
        self.assertEqual(result["uncertainty"]["reasons"], [])

    def test_weak_recall_is_logged(self) -> None:
        self.store.search("宇宙飞船的燃料配方", k=5)
        row = self.db.conn.execute(
            "SELECT COUNT(*) c FROM event_log WHERE op='recall.weak'"
        ).fetchone()
        self.assertEqual(row["c"], 1)

    def test_diary_first_does_not_count_as_uncertain(self) -> None:
        # 问经历时头一条就是日记：它没有「覆盖率」这把尺子，不该因此报不确定。
        self.make_episode("2026-11-03", ["我去医院做了体检", "医生说血脂有点高"])
        result = self.store.search("上周咱们都干嘛了", k=5)
        self.assertNotIn("coverage", result["uncertainty"]["reasons"])

    def test_doubt_lists_weak_recalls(self) -> None:
        # 「不确定时说出来」的出口：留了流水，才能回头看不确定集中在什么话题上。
        self.store.search("宇宙飞船的燃料配方", k=5)
        result = self.app.doubt(limit=10)
        self.assertTrue(result["ok"])
        items = result["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["query"], "宇宙飞船的燃料配方")
        self.assertIn("empty", items[0]["reasons"])


if __name__ == "__main__":
    unittest.main()
