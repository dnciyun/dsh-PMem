"""③ 原话通道的重排（0.34.0）：多扫候选，按「实词×2 + 双字 − 问号惩罚」选 top-2。

为什么单独一个文件：0.33.0 把线索补到 58/58 之后，剩下的瓶颈不在「能不能触发」，
而在「选中的两条是不是答案」。外部靶场 58 道问助手原话的题里，gold 行落在 bm25
第 3–8 名的有 20 道——bm25 只按「用词相近」排，而答案常和问句用词完全不同。
扫 8 条再重排后，gold 进 top-2 从 33 提到 44–45（平坦曲面，见
.tmp/analysis-034/weight_surface.json）。这里钉住四件事：

1. 重排能把 bm25 排第 3 的答案提到第 1；
2. 关掉重排 = 退回 bm25 顺序（消融/对照口径）；
3. scan 是硬上限：扫几条就只能在几条里选，扫不到就不会凭空出现；
4. 打分函数本身：实词×2、双字重合、问号惩罚各占一份。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from pmem.base import clock
from pmem.base.config import Config
from pmem.base.db import Database
from pmem.memory import journal
from pmem.memory.retrieval import channels as channels_mod
from pmem.memory.store import MemoryStore
from pmem.memory.text import normalize

from .support import drop_dir, temp_dir

TS = "2026-11-01T10:00:00Z"

# 一份真实失败形态的复刻（外部靶场 P12-CASE-004）：问「三层柜」那套，
# bm25 把两句都在聊「气话」的闲聊排前面，答案在第 3 名。
QUESTION = "你上回讲自己整理记忆的三层柜时，说吵架的气话放在哪一层？"
CHITCHAT_1 = "行，吵架的气话我不留，也不拿它来说你。气头上的话本来就不算数，过了就过了。"
CHITCHAT_2 = "还醒着呢，我陪你。气话这东西就是这样，白天忙起来压得住，一躺下它就自己冒头。"
ANSWER = (
    "杏子姐，我给自己弄了个记忆柜，分三层：今天、答应、别碰。今天就是白天这些琐碎，"
    "答应是你跟我说过要办的事，吵架气话全归别碰那层，不进心里。"
)
CHITCHAT_3 = "松快了就好，气话放那一层就别老去翻。闷成这样风扇给米米是对的，你自己屋里还凉快吗？"
LINES = [CHITCHAT_1, CHITCHAT_2, ANSWER, CHITCHAT_3]


def _config(*, rerank: bool = True, scan: int = 8, limit: int = 2, reserve: int = 120,
            max_evict: int = 2) -> Config:
    return Config(
        {
            "retrieval": {
                "vector": {"enabled": False},
                "assistant_voice": {
                    "enabled": True,
                    "rerank": rerank,
                    "scan": scan,
                    "limit": limit,
                    "reserve": reserve,
                    "max_evict": max_evict,
                },
            },
            "net": {"auto_extract": False},
        }
    )


class AssistantRerankCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temp_dir("assistant-rerank")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        clock.set_now(datetime.fromisoformat(TS.replace("Z", "+00:00")))
        for index, text in enumerate(LINES):
            journal.append(
                self.db,
                text,
                role="assistant",
                ts=f"2026-11-0{index + 1}T10:00:00Z",
                session_id="s1",
            )

    def tearDown(self) -> None:
        self.db.close()
        clock.reset_now()
        drop_dir(self.dir)

    def _voice(self, cfg: Config) -> list[dict]:
        store = MemoryStore(self.db, config=cfg, subject="self", actor="test")
        return store._assistant_voice(q=QUESTION, terms=[], limit=int(cfg.get("retrieval.assistant_voice.limit")))

    def _texts(self, items: list[dict]) -> list[str]:
        return [str(item.get("text") or "") for item in items]

    def test_rerank_promotes_answer_over_bm25(self) -> None:
        off = self._texts(self._voice(_config(rerank=False)))
        self.assertNotIn(ANSWER, off, f"bm25 顺序下答案本来就该排在后面：{off[:1]}")
        on = self._texts(self._voice(_config(rerank=True)))
        self.assertEqual(on[0], ANSWER, f"重排没把答案提到第 1 位：{on}")

    def test_rerank_off_is_bm25_order(self) -> None:
        off = self._texts(self._voice(_config(rerank=False)))
        # 关掉重排时，返回的就是 bm25 前 limit 条（这里的形态是两句闲聊）。
        self.assertEqual(off[:2], [CHITCHAT_1, CHITCHAT_3])
        self.assertNotIn(ANSWER, off)

    def test_scan_is_hard_cap(self) -> None:
        """扫 2 条时答案（bm25 第 3）根本取不到，重排也变不出来。"""
        narrow = self._texts(self._voice(_config(rerank=True, scan=2)))
        self.assertNotIn(ANSWER, narrow, narrow)
        wide = self._texts(self._voice(_config(rerank=True, scan=8)))
        self.assertIn(ANSWER, wide)

    def test_limit_still_caps_output(self) -> None:
        one = self._voice(_config(rerank=True, limit=1))
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["text"], ANSWER)

    def test_returned_items_are_assistant_role(self) -> None:
        for item in self._voice(_config()):
            self.assertEqual(item["role"], "assistant")
            self.assertEqual(item["source"], "journal")


class RankScoreCase(unittest.TestCase):
    def _score(self, body: str, question: str) -> float:
        from pmem.memory import query as query_mod

        return channels_mod._assistant_rank_score(
            body, query_mod.bigrams(normalize(question)), query_mod.content_terms(normalize(question))
        )

    def test_question_mark_is_penalised(self) -> None:
        statement = "马克杯在书房第二格抽屉里。"
        ask = "马克杯在书房第二格抽屉里吗？"
        self.assertGreater(self._score(statement, QUESTION), self._score(ask, QUESTION))

    def test_more_content_hits_score_higher(self) -> None:
        close = "吵架的气话放别碰那层，不进心里。"
        far = "今天风大，我把阳台的衣服收了。"
        self.assertGreater(self._score(close, QUESTION), self._score(far, QUESTION))
        self.assertEqual(self._score(far, QUESTION), 0.0)

    def test_defaults_exist(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.get("retrieval.assistant_voice.scan"), 8)
        self.assertTrue(cfg.get("retrieval.assistant_voice.rerank"))


class BudgetReserveCase(unittest.TestCase):
    """预留预算：问句命中线索时，允许为原话多花 `reserve` token（只在这条通路上）。"""

    def setUp(self) -> None:
        self.dir = temp_dir("assistant-reserve")
        self.db = Database(self.dir / "pmem.db")
        self.db.init()
        clock.set_now(datetime.fromisoformat(TS.replace("Z", "+00:00")))
        for index, text in enumerate(LINES):
            journal.append(
                self.db,
                text,
                role="assistant",
                ts=f"2026-11-0{index + 1}T10:00:00Z",
                session_id="s1",
            )
        # 用户记忆占满预算：6 条各约 120 token、且和问句有词面交集（否则进不了 payload），
        # 把 base 预算 600 填满，再塞不下原话。
        filler = "用户说：上回讲的三层柜，记忆柜分今天答应别碰三层，吵架的气话放别碰那层。"
        seed = MemoryStore(self.db, config=_config(), subject="self", actor="test")
        for index in range(6):
            seed.add(f"第{index}条：{filler}{filler}", session_id="s2")

    def tearDown(self) -> None:
        self.db.close()
        clock.reset_now()
        drop_dir(self.dir)

    def test_reserve_lets_quote_in_when_budget_is_tight(self) -> None:
        # 先把「不留预算」的行为钉住：关掉置换、base 预算塞不下原话时，它就不该出现。
        tight = MemoryStore(
            self.db, config=_config(reserve=0, max_evict=0), subject="self", actor="test"
        )
        no_reserve = tight.search(QUESTION, k=8, budget=450, record=False)
        self.assertFalse(
            any(ANSWER in str(item.get("text") or "") for item in no_reserve["items"]),
            f"reserve=0 时不该凭空多出预算：{no_reserve['budget_used']}",
        )
        store = MemoryStore(self.db, config=_config(max_evict=0), subject="self", actor="test")
        result = store.search(QUESTION, k=8, budget=450, record=False)
        texts = [str(item.get("text") or "") for item in result["items"]]
        self.assertTrue(any(ANSWER in text for text in texts), texts)
        # 允许超支的部分不超过 reserve。
        self.assertLessEqual(int(result["budget_used"]), 450 + 120)

    def test_config_defaults_include_reserve(self) -> None:
        self.assertEqual(Config().get("retrieval.assistant_voice.reserve"), 120)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
