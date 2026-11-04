"""日记（M10）：白天攒流水，该睡的时候才整理。

守的是两件事：

1. **原文只追加**：流水原样存，日记是派生出来的结论，两不相欠；
2. **结论必有证据**：模型写出来的日记里出现原文没有的专名，整篇回落成模板——
   宁可流水账，也不要一篇编出来的日记。

时间一律用 ``clock.set_now`` 推进，不靠 sleep，也不靠真实日历。
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmem.base import clock
from pmem.gate.sdk import PMem
from pmem.memory import journal

from .support import drop_dir, temp_dir

T0 = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


class FakeModel:
    """只回一段预设 JSON 的假模型，用来验「模型写的日记」这条路径。"""

    paid = True
    name = "fake"

    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.calls = 0

    def complete(self, messages, tools=None, hint=None):  # noqa: ANN001, ARG002
        from pmem.bench.model import Reply, Usage

        self.calls += 1
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return Reply(content=text, usage=Usage(prompt_tokens=500, completion_tokens=80), tool_calls=[])


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = temp_dir("journal")
        self.app = PMem.open(Path(self._dir) / "pmem.db")
        self.db = self.app.db
        self.store = self.app.store
        clock.set_now(T0)

    def tearDown(self) -> None:
        clock.reset_now()
        self.app.close()
        drop_dir(self._dir)

    def add_lines(self, lines, *, start: datetime | None = None) -> None:
        base = start or T0
        for index, (role, text) in enumerate(lines):
            journal.append(
                self.db,
                text,
                role=role,
                session_id="s1",
                ts=(base + timedelta(minutes=index)).isoformat(),
            )


class AppendTest(Base):
    def test_appends_in_order_and_keeps_text_verbatim(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门，第十二集，太顶了"), ("assistant", "那集确实厉害")])
        rows = journal.pending(self.db)
        self.assertEqual([row["role"] for row in rows], ["user", "assistant"])
        self.assertEqual(rows[0]["text"], "我昨晚看了石头门，第十二集，太顶了")
        self.assertEqual(rows[0]["day_key"], clock.day_key())

    def test_rejects_unknown_role(self) -> None:
        with self.assertRaises(ValueError):
            journal.append(self.db, "x", role="system")

    def test_empty_text_is_ignored(self) -> None:
        self.assertFalse(journal.append(self.db, "   ")["ok"])
        self.assertEqual(journal.pending(self.db), [])


class RuntimeStripTest(Base):
    """运行时上下文是网关这一层的事：不指望调用方永远剥干净。"""

    def test_polluted_line_keeps_only_the_real_message(self) -> None:
        polluted = (
            'Current DeepSeek Harness implementation checkout: "C:/x". '
            "Current DSH file policy: danger-full-access." + chr(10) + "重启好了"
        )
        self.assertTrue(journal.append(self.db, polluted)["ok"])
        self.assertEqual(journal.pending(self.db)[0]["text"], "重启好了")

    def test_pure_runtime_context_is_dropped(self) -> None:
        result = journal.append(self.db, "Current runtime context. This snapshot supersedes earlier ones.")
        self.assertFalse(result["ok"])
        self.assertEqual(journal.pending(self.db), [])

    def test_skeleton_block_is_stripped(self) -> None:
        text = "[骨架]" + chr(10) + "- 约束: x" + chr(10) + "[提示] y" + chr(10) + "我用旧的就好了"
        self.assertTrue(journal.append(self.db, text)["ok"])
        self.assertEqual(journal.pending(self.db)[0]["text"], "我用旧的就好了")

    def test_host_persona_lines_are_stripped(self) -> None:
        # 宿主的团队人格提示排在真消息前面，被当成用户原话记下来过一次：
        # 于是当天日记是脚手架拼的，还派生出「用户You are the Team Lead…」这种记忆。
        text = (
            'You are the Team Lead named "lead". Use list_agents({}) to find your teammates.'
            + chr(10)
            + "先完善，后面再考虑开源"
        )
        self.assertTrue(journal.append(self.db, text)["ok"])
        self.assertEqual(journal.pending(self.db)[0]["text"], "先完善，后面再考虑开源")

    def test_pure_harness_noise_is_dropped(self) -> None:
        for text in (
            'You are the Team Lead named "lead".',
            "Background subagent 68ae40e1 finished and will do no further work.",
            "Use list_agents({}) to find your teammates.",
        ):
            self.assertFalse(journal.append(self.db, text)["ok"], text)
        self.assertEqual(journal.pending(self.db), [])


class DigestTest(Base):
    def test_template_digest_writes_episode_and_facts(self) -> None:
        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集，太顶了"),
                ("assistant", "那集确实厉害"),
                ("user", "今天中午吃了牛肉面，加两个蛋"),
            ]
        )
        result = journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "template")
        self.assertEqual(result["facts"], ["用户昨晚看了石头门，第十二集，太顶了", "用户今天中午吃了牛肉面，加两个蛋"])

        episode = self.db.conn.execute("SELECT * FROM episode").fetchone()
        self.assertIsNotNone(episode)
        self.assertEqual(episode["day_key"], clock.day_key())
        self.assertIn("你昨晚看了石头门", episode["summary"])
        self.assertEqual(episode["memory_count"], 2)

        # 事实进了记忆表，并且指回日记 —— 这一步是「进星图」的前提
        rows = self.db.conn.execute("SELECT id, text_raw, episode_id, source_kind FROM memory").fetchall()
        self.assertEqual(len(rows), 2)
        grades = {
            row["target_id"]: row["grade"]
            for row in self.db.conn.execute(
                "SELECT target_id, grade FROM target_grade WHERE target_tbl='memory'"
            ).fetchall()
        }
        for row in rows:
            self.assertEqual(row["episode_id"], episode["id"])
            self.assertEqual(row["source_kind"], "journal")
            self.assertEqual(grades.get(row["id"]), "C")

    def test_second_digest_has_nothing_left(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门，第十二集")] * 3)
        self.assertTrue(journal.digest(self.db, store=self.store, config=self.app.config)["ok"])
        again = journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "no_lines")

    def test_too_few_lines_does_not_write(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了面")])
        result = journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "too_few_lines")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) c FROM episode").fetchone()["c"], 0)

    def test_dry_run_touches_nothing(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了面"), ("user", "在写前端")])
        result = journal.digest(self.db, store=self.store, config=self.app.config, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) c FROM episode").fetchone()["c"], 0)
        self.assertEqual(len(journal.pending(self.db)), 3)


class ModelDigestTest(Base):
    def test_model_draft_is_used_when_it_checks_out(self) -> None:
        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集，太顶了"),
                ("user", "今天中午吃了牛肉面"),
                ("user", "我在杭州做前端开发"),
            ]
        )
        model = FakeModel({"title": "石头门和牛肉面", "lines": ["你昨晚看了石头门第十二集", "你中午吃了牛肉面"]})
        result = journal.digest(
            self.db,
            store=self.store,
            config=self.app.config,
            writer=lambda rows: journal.model_draft(model, rows, day_key=clock.day_key()),
        )
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["problems"], [])
        self.assertEqual(model.calls, 1)
        # 事实从**原话**派生，不从日记派生：模型措辞再怎么变，证据层不变。
        self.assertEqual(result["facts"][0], "用户昨晚看了石头门，第十二集，太顶了")

    def test_invented_name_falls_back_to_template(self) -> None:
        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集，太顶了"),
                ("user", "今天中午吃了牛肉面"),
                ("user", "我在杭州做前端开发"),
            ]
        )
        # 《海德格尔全集》原文里根本没有 —— 判为编造，整篇回落
        model = FakeModel({"title": "深夜", "lines": ["你昨晚看了《海德格尔全集》"]})
        result = journal.digest(
            self.db,
            store=self.store,
            config=self.app.config,
            writer=lambda rows: journal.model_draft(model, rows, day_key=clock.day_key()),
        )
        self.assertEqual(result["mode"], "template")
        self.assertTrue(any(p.startswith("unknown_name") for p in result["problems"]))

    def test_broken_json_falls_back(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了面"), ("user", "在写前端")])
        model = FakeModel("我想了想，今天挺好的。")
        result = journal.digest(
            self.db,
            store=self.store,
            config=self.app.config,
            writer=lambda rows: journal.model_draft(model, rows, day_key=clock.day_key()),
        )
        self.assertEqual(result["mode"], "template")
        self.assertTrue(result["problems"])


class DueTest(Base):
    def test_idle_after_quiet_minutes(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了面"), ("user", "在写前端")])
        clock.set_now(T0 + timedelta(minutes=10))
        self.assertFalse(journal.due(self.db)["due"])
        clock.set_now(T0 + timedelta(minutes=50))
        verdict = journal.due(self.db)
        self.assertTrue(verdict["due"])
        self.assertEqual(verdict["reason"], "idle")

    def test_yesterday_leftover_waits_for_model(self) -> None:
        """跨天不抢着写：先等在场模型自己写（醒来卡会提醒）。"""
        yesterday = T0 - timedelta(days=1)
        self.add_lines([("user", "昨天看了石头门"), ("user", "昨天吃了面")], start=yesterday)
        verdict = journal.due(self.db)
        self.assertFalse(verdict["due"])
        self.assertEqual(verdict["reason"], "awaiting_model")

        # 拖过 stale_days（默认 2 天）就由 daemon 用模板结账，防止流水烂在库里
        clock.set_now(T0 + timedelta(days=2))
        verdict = journal.due(self.db)
        self.assertTrue(verdict["due"])
        self.assertEqual(verdict["reason"], "rollover")
        self.assertEqual(verdict["day_key"], yesterday.date().isoformat())

    def test_too_few_lines_never_sleeps(self) -> None:
        self.add_lines([("user", "在")])
        clock.set_now(T0 + timedelta(minutes=30))  # 还是同一天；跨天属于 rollover，不走这条
        verdict = journal.due(self.db)
        self.assertFalse(verdict["due"])
        self.assertEqual(verdict["reason"], "too_few_lines")


class WakeRecentTest(Base):
    def test_card_shows_recent_and_footer_once(self) -> None:
        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集，太顶了"),
                ("user", "今天中午吃了牛肉面，加两个蛋"),
                ("user", "我在杭州做前端开发"),
            ]
        )
        journal.digest(self.db, store=self.store, config=self.app.config)
        text = self.app.wake()["data"]["text"]
        self.assertIn("最近：", text)
        self.assertIn("石头门", text)
        footer = "（以上只是最常用的一小部分；拿不准时用 memory_search 查，别直接说没有）"
        self.assertEqual(text.count(footer), 1)

    def test_no_diary_means_no_recent_line(self) -> None:
        self.store.add("用户叫林野", kind="semantic", grade="S")
        text = self.app.wake()["data"]["text"]
        self.assertNotIn("最近：", text)


class RecallTest(Base):
    def test_daily_event_is_recallable_days_later(self) -> None:
        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集，太顶了"),
                ("user", "今天中午吃了牛肉面，加两个蛋"),
                ("user", "我在杭州做前端开发"),
            ]
        )
        journal.digest(self.db, store=self.store, config=self.app.config)
        clock.set_now(T0 + timedelta(days=3))
        for query in ("我看了什么", "石头门", "我最近吃了什么"):
            items = self.app.search(query, k=3)["data"]["items"]
            self.assertTrue(items, f"{query} 应该能搜到东西")
        self.assertIn("石头门", self.app.search("我看了什么", k=3)["data"]["items"][0]["text"])


class PruneTest(Base):
    def test_prune_only_removes_digested_and_old(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了面"), ("user", "在写前端")])
        journal.digest(self.db, store=self.store, config=self.app.config)
        journal.append(self.db, "还没消化的这一行")
        clock.set_now(T0 + timedelta(days=40))
        removed = journal.prune(self.db, keep_days=30)
        self.assertEqual(removed, 3)
        left = journal.pending(self.db)
        self.assertEqual([row["text"] for row in left], ["还没消化的这一行"])


class SchedulerTest(Base):
    def test_tick_digests_when_idle(self) -> None:
        from pmem.base.config import Config
        from pmem.ops import scheduler

        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集"),
                ("user", "今天中午吃了牛肉面"),
                ("user", "我在杭州做前端开发"),
            ]
        )
        clock.set_now(T0 + timedelta(minutes=50))
        note = scheduler.journal_tick(self.db, Config(), "self")
        self.assertTrue(note["ok"])
        self.assertTrue(note["digest"]["ok"])
        self.assertEqual(len(note["digest"]["memory_ids"]), 3)

    def test_tick_is_quiet_when_not_due(self) -> None:
        from pmem.base.config import Config
        from pmem.ops import scheduler

        self.add_lines([("user", "我昨晚看了石头门")])
        note = scheduler.journal_tick(self.db, Config(), "self")
        self.assertTrue(note["ok"])
        self.assertIn("skipped", note)


class WriterTest(Base):
    def test_no_model_configured_means_template(self) -> None:
        self.assertIsNone(journal.build_writer(self.app.config))

    def test_model_without_key_falls_back_to_none(self) -> None:
        import os

        from pmem.base.config import Config

        cfg = Config()
        old = {k: os.environ.pop(k, None) for k in ("PMEM_BENCH_API_KEY", "DEEPSEEK_API_KEY")}
        try:
            self.assertIsNone(journal.build_writer(cfg, model="deepseek-chat"))
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value

    def test_model_can_come_from_env(self) -> None:
        """没有配置文件也能开模型：daemon 的环境变量就够了。"""
        import os

        from pmem.base.config import Config

        old = {k: os.environ.get(k) for k in ("PMEM_JOURNAL_MODEL", "PMEM_BENCH_API_KEY", "DEEPSEEK_API_KEY")}
        os.environ["PMEM_JOURNAL_MODEL"] = "deepseek-chat"
        os.environ.pop("PMEM_BENCH_API_KEY", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            # 有模型名但没凭证 → 仍然退回模板（不抛、不阻塞）
            self.assertIsNone(journal.build_writer(Config()))
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_facts_are_third_person_even_without_wo(self) -> None:
        self.add_lines(
            [
                ("user", "今天中午吃了牛肉面"),
                ("user", "我妈说明天过来，让我先收拾屋子"),
                ("user", "我们周末去苏州"),
            ]
        )
        result = journal.digest(self.db, store=self.store, config=self.app.config)
        self.assertEqual(
            result["facts"],
            ["用户今天中午吃了牛肉面", "用户妈妈说明天过来，让用户先收拾屋子", "用户周末去苏州"],
        )


class WriteEpisodeTest(Base):
    """「我自己写日记」这条主路：不调模型、不要 key，靠校验兜底。"""

    def _seed(self) -> None:
        self.add_lines(
            [
                ("user", "我昨晚看了石头门，第十二集，太顶了"),
                ("assistant", "那一集确实厉害"),
                ("user", "今天中午吃了牛肉面，加两个蛋"),
                ("user", "我妈说明天过来，让我先收拾屋子"),
            ]
        )

    def test_writes_what_i_wrote(self) -> None:
        self._seed()
        result = journal.write_episode(
            self.db,
            title="石头门和牛肉面",
            lines=["你昨晚看了石头门第十二集，说太顶了", "你中午吃了牛肉面，加了两个蛋"],
            store=self.store,
            config=self.app.config,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["title"], "石头门和牛肉面")
        # 事实仍然来自原话，不来自我写的日记
        self.assertEqual(result["facts"][0], "用户昨晚看了石头门，第十二集，太顶了")
        self.assertEqual(len(result["memory_ids"]), 3)  # 只从用户那三句派生，助手那句不算事实

    def test_invented_name_falls_back(self) -> None:
        self._seed()
        result = journal.write_episode(
            self.db,
            title="深夜",
            lines=["你昨晚看了《海德格尔全集》"],
            store=self.store,
            config=self.app.config,
        )
        self.assertEqual(result["mode"], "template")
        self.assertTrue(any(p.startswith("unknown_name") for p in result["problems"]))

    def test_empty_is_refused(self) -> None:
        self._seed()
        result = journal.write_episode(self.db, title="", lines=[], config=self.app.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "empty")

    def test_nothing_pending_is_refused(self) -> None:
        result = journal.write_episode(self.db, title="x", lines=["你好"], config=self.app.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_lines")


class HintTest(Base):
    """该睡的时候，醒来卡提醒在场的模型自己写——这是主路的触发点。"""

    def test_hint_appears_only_after_idle(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了牛肉面"), ("user", "在写前端")])
        self.assertNotIn("待整理：", self.app.wake()["data"]["text"])
        clock.set_now(T0 + timedelta(minutes=50))
        text = self.app.wake()["data"]["text"]
        self.assertIn("待整理：", text)
        self.assertIn("memory_journal", text)
        self.assertIn("3 行", text)

    def test_hint_goes_away_after_writing(self) -> None:
        self.add_lines([("user", "我昨晚看了石头门"), ("user", "今天吃了牛肉面"), ("user", "在写前端")])
        clock.set_now(T0 + timedelta(minutes=50))
        journal.write_episode(
            self.db, title="今天", lines=["你昨晚看了石头门", "你吃了牛肉面"], store=self.store, config=self.app.config
        )
        self.assertNotIn("待整理：", self.app.wake()["data"]["text"])


class MultiDayTest(Base):
    def test_a_week_of_chatting_becomes_diary_and_stays_recallable(self) -> None:
        """连聊几天：每天空闲后写一篇日记，第五天还能把第一天的动画答出来。"""
        from pmem.base.config import Config
        from pmem.ops import scheduler

        cfg = Config()
        script = [
            (0, ["我昨晚看了石头门，第十二集，太顶了", "今天中午吃了牛肉面，加两个蛋"]),
            (1, ["今天去公司开会，讲了下周的排期"]),
            (2, ["周末去了苏州，那边下雨"]),
        ]
        for offset, texts in script:
            base = T0 + timedelta(days=offset)
            clock.set_now(base)
            for text in texts:
                journal.append(self.db, text, ts=base.isoformat())
            clock.set_now(base + timedelta(minutes=50))  # 空闲够久 → 该睡了
            note = scheduler.journal_tick(self.db, cfg, "self")
            self.assertTrue(note["ok"], note)

        # 剩下的日子等它「老」到 stale_days（默认 2 天），daemon 才用模板结账。
        clock.set_now(T0 + timedelta(days=6))
        for _ in range(3):
            note = scheduler.journal_tick(self.db, cfg, "self")
            self.assertTrue(note["ok"], note)

        episodes = self.db.conn.execute("SELECT COUNT(*) c FROM episode").fetchone()["c"]
        self.assertEqual(episodes, 3)
        facts = self.db.conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
        self.assertEqual(facts, 4)

        # 卡只看最近两天：第三天问，苏州（最后一天）还在窗口里
        clock.set_now(T0 + timedelta(days=3))
        card = self.app.wake()["data"]["text"]
        self.assertIn("最近：", card)
        self.assertIn("苏州", card)

        hits = [it["text"] for it in self.app.search("我看了什么", k=3)["data"]["items"]]
        self.assertTrue(any("石头门" in text for text in hits), hits)


if __name__ == "__main__":
    unittest.main()
